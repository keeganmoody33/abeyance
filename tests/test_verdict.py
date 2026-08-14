"""The approval math, driven directly. No store, no transport, no clock — the decision
procedure is a pure function and its tests should prove that by not needing anything."""
from __future__ import annotations

import pytest

from abeyance import (ANY_ONE, Approver, ApprovalPolicy, Item, Proposal, UNANIMOUS, Verdict,
                      majority, summarize, verdicts)


def make(approver_ledgers, n_items=3, advisory=()):
    """approver_ledgers: {address: (approved, rejected) | None for 'no reply'}"""
    approvers = {}
    for addr, ledger in approver_ledgers.items():
        a = Approver(addr)
        if ledger is not None:
            a.approved, a.rejected = list(ledger[0]), list(ledger[1])
            a.replied_at = "2026-01-01T00:00:00Z"
        approvers[a.address] = a
    return Proposal(
        id="t1",
        items=[Item(n=i, summary=f"i{i}", advisory=(i in advisory))
               for i in range(1, n_items + 1)],
        approvers=approvers)


# --------------------------------------------------------------------------- unanimous


def test_unanimous_needs_every_approver():
    p = make({"a@x": ([1], []), "b@x": None})
    v = verdicts(p, UNANIMOUS)
    assert v[1] is Verdict.WAITING, "one yes out of two is not consent"


def test_unanimous_passes_when_all_approve():
    p = make({"a@x": ([1, 2], []), "b@x": ([1], [])})
    v = verdicts(p, UNANIMOUS)
    assert v[1] is Verdict.APPROVED
    assert v[2] is Verdict.UNREACHABLE, "b answered and passed over 2 — nobody vetoed it, it "\
                                        "simply cannot reach unanimity"


def test_split_is_a_deadlock_not_a_majority():
    """The load-bearing case. Two people with equal standing disagreed; the machine must
    refuse to pick a side rather than quietly take one."""
    p = make({"a@x": ([1], []), "b@x": ([], [1])})
    assert verdicts(p, UNANIMOUS)[1] is Verdict.DEADLOCKED


def test_lone_rejection_is_a_rejection_not_a_deadlock():
    p = make({"a@x": ([], [1]), "b@x": None})
    assert verdicts(p, UNANIMOUS)[1] is Verdict.REJECTED


def test_silence_on_an_item_is_never_a_rejection():
    """Silence is not a no. It is either "not yet" or "not enough", never "against" — the
    difference matters because a rejection is a decision you should not re-ask, and the other
    two are worth re-proposing."""
    nobody_replied = make({"a@x": None, "b@x": None})
    assert verdicts(nobody_replied, UNANIMOUS)[2] is Verdict.WAITING

    one_answered_and_passed_over_it = make({"a@x": ([1], []), "b@x": None})
    assert verdicts(one_answered_and_passed_over_it, UNANIMOUS)[2] is Verdict.UNREACHABLE


def test_silence_can_stay_open_under_the_waiting_reading():
    """`silence_after_reply="waiting"` is for loops with a human driving follow-ups. On a cron
    it means waiting forever, which is why it is not the default."""
    policy = ApprovalPolicy(silence_after_reply="waiting")
    p = make({"a@x": ([1], []), "b@x": None})
    assert verdicts(p, policy)[2] is Verdict.WAITING


# --------------------------------------------------------------------------- thresholds


def test_any_one_passes_on_first_yes():
    p = make({"a@x": ([1], []), "b@x": None})
    assert verdicts(p, ANY_ONE)[1] is Verdict.APPROVED


def test_no_veto_means_a_rejection_is_only_an_abstention():
    p = make({"a@x": ([1], []), "b@x": ([], [1])})
    assert verdicts(p, ANY_ONE)[1] is Verdict.APPROVED


def test_veto_off_but_threshold_unreachable():
    """Nobody blocked it; it just cannot get to two yeses any more. That is UNREACHABLE, and
    it is deliberately not REJECTED — nothing was decided, so it is worth re-proposing."""
    policy = ApprovalPolicy(threshold=2, veto=False)
    p = make({"a@x": ([1], []), "b@x": ([], []), "c@x": ([], [])})
    assert verdicts(p, policy)[1] is Verdict.UNREACHABLE


def test_majority_of_three():
    policy = majority(3)
    p = make({"a@x": ([1], []), "b@x": ([1], []), "c@x": None})
    assert verdicts(p, policy)[1] is Verdict.APPROVED


def test_threshold_is_clamped_to_the_panel_size():
    """A policy asking for four yeses from two people would wait forever. Clamping keeps a
    misconfigured threshold from becoming a silent deadlock."""
    policy = ApprovalPolicy(threshold=4, veto=False)
    p = make({"a@x": ([1], []), "b@x": ([1], [])})
    assert verdicts(p, policy)[1] is Verdict.APPROVED


# --------------------------------------------------------------------------- advisory


def test_advisory_items_never_block_and_never_wait():
    p = make({"a@x": None, "b@x": None}, advisory=(3,))
    v = verdicts(p, UNANIMOUS)
    assert v[3] is Verdict.APPROVED
    assert summarize(p, UNANIMOUS).waiting == [1, 2]


# --------------------------------------------------------------------------- summary


def test_summary_is_settled_only_when_nothing_can_change():
    both_still_out = make({"a@x": ([1], [2]), "b@x": None})
    s = summarize(both_still_out, UNANIMOUS)
    assert s.rejected == [2]
    assert s.waiting == [1], "b has not answered, so a's yes on 1 can still become unanimous"
    assert s.unreachable == [3], "a already answered and passed over 3"
    assert not s.settled

    both_answered = make({"a@x": ([1], [2]), "b@x": ([1], [2])})
    s2 = summarize(both_answered, UNANIMOUS)
    assert s2.approved == [1]
    assert s2.rejected == [2]
    assert s2.unreachable == [3], "neither vetoed 3; neither approved it either"
    assert s2.settled, "a settled proposal is one no further reply could change"


def test_no_approvers_waits_rather_than_passing():
    """Vacuous truth would make an empty approver set approve everything. It must not."""
    p = make({})
    assert verdicts(p, UNANIMOUS)[1] is Verdict.WAITING


@pytest.mark.parametrize("bad", [
    {"threshold": 0},
    {"expire_after_days": 0},
    {"max_turns": 0},
    {"nudge_cap": 3, "nudge_after_hours": (1.0,)},
    {"nudge_after_hours": (24.0, 400.0), "nudge_cap": 2},   # second nudge is after expiry
])
def test_policy_validation_rejects_incoherent_settings(bad):
    with pytest.raises(ValueError):
        ApprovalPolicy(**bad).validate()
