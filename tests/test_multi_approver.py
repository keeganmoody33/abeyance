"""Two people, arriving at different times, sometimes disagreeing.

This is the part no interrupt-style approval library models, because an interrupt has one
waiter. Here the approvers are independent, asynchronous, and identified by who sent the
reply.
"""
from __future__ import annotations

from conftest import approvers, items
from abeyance import ANY_ONE, Escalation, Status, UNANIMOUS, Verdict


def test_both_must_approve_before_anything_runs(loop, transport):
    res = loop.propose(items(2), approvers("csm@x.test", "eng@x.test"))

    transport.receive(res.id, "csm@x.test", "approve 1 and 2")
    loop.record_from(res.id, loop.read(res.id)[0])
    ran = []
    assert loop.execute(res.id, lambda i: ran.append(i.n)).blocked
    assert ran == []

    transport.receive(res.id, "eng@x.test", "approve 1")
    loop.record_from(res.id, loop.read(res.id)[0])
    report = loop.execute(res.id, lambda i: ran.append(i.n))
    assert ran == [1], "only the item both of them approved"
    assert report.verdicts[2] is Verdict.UNREACHABLE, \
        "eng answered and passed over 2 — no veto, but no unanimity either"


def test_disagreement_deadlocks_writes_nothing_and_escalates(loop, transport, escalations):
    res = loop.propose(items(2), approvers("csm@x.test", "eng@x.test"))
    transport.receive(res.id, "csm@x.test", "approve 1 and 2")
    loop.record_from(res.id, loop.read(res.id)[0])
    transport.receive(res.id, "eng@x.test", "approve 2, skip 1")
    loop.record_from(res.id, loop.read(res.id)[0])

    ran = []
    report = loop.execute(res.id, lambda i: ran.append(i.n))
    assert ran == [2], "only the item they agreed on"
    assert report.verdicts[1] is Verdict.DEADLOCKED
    assert loop.get(res.id).status is Status.DEADLOCKED

    ev = [e for e in escalations if e.kind is Escalation.DEADLOCK]
    assert ev and ev[0].items == [1]


def test_each_reply_is_attributed_to_its_own_sender(loop, transport):
    """Two approvers on one thread are indistinguishable if you read "the reply". They are
    told apart by the From address, which is why the transport must attribute."""
    res = loop.propose(items(3), approvers("csm@x.test", "eng@x.test"))
    transport.receive(res.id, "csm@x.test", "approve 1,2")
    transport.receive(res.id, "eng@x.test", "approve 2,3")

    inbound = loop.read(res.id)
    assert {i.sender for i in inbound} == {"csm@x.test", "eng@x.test"}
    for i in inbound:
        loop.record_from(res.id, i)

    p = loop.get(res.id)
    assert p.approver("csm@x.test").approved == [1, 2]
    assert p.approver("eng@x.test").approved == [2, 3]
    assert loop.summary(p).approved == [2]


def test_the_second_approver_can_arrive_much_later(loop, transport, clock):
    """The whole point, in miniature: nothing is running between the two replies."""
    res = loop.propose(items(1), approvers("csm@x.test", "eng@x.test"))
    transport.receive(res.id, "csm@x.test", "approve 1", epoch=clock.now())
    loop.record_from(res.id, loop.read(res.id)[0])

    clock.advance(days=3)
    transport.receive(res.id, "eng@x.test", "approve 1", epoch=clock.now())
    loop.record_from(res.id, loop.read(res.id)[0])

    ran = []
    loop.execute(res.id, lambda i: ran.append(i.n))
    assert ran == [1]


def test_any_one_policy_runs_on_the_first_yes(make_loop, transport):
    loop = make_loop(policy=ANY_ONE)
    res = loop.propose(items(1), approvers("a@x.test", "b@x.test"))
    transport.receive(res.id, "a@x.test", "approve 1")
    loop.record_from(res.id, loop.read(res.id)[0])

    ran = []
    assert not loop.execute(res.id, lambda i: ran.append(i.n)).blocked
    assert ran == [1]


def test_recording_twice_for_one_approver_replaces_not_appends(loop, transport):
    """An approver who corrects themselves should end up with one ledger, not the union of
    two. Also makes the whole ingest step safe to re-run."""
    res = loop.propose(items(3), approvers("a@x.test"))
    loop.record(res.id, "a@x.test", approve=[1, 2])
    loop.record(res.id, "a@x.test", approve=[3])
    assert loop.get(res.id).approver("a@x.test").approved == [3]


def test_roles_required_catches_a_one_person_two_yes_policy(make_loop):
    """The misconfiguration that produces a proposal nobody can ever satisfy: a policy that
    wants two roles handed a single approver. Caught before anything is sent."""
    import pytest

    from abeyance import ApprovalPolicy, ConfigurationError
    loop = make_loop(policy=ApprovalPolicy(roles_required=("csm", "eng")))
    with pytest.raises(ConfigurationError):
        loop.propose(items(1), approvers("only@x.test"))
