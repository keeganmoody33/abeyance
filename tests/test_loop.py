"""End-to-end through the state machine, on in-memory adapters.

Every test here is the shape of a real tick: propose in one call, then a *separate* call that
knows nothing except what is in the store. Nothing is held open between them, which is the
property the library exists to provide.
"""
from __future__ import annotations

import pytest

from conftest import approvers, items
from detached import (AlreadyExecuted, ApprovalPolicy, Escalation, Item, ItemOutcome,
                      NoApproversError, SINGLE_APPROVER, Status, UnknownApprover, Verdict)


def test_propose_sends_once_and_persists(loop, transport):
    res = loop.propose(items(3), approvers("a@x.test"), subject_key="acme")
    assert res.sent
    assert len(transport.sent) == 1
    assert transport.sent[0].to == ["a@x.test"]

    # The whole point: a fresh reader of the store can reconstruct everything.
    p = loop.get(res.id)
    assert p.status is Status.AWAITING_REPLY
    assert len(p.items) == 3
    assert p.waiting_on == ["a@x.test"]


def test_dry_run_renders_without_sending(loop, transport):
    res = loop.propose(items(2), approvers("a@x.test"), subject_key="acme", dry_run=True)
    assert not res.sent
    assert transport.sent == []
    assert "1." in res.body and "2." in res.body
    assert "expires" in res.body or "expire" in res.body


def test_a_proposal_with_no_actionable_items_is_not_sent(loop, transport):
    res = loop.propose([Item(n=1, summary="fyi", advisory=True)], approvers("a@x.test"))
    assert not res.sent and res.skipped
    assert transport.sent == []


def test_refuses_a_proposal_nobody_can_approve(loop):
    with pytest.raises(NoApproversError):
        loop.propose(items(1), [])


def test_poll_is_quiet_until_somebody_replies(loop, transport):
    res = loop.propose(items(2), approvers("a@x.test"))
    assert loop.poll().actionable == []

    transport.receive(res.id, "a@x.test", "approve 1")
    poll = loop.poll()
    assert poll.actionable == [res.id]
    assert bool(poll) is True


def test_read_records_nothing(loop, transport):
    res = loop.propose(items(2), approvers("a@x.test"))
    transport.receive(res.id, "a@x.test", "approve 1, skip 2")

    inbound = loop.read(res.id)
    assert len(inbound) == 1
    assert inbound[0].suggestion.approve == [1]
    # Nothing was written by reading.
    assert loop.get(res.id).approver("a@x.test").replied_at is None
    # ... and it is still actionable, so a crash mid-tick loses nothing.
    assert loop.poll().actionable == [res.id]


def test_full_single_approver_cycle(loop, transport):
    res = loop.propose(items(3), approvers("a@x.test"), subject_key="acme")
    transport.receive(res.id, "a@x.test", "approve 1 and 3, skip 2")

    inbound = loop.read(res.id)[0]
    loop.record_from(res.id, inbound)

    ran = []
    report = loop.execute(res.id, lambda item: ran.append(item.n) or {"written": True})
    assert ran == [1, 3]
    assert report.verdicts[2] is Verdict.REJECTED
    assert loop.get(res.id).status is Status.EXECUTED


def test_execute_blocks_while_an_approver_is_outstanding(loop, transport):
    res = loop.propose(items(2), approvers("a@x.test", "b@x.test"))
    transport.receive(res.id, "a@x.test", "approve 1,2")
    loop.record_from(res.id, loop.read(res.id)[0])

    report = loop.execute(res.id, lambda item: {"written": True})
    assert report.blocked
    assert report.outcomes == []
    assert loop.get(res.id).status is Status.PARTIALLY_APPROVED


def test_execute_is_idempotent(loop, transport):
    res = loop.propose(items(1), approvers("a@x.test"))
    transport.receive(res.id, "a@x.test", "approve 1")
    loop.record_from(res.id, loop.read(res.id)[0])

    calls = []
    loop.execute(res.id, lambda item: calls.append(item.n))
    with pytest.raises(AlreadyExecuted):
        loop.execute(res.id, lambda item: calls.append(item.n))
    assert calls == [1], "an hourly apply tick must not double-write"


def test_a_refusing_item_does_not_abandon_the_rest(loop, transport, escalations):
    """One item whose target moved must not take the other yeses down with it — the approver
    would have no way to know which of their decisions took effect."""
    res = loop.propose(items(3), approvers("a@x.test"))
    transport.receive(res.id, "a@x.test", "all")
    loop.record_from(res.id, loop.read(res.id)[0])

    def executor(item):
        if item.n == 2:
            raise RuntimeError("anchor not found")
        return {"written": True}

    report = loop.execute(res.id, executor)
    assert [o.n for o in report.written] == [1, 3]
    assert [o.n for o in report.refused] == [2]
    assert "anchor not found" in report.refused[0].error
    assert any(e.kind is Escalation.EXECUTION_REFUSED for e in escalations)


def test_advisory_items_are_never_executed(loop, transport):
    res = loop.propose(items(3, advisory=(3,)), approvers("a@x.test"))
    transport.receive(res.id, "a@x.test", "all")
    loop.record_from(res.id, loop.read(res.id)[0])

    ran = []
    loop.execute(res.id, lambda item: ran.append(item.n))
    assert 3 not in ran


def test_dry_run_execute_changes_no_state(loop, transport):
    res = loop.propose(items(2), approvers("a@x.test"))
    transport.receive(res.id, "a@x.test", "all")
    loop.record_from(res.id, loop.read(res.id)[0])

    ran = []
    report = loop.execute(res.id, lambda item: ran.append(item.n), dry_run=True)
    assert ran == []
    assert loop.get(res.id).status is not Status.EXECUTED
    assert all(o.skipped == "dry run" for o in report.outcomes)


def test_executor_return_shapes(loop, transport):
    res = loop.propose(items(3), approvers("a@x.test"))
    transport.receive(res.id, "a@x.test", "all")
    loop.record_from(res.id, loop.read(res.id)[0])

    def executor(item):
        if item.n == 1:
            return None                                    # None means "done"
        if item.n == 2:
            return {"written": True, "path": "a/b.md"}     # dict, extra keys -> detail
        return ItemOutcome(n=0, written=True, detail={"x": 1})

    report = loop.execute(res.id, executor)
    assert all(o.written for o in report.outcomes)
    assert report.outcomes[1].detail["path"] == "a/b.md"
    assert report.outcomes[2].n == 3, "the loop owns item numbering, not the executor"


def test_unknown_approver_is_refused_not_ignored(loop, transport):
    res = loop.propose(items(1), approvers("a@x.test"))
    with pytest.raises(UnknownApprover):
        loop.record(res.id, "stranger@x.test", approve=[1])


def test_a_reply_from_a_stranger_escalates(loop, transport, escalations):
    res = loop.propose(items(1), approvers("a@x.test"))
    transport.receive(res.id, "someone-else@x.test", "approve 1")

    poll = loop.poll()
    assert poll.actionable == []
    assert any(e.kind is Escalation.UNKNOWN_SENDER for e in escalations)


def test_conditional_replies_escalate_instead_of_auto_recording(loop, transport, escalations):
    res = loop.propose(items(2), approvers("a@x.test"))
    transport.receive(res.id, "a@x.test", "approve 1 but can you reword it first")

    assert loop.record_from(res.id, loop.read(res.id)[0]) is None
    assert any(e.kind is Escalation.AMBIGUOUS_REPLY for e in escalations)
    assert loop.get(res.id).approver("a@x.test").replied_at is None


def test_record_rejects_contradictory_decisions(loop, transport):
    res = loop.propose(items(3), approvers("a@x.test"))
    with pytest.raises(ValueError):
        loop.record(res.id, "a@x.test", approve=[1], reject=[1])
    with pytest.raises(ValueError):
        loop.record(res.id, "a@x.test", approve=[9])


def test_receipt_goes_to_a_fresh_thread(loop, transport):
    res = loop.propose(items(1), approvers("a@x.test"), subject_key="acme")
    transport.receive(res.id, "a@x.test", "approve 1")
    loop.record_from(res.id, loop.read(res.id)[0])
    report = loop.execute(res.id, lambda item: {"written": True, "path": "x.md"})

    out = loop.receipt(res.id, report, to=["watcher@x.test"])
    assert out["thread_id"] != res.id, "a receipt must not reply into the approval thread"
    assert transport.sent[-1].to == ["watcher@x.test"]
    assert "x.md" in transport.sent[-1].body


def test_force_stops_waiting_and_runs_what_cleared(loop, transport):
    res = loop.propose(items(2), approvers("a@x.test", "b@x.test"))
    transport.receive(res.id, "a@x.test", "approve 1,2")
    loop.record_from(res.id, loop.read(res.id)[0])

    ran = []
    report = loop.execute(res.id, lambda item: ran.append(item.n), force=True)
    assert report.blocked == ""
    assert ran == [], "force does not invent consent — nothing had reached the threshold"
    assert loop.get(res.id).history[-1]["forced"] is True
