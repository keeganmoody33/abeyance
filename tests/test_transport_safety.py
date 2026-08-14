"""The two ways a detached loop can read its own voice as consent.

Both have the same signature in production: everything looks like it worked, and something
was written that nobody approved. Neither raises. Neither shows up in a log. They are the
reason the transport contract says "exclude by sender as well as by id".
"""
from __future__ import annotations

from conftest import approvers, items
from detached.adapters import MemoryTransport, address_of, strip_quoted


def test_our_own_message_is_never_read_as_a_reply(loop, transport):
    """The crash-between-send-and-save case. A message we sent but failed to record has an id
    the state does not know about, so an id-only filter lets it back in as inbound — and the
    apply tick then reads our own proposal text as somebody's approval."""
    res = loop.propose(items(3), approvers("a@x.test"))

    p = loop.get(res.id)
    p.last_outbound_id = ""        # simulate the state write that never landed
    p.sent_message_id = ""
    loop.save(p)

    assert loop.read(res.id) == [], "our own outbound must be excluded by sender too"
    assert loop.poll().actionable == []


def test_a_recorded_reply_does_not_resurface_forever(loop, transport):
    """Recording a decision sends nothing, so the outbound anchor does not move. Without a
    seen-set the same reply is 'new' on every hourly poll and the loop looks permanently
    actionable."""
    res = loop.propose(items(2), approvers("a@x.test", "b@x.test"))
    transport.receive(res.id, "a@x.test", "approve 1")

    loop.record_from(res.id, loop.read(res.id)[0])
    assert loop.read(res.id) == []
    assert loop.poll().actionable == []


def test_dismiss_consumes_a_reply_that_is_not_a_decision(loop, transport):
    res = loop.propose(items(1), approvers("a@x.test"))
    transport.receive(res.id, "a@x.test", "thanks, out of office until Monday")

    inbound = loop.read(res.id)
    assert inbound and not inbound[0].suggestion.confident
    loop.dismiss(res.id, [inbound[0].reply.message_id], note="OOO autoreply")
    assert loop.read(res.id) == []


def test_a_second_reply_from_the_same_person_is_seen(loop, transport):
    """A correction has to get through even though an earlier reply was already consumed."""
    res = loop.propose(items(3), approvers("a@x.test"))
    transport.receive(res.id, "a@x.test", "approve 1")
    loop.record_from(res.id, loop.read(res.id)[0])

    transport.receive(res.id, "a@x.test", "actually approve 1 and 2")
    inbound = loop.read(res.id)
    assert len(inbound) == 1
    assert inbound[0].suggestion.approve == [1, 2]


def test_poll_survives_one_unreadable_thread(loop, transport, monkeypatch):
    """One broken conversation must not blind the gate to every other open proposal."""
    good = loop.propose(items(1), approvers("a@x.test"))
    bad = loop.propose(items(1), approvers("b@x.test"))
    transport.receive(good.id, "a@x.test", "approve 1")

    original = transport.fetch_replies

    def flaky(thread_id, exclude_ids=(), after_epoch=0):
        if thread_id == bad.id:
            raise RuntimeError("thread unreachable")
        return original(thread_id, exclude_ids, after_epoch)

    monkeypatch.setattr(transport, "fetch_replies", flaky)
    poll = loop.poll()
    assert poll.actionable == [good.id]
    assert bad.id in poll.errors


# --------------------------------------------------------------------------- parsing helpers


def test_address_extraction_is_case_insensitive():
    """A From header's casing is not under our control, and a case-sensitive approver lookup
    turns a real approval into silence — then nudges someone who already answered."""
    assert address_of("Emily Ellin <Emily@Example.COM>") == "emily@example.com"
    assert address_of("PLAIN@EXAMPLE.COM") == "plain@example.com"


def test_quoted_history_is_trimmed():
    body = ("approve 1 and 3\n\n"
            "On Mon, 1 Jan 2026 at 09:00, Loop <loop@x> wrote:\n"
            "> 1. do the thing\n> 2. do the other thing\n")
    assert strip_quoted(body) == "approve 1 and 3"


def test_trimming_never_returns_nothing():
    """A reply that is entirely quoted text still has to yield something to look at, or the
    decision disappears into an empty string."""
    assert strip_quoted("> only quoted text") != ""


def test_memory_transport_ignores_other_threads():
    t = MemoryTransport()
    a = t.send(["x@y"], "s", "b")
    b = t.send(["x@y"], "s", "b")
    t.receive(a.thread_id, "x@y", "for a")
    t.receive(b.thread_id, "x@y", "for b")
    assert [r.text for r in t.fetch_replies(a.thread_id)] == ["for a"]
