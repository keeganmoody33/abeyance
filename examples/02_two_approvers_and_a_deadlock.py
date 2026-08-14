#!/usr/bin/env python3
"""Two people who have to agree, arriving on different days, disagreeing about one item.

This is the case no interrupt-style approval library models, because an interrupt has exactly
one waiter. Three things happen here that are only possible when the wait is a record rather
than a paused process:

  * the second approver answers three days after the first, on a different tick
  * one item they disagree on is **left alone** rather than decided by majority
  * everyone hears what actually changed, in a fresh thread, including the item nobody wrote

The deadlock is the point. Two people with equal standing said opposite things about a real
change; a system that resolves that on its own has laundered a disagreement into a write.

    python examples/02_two_approvers_and_a_deadlock.py
"""
from __future__ import annotations

from detached import (ApprovalLoop, ApprovalPolicy, Approver, EscalationEvent, Item,
                      render_escalation)
from detached.adapters import MemoryStore, MemoryTransport, RecordingNotifier

STORE = MemoryStore()
TRANSPORT = MemoryTransport(address="gtm@example.test")
NOTIFIER = RecordingNotifier()
ESCALATED: list = []

# Unanimity, a week to answer, two nudges, and a hard stop at three turns. Every one of these
# is a decision about how much patience the loop has before it gives up and says so.
POLICY = ApprovalPolicy(
    threshold="all",
    veto=True,
    expire_after_days=7,
    nudge_after_hours=(24.0, 72.0),
    nudge_cap=2,
    max_turns=3,
    roles_required=("csm", "engineer"),   # refuses to send if one of them is missing
)


def build_loop() -> ApprovalLoop:
    return ApprovalLoop("client-feedback", store=STORE, transport=TRANSPORT, policy=POLICY,
                        notifier=NOTIFIER, on_escalate=ESCALATED.append)


def apply_change(item: Item) -> dict:
    kind = item.payload.get("kind")
    if kind == "persona":
        return {"written": True, "detail": {"file": "personas/district-leader.md"}}
    if kind == "segment":
        return {"written": True, "detail": {"file": "segments/target-states.md"}}
    return {"written": True, "detail": {"file": item.payload.get("path", "?")}}


def main() -> None:
    loop = build_loop()

    proposal = loop.propose(
        items=[
            Item(n=1, summary="Add Directors of Curriculum & Instruction to the target personas",
                 payload={"kind": "persona"},
                 detail=('Adam Javer, 2026-08-07, on the campaign doc:\n'
                         '"superintendent connects have been limited — layer in Directors of\n'
                         'Curriculum & Instruction at district level"')),
            Item(n=2, summary="Widen the account list beyond California",
                 payload={"kind": "segment"},
                 detail=('Adam Javer, 2026-08-07, on the lead list:\n'
                         '"all accounts in the current list are California — spread the\n'
                         'outreach across a wider sampling of states"')),
            Item(n=3, summary='Ban the word "worth" in subject lines (enforced by the linter)',
                 payload={"kind": "lint-rule", "path": "messaging/lint-rules.yaml"},
                 detail="Requested directly on the copy. Would block builds that use it."),
        ],
        approvers=[
            Approver("csm@example.test", role="csm", display_name="Emily", channel_id="U0A6"),
            Approver("eng@example.test", role="engineer", display_name="Effa", channel_id="U0B4"),
        ],
        subject_key="acme",
        track="behaviour-change",
    )
    print(f"=== proposed to {proposal.to} ===\n{proposal.body}")

    # ---- day 1: the CSM answers -------------------------------------------------
    TRANSPORT.receive(proposal.id, "csm@example.test", "approve 1 and 2, and yes to 3")
    for inbound in loop.read(proposal.id):
        loop.record_from(proposal.id, inbound)

    report = loop.execute(proposal.id, apply_change)
    print(f"\n[day 1] blocked: {report.blocked!r} — waiting on "
          f"{loop.get(proposal.id).waiting_on}")

    # ---- day 4: the engineer answers, and disagrees about the lint rule ---------
    TRANSPORT.receive(proposal.id, "eng@example.test",
                      "approve 1 and 2. no on 3 — that rule would break three live campaigns.")
    for inbound in loop.read(proposal.id):
        loop.record_from(proposal.id, inbound)

    report = loop.execute(proposal.id, apply_change)
    print(f"\n[day 4] verdicts: {report.to_doc()['verdicts']}")
    print(f"        written:  {[o.n for o in report.written]}")
    print(f"        status:   {loop.get(proposal.id).status.value}")

    # ---- the receipt: a fresh thread, to a wider audience -----------------------
    out = loop.receipt(proposal.id, report,
                       to=["csm@example.test", "eng@example.test", "lead@example.test"],
                       dry_run=True)
    print(f"\n=== receipt (fresh thread) -> {out['to']} ===\n{out['body']}")

    # ---- the exception path: what a human actually has to look at ---------------
    print("=== escalations ===")
    print(render_escalation(ESCALATED))


if __name__ == "__main__":
    main()
