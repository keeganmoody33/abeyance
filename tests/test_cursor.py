"""The due-gate and the watermark guard.

The guard test is the one that matters. A cursor that advances after a failed send skips that
window **permanently and silently** — no error, no retry, no gap anywhere to notice. Making
that a raised exception rather than a documented rule is the entire point of `CursorRun`.
"""
from __future__ import annotations

import pytest

from detached import CursorNotCommittable, DueGate, TriggerResult
from detached.adapters import MemoryStore


@pytest.fixture
def gate(clock):
    return DueGate(MemoryStore(), name="t", floor_days=14, clock=clock)


def fired(reason="something new", **marks):
    return lambda subject, cursor: TriggerResult(fired=True, reason=reason, marks=marks)


def quiet(reason=""):
    return lambda subject, cursor: TriggerResult(fired=False, reason=reason)


# --------------------------------------------------------------------------- due


def test_a_never_run_subject_is_always_due(gate):
    gate.register("event", quiet())
    v = gate.evaluate("acme")
    assert v.due and v.trigger == "floor-sweep"
    assert "never run" in " ".join(v.reasons)


def test_a_trigger_makes_a_subject_due_and_hands_marks_forward(gate, clock):
    gate.register("event", fired("new event 12345", last_event="12345"))
    with gate.begin("acme") as run:
        run.advance()

    v = gate.evaluate("acme")
    assert v.due and v.trigger == "event"
    assert v.marks == {"last_event": "12345"}


def test_the_floor_sweep_catches_a_trigger_that_has_gone_dead(gate, clock):
    """Trigger-only scheduling means a subject whose source breaks — revoked token, renamed
    channel, integration quietly returning empty — is never due again, and looks exactly like
    a quiet subject. The floor turns that silence into a run."""
    gate.register("event", quiet("no events"))
    with gate.begin("acme") as run:
        run.advance()

    clock.advance(days=10)
    assert not gate.evaluate("acme").due

    clock.advance(days=5)
    v = gate.evaluate("acme")
    assert v.due and v.trigger == "floor-sweep"


def test_expensive_triggers_only_run_on_a_deep_pass(gate):
    calls = []

    def costly(subject, cursor):
        calls.append(subject)
        return TriggerResult(fired=False)

    gate.register("cheap", quiet())
    gate.register("crawl", costly, expensive=True)

    gate.evaluate("acme", deep=False)
    assert calls == []
    gate.evaluate("acme", deep=True)
    assert calls == ["acme"]


def test_an_expensive_trigger_is_skipped_once_the_subject_is_already_due(gate):
    calls = []
    gate.register("cheap", fired())
    gate.register("crawl", lambda s, c: calls.append(s) or TriggerResult(fired=False),
                  expensive=True)
    gate.evaluate("acme", deep=True)
    assert calls == [], "the answer cannot change and the call costs real money"


def test_a_broken_trigger_does_not_blind_the_gate(gate):
    def boom(subject, cursor):
        raise RuntimeError("api down")

    gate.register("broken", boom)
    gate.register("good", fired("real event"))
    v = gate.evaluate("acme")
    assert v.due
    assert any("api down" in r for r in v.reasons)


def test_a_blocked_subject_is_reported_not_quietly_skipped(gate):
    """Blocked and not-due are the same shape in a log and very different problems: one is a
    healthy quiet week, the other is a misconfiguration that will never resolve itself."""
    gate.precondition(lambda s, c: "no approver configured" if s == "acme" else None)
    gate.register("event", fired())
    v = gate.evaluate("acme")
    assert v.blocked and not v.due
    assert gate.due(["acme"]) == []


# --------------------------------------------------------------------------- the guard


def test_advance_refuses_while_a_precondition_is_outstanding(gate):
    run = gate.begin("acme")
    run.require("ledger", "the append-only record")
    run.require("digest", "the approval email")
    run.satisfied("ledger", ref="abc123")

    with pytest.raises(CursorNotCommittable) as e:
        run.advance(marks={"last_event": "9"})
    assert "digest" in str(e.value)
    assert gate.cursor("acme").last_run is None, "the window is still unread — recoverable"


def test_advance_succeeds_once_everything_landed(gate):
    run = gate.begin("acme")
    run.require("ledger")
    run.require("digest")
    run.satisfied("ledger", "abc")
    run.satisfied("digest", "thread-1")
    cur = run.advance(marks={"last_event": "9"}, seen={"comments": ["c1", "c2"]})

    assert cur.last_run and cur.runs == 1
    assert gate.cursor("acme").marks["last_event"] == "9"
    assert gate.cursor("acme").seen["comments"] == ["c1", "c2"]


def test_a_failed_step_records_why(gate):
    run = gate.begin("acme")
    run.require("digest")
    run.failed("digest", reason="smtp 550")
    with pytest.raises(CursorNotCommittable):
        run.advance()
    assert "smtp 550" in str(gate.cursor("acme").marks["_failures"])


def test_declaring_satisfaction_for_something_never_required_is_a_bug(gate):
    run = gate.begin("acme")
    with pytest.raises(KeyError):
        run.satisfied("typo")


def test_advancing_twice_on_one_run_is_refused(gate):
    run = gate.begin("acme")
    run.advance()
    with pytest.raises(CursorNotCommittable):
        run.advance()


def test_an_exception_inside_the_run_abandons_rather_than_advances(gate):
    with pytest.raises(RuntimeError):
        with gate.begin("acme") as run:
            run.require("digest")
            raise RuntimeError("send blew up")
    assert gate.cursor("acme").last_run is None
    assert gate.cursor("acme").marks["_abandoned"]


# --------------------------------------------------------------------------- seen sets


def test_seen_sets_are_bounded_and_deduped(gate):
    cur = gate.cursor("acme")
    cur.remember("comments", [f"c{i}" for i in range(600)], cap=500)
    assert len(cur.seen["comments"]) == 500
    assert cur.seen["comments"][-1] == "c599", "newest kept"

    cur.remember("comments", ["c599"], cap=500)
    assert cur.seen["comments"].count("c599") == 1


def test_unseen_filters_against_the_remembered_set(gate):
    cur = gate.cursor("acme")
    cur.remember("comments", ["a", "b"])
    assert cur.unseen("comments", ["a", "b", "c"]) == ["c"]
