"""Dispatch: did the container actually run, and what happens when it did not.

This is the file that covers the failure with no error message — a worker the platform accepted
and never booted. Every test here is a state a real platform produces at the worst possible
moment, which is exactly why they are driven through `MemoryRunner` rather than against a cloud.
"""
from __future__ import annotations

import json

import pytest

from abeyance import (Actor, Case, CasePolicy, Contribution, ContributionKind,
                      ContributionRequest, Dispatcher, Escalation, RequestStatus, RunState)
from abeyance.adapters import MemoryRunner


@pytest.fixture
def case():
    return Case(id="case-1", subject_key="acme", action="launch-campaign",
                requests=[ContributionRequest(id="perf", need="campaign-performance",
                                              capability="db-evidence",
                                              spec={"client": "acme", "window_days": 30})])


@pytest.fixture
def make_dispatcher(runner, registry, clock, escalations):
    def _make(policy=None, env_for=None):
        return Dispatcher(runner, registry, policy=policy or CasePolicy(), clock=clock,
                          env_for=env_for, on_escalate=escalations.append, label="test")
    return _make


def evidence(case_id="case-1", request_id="perf", epoch=1, payload=None):
    return Contribution(case_id=case_id, request_id=request_id,
                        kind=ContributionKind.EVIDENCE, actor=Actor.worker("db-evidence"),
                        payload=payload or {"reply_rate": 3.1}, created_epoch=epoch)


# --------------------------------------------------------------- starting


def test_a_requested_need_starts_a_container_with_the_declared_image(case, make_dispatcher,
                                                                    runner):
    report = make_dispatcher().tick(case, [])

    assert [r.action for r in report.records] == ["dispatched"]
    assert report.changed is True
    started = runner.last
    assert started["image"] == "postgres:16-alpine"
    assert started["app"] == "workers-readonly", "the app is the isolation boundary"
    assert case.request("perf").status is RequestStatus.DISPATCHED
    assert case.request("perf").attempts == 1
    assert case.request("perf").machine_ref == started["ref"]


def test_the_worker_contract_env_is_everything_a_worker_needs_and_no_more(case, make_dispatcher,
                                                                         runner):
    make_dispatcher().tick(case, [])
    env = runner.env_of(runner.last["ref"])

    assert env["ABEYANCE_CASE_ID"] == "case-1"
    assert env["ABEYANCE_REQUEST_ID"] == "perf"
    assert env["ABEYANCE_NEED"] == "campaign-performance"
    assert env["ABEYANCE_EXPECTS"] == "evidence"
    assert env["ABEYANCE_ACTOR"] == "worker:db-evidence"
    assert json.loads(env["ABEYANCE_SPEC"]) == {"client": "acme", "window_days": 30}
    assert not any("PASSWORD" in k or "TOKEN" in k or "DSN" in k for k in env), (
        "the dispatcher must never invent a credential — the grant is the caller's, via env_for")


def test_env_for_is_where_a_credential_grant_becomes_visible(case, make_dispatcher, runner):
    grants = []

    def env_for(cap, c, req):
        grants.append((cap.name, cap.reach, req.id))
        return {"STORE_DSN": "postgres://ro@host/db"} if "db-read" in cap.reach else {}

    make_dispatcher(env_for=env_for).tick(case, [])
    assert grants == [("db-evidence", ("db-read",), "perf")]
    assert runner.env_of(runner.last["ref"])["STORE_DSN"] == "postgres://ro@host/db"


def test_the_lease_is_derived_from_the_capabilitys_own_declared_timeout(case, make_dispatcher,
                                                                       clock):
    make_dispatcher(CasePolicy(lease_grace_seconds=30)).tick(case, [])
    req = case.request("perf")
    assert req.lease_expires_epoch == clock.now() + 120 + 30, (
        "the worker declares how long it needs; the lease follows it rather than a global guess")


# --------------------------------------------------------------- collecting


def test_a_contribution_settles_the_request(case, make_dispatcher):
    d = make_dispatcher()
    d.tick(case, [])
    report = d.tick(case, [evidence()])

    assert [r.action for r in report.records] == ["satisfied"]
    assert case.request("perf").status is RequestStatus.SATISFIED


def test_collect_happens_before_the_overdue_check(case, make_dispatcher, clock, runner):
    """A worker that finished just as its lease ran out must settle, not be duplicated."""
    d = make_dispatcher()
    d.tick(case, [])
    clock.advance(hours=2)
    runner.set_state(runner.last["ref"], RunState.EXITED)

    report = d.tick(case, [evidence()])
    assert [r.action for r in report.records] == ["satisfied"]
    assert len(runner.started) == 1, "no second container for work that succeeded"


def test_an_in_flight_worker_inside_its_lease_is_left_alone(case, make_dispatcher, clock, runner):
    d = make_dispatcher()
    d.tick(case, [])
    clock.advance(seconds=30)

    report = d.tick(case, [])
    assert [r.action for r in report.records] == ["in-flight"]
    assert len(runner.started) == 1
    assert report.changed is False, "a quiet tick must not rewrite the row"


# --------------------------------------------------------------- the lost dispatch


def test_a_container_that_never_booted_is_retried_and_escalated(case, make_dispatcher, clock,
                                                                runner, escalations):
    """The failure with no error message: the platform accepted it and nothing ran."""
    d = make_dispatcher()
    d.tick(case, [])
    first_ref = runner.last["ref"]
    runner.set_state(first_ref, RunState.GONE)
    clock.advance(hours=1)

    report = d.tick(case, [])

    assert [r.action for r in report.records] == ["redispatched"]
    assert case.request("perf").attempts == 2
    assert case.request("perf").machine_ref != first_ref
    assert len(runner.started) == 2
    assert [e.kind for e in escalations] == [Escalation.DISPATCH_LOST]
    assert "gone" in escalations[0].detail


def test_a_worker_that_exited_cleanly_without_contributing_counts_as_failed(case, make_dispatcher,
                                                                           clock, runner,
                                                                           escalations):
    """Exit status is not the signal. A satisfied request is.

    The reason lives on the escalation and in the case history, not on the redispatch record —
    that record describes the new attempt, and conflating "why the last one died" with "what we
    just started" makes a tick log harder to read, not easier.
    """
    d = make_dispatcher()
    d.tick(case, [])
    runner.set_state(runner.last["ref"], RunState.EXITED)
    clock.advance(hours=1)

    report = d.tick(case, [])
    assert [r.action for r in report.records] == ["redispatched"]
    assert "exited with no contribution" in escalations[-1].detail
    assert [h["state"] for h in case.history if h["event"] == "dispatch-lost"] == ["exited"]


def test_a_worker_still_running_past_its_lease_is_stopped_then_retried(case, make_dispatcher,
                                                                      clock, runner):
    d = make_dispatcher()
    d.tick(case, [])
    stuck = runner.last["ref"]
    clock.advance(hours=1)  # RUNNING is MemoryRunner's default state

    report = d.tick(case, [])
    assert stuck in runner.stopped, (
        "a hung worker holding a case open forever is worse than a duplicated one")
    assert [r.action for r in report.records] == ["redispatched"]


def test_an_unreachable_platform_extends_rather_than_duplicating(case, make_dispatcher, clock,
                                                                runner):
    """`state()` raising means we do not know. Duplicating a healthy worker on the strength of
    a network blip is the wrong way to fail."""
    d = make_dispatcher()
    d.tick(case, [])
    clock.advance(hours=1)

    def boom(ref):
        raise RuntimeError("api unreachable")

    runner.state = boom
    report = d.tick(case, [])

    assert [r.action for r in report.records] == ["in-flight"]
    assert "state unknown" in report.records[0].detail
    assert len(runner.started) == 1
    assert case.request("perf").lease_expires_epoch > clock.now()


def test_attempts_are_exhausted_into_a_failure_that_blocks_the_case(case, make_dispatcher, clock,
                                                                   runner, escalations):
    d = make_dispatcher(CasePolicy(max_attempts=2))
    d.tick(case, [])
    for _ in range(3):
        runner.set_state(runner.last["ref"], RunState.GONE)
        clock.advance(hours=1)
        d.tick(case, [])

    req = case.request("perf")
    assert req.status is RequestStatus.FAILED
    assert req.attempts == 2
    assert req.blocks_authorization is True, (
        "a case whose evidence could not be gathered must not proceed on what it happens to have")
    kinds = [e.kind for e in escalations]
    assert Escalation.REQUEST_FAILED in kinds


def test_a_failed_request_is_not_retried_forever(case, make_dispatcher, clock, runner):
    d = make_dispatcher(CasePolicy(max_attempts=1))
    d.tick(case, [])
    runner.set_state(runner.last["ref"], RunState.GONE)
    clock.advance(hours=1)
    d.tick(case, [])
    assert case.request("perf").status is RequestStatus.FAILED

    before = len(runner.started)
    report = d.tick(case, [])
    assert len(runner.started) == before
    assert [r.action for r in report.records] == ["skipped"]


# --------------------------------------------------------------- platform refusal


def test_a_platform_that_refuses_to_start_burns_an_attempt(case, make_dispatcher, runner):
    """Otherwise a permanently broken image retries forever and the case never blocks."""
    d = make_dispatcher()
    runner.fail_next = "no capacity in iad"
    report = d.tick(case, [])

    assert case.request("perf").attempts == 1
    assert case.request("perf").status is RequestStatus.REQUESTED
    assert "no capacity" in case.request("perf").last_error
    assert [r.action for r in report.records] == ["skipped"]


def test_repeated_refusal_ends_in_a_failure(case, make_dispatcher, runner, escalations):
    d = make_dispatcher(CasePolicy(max_attempts=2))
    for _ in range(2):
        runner.fail_next = "no capacity"
        d.tick(case, [])
    assert case.request("perf").status is RequestStatus.FAILED
    assert Escalation.REQUEST_FAILED in [e.kind for e in escalations]


# --------------------------------------------------------------- the ceiling


def test_a_need_no_capability_produces_fails_loudly_instead_of_improvising(make_dispatcher,
                                                                          escalations, runner):
    case = Case(id="c", action="a", requests=[
        ContributionRequest(id="wire", need="wire-money", capability="whatever")])
    report = make_dispatcher().tick(case, [])

    assert case.request("wire").status is RequestStatus.FAILED
    assert [r.action for r in report.records] == ["unmatched"]
    assert runner.started == [], "nothing is started when the need is unreachable"
    assert [e.kind for e in escalations] == [Escalation.CAPABILITY_MISSING]
    assert "human decision" in escalations[0].detail


def test_a_request_with_no_capability_is_answered_out_of_band_and_never_dispatched(
        make_dispatcher, runner):
    """The `human-decision` shape. It still blocks authorization, which is what makes
    "we never asked anyone" impossible to miss."""
    case = Case(id="c", action="a", requests=[
        ContributionRequest(id="human-decision", need="human-decision", capability="",
                            expects=ContributionKind.DECISION)])
    report = make_dispatcher().tick(case, [])

    assert [r.action for r in report.records] == ["external"]
    assert runner.started == []
    assert case.request("human-decision").status is RequestStatus.REQUESTED
    assert case.request("human-decision").blocks_authorization is True


# --------------------------------------------------------------- bookkeeping


def test_a_cancelled_request_is_skipped(case, make_dispatcher, runner):
    case.request("perf").status = RequestStatus.CANCELLED
    report = make_dispatcher().tick(case, [])
    assert [r.action for r in report.records] == ["skipped"]
    assert runner.started == []


def test_a_satisfied_request_whose_contribution_vanished_says_so(case, make_dispatcher, runner):
    """A deleted row or a restored backup. Re-running silently would be worse — the case may
    already have acted on it."""
    case.request("perf").status = RequestStatus.SATISFIED
    report = make_dispatcher().tick(case, [])
    assert [r.action for r in report.records] == ["skipped"]
    assert "contribution not found" in report.records[0].detail
    assert runner.started == []


def test_the_case_history_records_every_dispatch_for_the_audit_trail(case, make_dispatcher,
                                                                    clock, runner):
    d = make_dispatcher()
    d.tick(case, [])
    runner.set_state(runner.last["ref"], RunState.GONE)
    clock.advance(hours=1)
    d.tick(case, [])

    events = [h["event"] for h in case.history]
    assert events.count("dispatched") == 2
    assert "dispatch-lost" in events
    assert case.history[0]["image"] == "postgres:16-alpine"
    assert case.history[0]["runner"] == "memory"


def test_the_dispatcher_holds_nothing_between_ticks(case, registry, runner, clock, escalations):
    """A fresh Dispatcher must pick up exactly where the last one left off, because in
    production each tick is a different process."""
    Dispatcher(runner, registry, clock=clock, on_escalate=escalations.append).tick(case, [])
    ref = case.request("perf").machine_ref

    fresh = Dispatcher(runner, registry, clock=clock, on_escalate=escalations.append)
    report = fresh.tick(case, [])
    assert [r.action for r in report.records] == ["in-flight"]
    assert case.request("perf").machine_ref == ref
