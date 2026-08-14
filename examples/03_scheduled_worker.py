#!/usr/bin/env python3
"""The production shape: two cron entries, many subjects, a guarded watermark.

This is what the library is actually for. Two ticks in a container, on a schedule:

    30 7 * * 1-5   python worker.py propose    # expensive; only for subjects that are DUE
    0  * * * *     python worker.py apply      # cheap gate; spends nothing on a quiet hour

The two ideas worth stealing even if you never install this:

  **The floor sweep is not optional.** Trigger-only scheduling means a subject whose source
  breaks — revoked token, renamed channel, an integration quietly returning empty — is never
  due again and looks exactly like a healthy quiet subject. The floor turns that silence into
  a run.

  **The watermark is guarded, not documented.** `CursorRun` refuses to advance until the
  durable record AND the ask have both landed. A cursor that moves after a failed send skips
  that window permanently and silently: no error, no retry, no gap anywhere to notice.

    python examples/03_scheduled_worker.py
"""
from __future__ import annotations

import sys

from detached import (ApprovalLoop, ApprovalPolicy, Approver, CursorNotCommittable, DueGate,
                      FrozenClock, Item, TriggerResult)
from detached.adapters import ConsoleNotifier, MemoryStore, MemoryTransport

STORE = MemoryStore()
TRANSPORT = MemoryTransport(address="worker@example.test")
CLOCK = FrozenClock()

SUBJECTS = ["acme", "globex", "initech"]

# Pretend upstream: the last event id we have seen per subject, and what the source says now.
UPSTREAM = {"acme": "evt-9", "globex": None, "initech": "evt-3"}


def build_loop() -> ApprovalLoop:
    return ApprovalLoop(
        "review", store=STORE, transport=TRANSPORT, clock=CLOCK,
        notifier=ConsoleNotifier(),
        policy=ApprovalPolicy(expire_after_days=7, nudge_after_hours=(24.0, 72.0),
                              nudge_cap=2, max_turns=3))


def build_gate() -> DueGate:
    gate = DueGate(STORE, name="review", floor_days=14, clock=CLOCK)

    def new_event(subject: str, cursor) -> TriggerResult:
        """Cheap: one lookup. Runs on every tick for every subject."""
        latest = UPSTREAM.get(subject)
        if latest and latest != cursor.marks.get("last_event"):
            return TriggerResult(fired=True, reason=f"new event {latest}",
                                 marks={"last_event": latest})
        return TriggerResult(fired=False, reason="no new events")

    def comment_volume(subject: str, cursor) -> TriggerResult:
        """Expensive: a paginated crawl. Deep passes only, and skipped entirely once
        something cheaper has already made the subject due."""
        return TriggerResult(fired=False, reason="(would crawl comments here)")

    def has_approvers(subject: str, cursor):
        return None if subject in APPROVERS else "no approver configured for this subject"

    return (gate
            .register("new-event", new_event)
            .register("comment-volume", comment_volume, expensive=True)
            .precondition(has_approvers))


APPROVERS = {
    "acme": [Approver("owner@acme.test", role="owner", channel_id="U1")],
    "globex": [Approver("owner@globex.test", role="owner", channel_id="U2")],
    # "initech" is deliberately missing, to show a BLOCKED subject being reported rather than
    # silently skipped — blocked and quiet look identical in a log and are very different
    # problems.
}


# --------------------------------------------------------------------------- propose tick


def propose_tick(deep: bool = True) -> None:
    gate, loop = build_gate(), build_loop()

    for verdict in gate.evaluate_all(SUBJECTS, deep=deep):
        if verdict.blocked:
            print(f"[{verdict.subject}] BLOCKED — {verdict.blocked}")
            continue
        if not verdict.due:
            print(f"[{verdict.subject}] not due — {'; '.join(verdict.reasons)}")
            continue

        print(f"[{verdict.subject}] DUE via {verdict.trigger} — {'; '.join(verdict.reasons)}")

        with gate.begin(verdict.subject) as run:
            # Declare the checklist where the run starts, so it is visible in the same screen
            # as the work rather than buried in a comment near the commit.
            run.require("ledger", "the append-only record of what we read")
            run.require("digest", "the approval request itself")

            findings = gather(verdict.subject)
            write_ledger(verdict.subject, findings)
            run.satisfied("ledger", ref=f"{verdict.subject}-ledger")

            result = loop.propose(
                items=[Item(n=i, summary=f, payload={"finding": f})
                       for i, f in enumerate(findings, start=1)],
                approvers=APPROVERS[verdict.subject],
                subject_key=verdict.subject)
            if not result.sent:
                # Quiet cycle: nothing to ask about. The window was still fully read, so the
                # watermark may legitimately advance — but say so explicitly.
                run.satisfied("digest", ref="nothing to ask")
            else:
                run.satisfied("digest", ref=result.id)

            run.advance(marks=verdict.marks)
            print(f"    watermark advanced to {verdict.marks or '(unchanged)'}")


def gather(subject: str) -> list:
    return [f"{subject}: finding A", f"{subject}: finding B"]


def write_ledger(subject: str, findings: list) -> None:
    pass  # your append-only record


# --------------------------------------------------------------------------- apply tick


def apply_tick() -> int:
    """The hourly half. Returns the number of proposals it actually spent anything on."""
    loop = build_loop()
    loop.nudge()

    poll = loop.poll()          # deterministic; no model, no interpretation
    if not poll.actionable:
        print(f"apply: checked {poll.checked}, nothing to do — exiting free")
        return 0

    for pid in poll.actionable:
        for inbound in loop.read(pid):
            loop.record_from(pid, inbound)
        report = loop.execute(pid, executor=lambda item: {"written": True})
        print(f"apply: {pid} -> {report.to_doc()['verdicts']}")
    return len(poll.actionable)


# --------------------------------------------------------------------------- guard demo


def show_the_guard() -> None:
    """What happens when the send fails: the watermark refuses to move, so the window stays
    unread — which is the recoverable state."""
    gate = build_gate()
    run = gate.begin("acme")
    run.require("ledger")
    run.require("digest")
    run.satisfied("ledger", "ok")
    run.failed("digest", reason="SMTP 550 mailbox unavailable")
    try:
        run.advance(marks={"last_event": "evt-999"})
    except CursorNotCommittable as e:
        print(f"\nrefused, correctly:\n  {e}")
    print(f"  cursor still at: {gate.cursor('acme').marks.get('last_event')}")


if __name__ == "__main__":
    print("=== propose tick ===")
    propose_tick()

    print("\n=== propose tick again, immediately ===")
    propose_tick()

    print("\n=== apply tick, nobody has replied ===")
    apply_tick()

    print("\n=== an owner replies ===")
    pid = [p.id for p in build_loop().pending() if p.subject_key == "acme"][0]
    TRANSPORT.receive(pid, "owner@acme.test", "approve 1, skip 2")
    apply_tick()

    print("\n=== 15 days later: the floor sweep catches a subject whose trigger went dead ===")
    CLOCK.advance(days=15)
    propose_tick(deep=False)

    show_the_guard()
    sys.exit(0)
