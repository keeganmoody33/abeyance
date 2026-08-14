"""Time, turns, nudges, and the ways a detached wait goes wrong.

These are the paths that only appear after days of real elapsed time, which is why the clock
is injectable — untestable time-based behaviour is untested time-based behaviour, and every
one of these cases has a production failure behind it.
"""
from __future__ import annotations

import pytest

from conftest import approvers, items
from detached import (ApprovalPolicy, Escalation, SINGLE_APPROVER, Status)


# --------------------------------------------------------------------------- expiry


def test_expiry_settles_an_unanswered_proposal_and_says_so(make_loop, clock, escalations):
    """An expiry that passes silently is indistinguishable from a healthy quiet week, and the
    work in it dies unnoticed. Every expiry escalates."""
    loop = make_loop(policy=ApprovalPolicy(expire_after_days=7))
    res = loop.propose(items(2), approvers("a@x.test"))

    clock.advance(days=6)
    assert loop.sweep()["expired"] == []

    clock.advance(days=2)
    assert loop.sweep()["expired"] == [res.id]
    assert loop.get(res.id).status is Status.EXPIRED
    ev = [e for e in escalations if e.kind is Escalation.EXPIRY]
    assert ev and "a@x.test" in ev[0].detail


def test_replying_restarts_the_expiry_clock(make_loop, transport, clock):
    """The bug this prevents: expiry anchored on send time kills a live negotiation at the
    deadline, mid-sentence, while both parties are actively talking."""
    loop = make_loop(policy=ApprovalPolicy(expire_after_days=7, max_turns=5))
    res = loop.propose(items(2), approvers("a@x.test", "b@x.test"))

    clock.advance(days=6)
    transport.receive(res.id, "a@x.test", "approve 1", epoch=clock.now())
    loop.record_from(res.id, loop.read(res.id)[0])

    clock.advance(days=4)          # 10 days after send, but only 4 since the last activity
    assert loop.sweep()["expired"] == []
    assert loop.get(res.id).is_open

    clock.advance(days=4)          # now 8 days of actual silence
    assert loop.sweep()["expired"] == [res.id]


def test_poll_settles_expiries_for_free(make_loop, clock):
    loop = make_loop(policy=ApprovalPolicy(expire_after_days=1, nudge_after_hours=(6.0,),
                                           nudge_cap=1))
    res = loop.propose(items(1), approvers("a@x.test"))
    clock.advance(days=2)
    poll = loop.poll()
    assert poll.expired == [res.id]
    assert poll.actionable == []


# --------------------------------------------------------------------------- turns


def test_turn_cap_stalls_instead_of_asking_forever(make_loop, transport, clock, escalations):
    """A loop that keeps asking gets filtered, and then the *next* real proposal is ignored
    too. Better to stop and say so."""
    loop = make_loop(policy=ApprovalPolicy(max_turns=3, expire_after_days=30))
    res = loop.propose(items(1), approvers("a@x.test"))

    assert loop.ask(res.id, "which part of item 1?") is not None      # turn 2
    assert loop.ask(res.id, "still unclear — the first or second line?") is not None  # turn 3
    assert loop.ask(res.id, "one more time?") is None                 # capped

    assert loop.get(res.id).status is Status.STALLED
    assert any(e.kind is Escalation.STALL for e in escalations)


def test_a_clarification_keeps_the_thread(loop, transport):
    res = loop.propose(items(1), approvers("a@x.test"), subject_key="acme")
    loop.ask(res.id, "which line did you mean?")
    assert transport.sent[-1].thread_id == res.id
    assert transport.sent[-1].subject.startswith("Re: ")
    assert loop.get(res.id).status is Status.CLARIFYING


# --------------------------------------------------------------------------- nudges


def test_nudges_fire_on_schedule_and_stop_at_the_cap(make_loop, clock, notifier):
    loop = make_loop(policy=ApprovalPolicy(expire_after_days=7,
                                           nudge_after_hours=(24.0, 72.0), nudge_cap=2))
    res = loop.propose(items(1), approvers("a@x.test"))

    assert loop.nudge().sent == []                    # too soon

    clock.advance(hours=25)
    assert len(loop.nudge().sent) == 1
    assert len(notifier.sent) == 1

    clock.advance(hours=1)
    assert loop.nudge().sent == [], "the second nudge is not due at 26h"

    clock.advance(hours=50)                           # 75h of silence
    assert len(loop.nudge().sent) == 1
    assert len(notifier.sent) == 2

    clock.advance(days=2)
    assert loop.nudge().sent == [], "capped — an uncapped reminder becomes a filter rule"


def test_an_approver_who_replied_is_never_nudged(make_loop, transport, clock, notifier):
    loop = make_loop(policy=ApprovalPolicy(expire_after_days=14,
                                           nudge_after_hours=(24.0,), nudge_cap=1))
    res = loop.propose(items(1), approvers("a@x.test", "b@x.test"))
    transport.receive(res.id, "a@x.test", "approve 1", epoch=clock.now())
    loop.record_from(res.id, loop.read(res.id)[0])

    clock.advance(hours=30)
    sent = loop.nudge().sent
    assert [s["to"] for s in sent] == ["b@x.test"]


def test_nudge_without_a_channel_is_reported_not_silently_dropped(make_loop, clock):
    loop = make_loop(policy=ApprovalPolicy(expire_after_days=7, nudge_after_hours=(1.0,),
                                           nudge_cap=1))
    res = loop.propose(items(1), approvers("a@x.test", channel=False))
    clock.advance(hours=2)
    out = loop.nudge()
    assert out.sent == []
    assert out.skipped and "channel_id" in out.skipped[0]["error"]


def test_nudge_dry_run_sends_nothing(make_loop, clock, notifier):
    loop = make_loop(policy=ApprovalPolicy(expire_after_days=7, nudge_after_hours=(1.0,),
                                           nudge_cap=1))
    loop.propose(items(1), approvers("a@x.test"))
    clock.advance(hours=2)
    assert len(loop.nudge(dry_run=True).sent) == 1
    assert notifier.sent == []
