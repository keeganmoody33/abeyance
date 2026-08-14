#!/usr/bin/env python3
"""The smallest useful loop: one approver, one batch, run to completion.

Read this one first. It is the whole library in forty lines, and it is deliberately written as
TWO functions that share nothing but the store — because that is the claim. `propose_tick()`
returns and its process could exit; `apply_tick()` starts from nothing but a store handle and
finishes the job.

    python examples/01_single_approver.py
"""
from __future__ import annotations

from detached import ApprovalLoop, Approver, Item, SINGLE_APPROVER
from detached.adapters import MemoryStore, MemoryTransport

STORE = MemoryStore()
TRANSPORT = MemoryTransport(address="ops@example.test")


def build_loop() -> ApprovalLoop:
    """Both ticks build the loop the same way. Nothing is carried over in memory."""
    return ApprovalLoop("cleanup", store=STORE, transport=TRANSPORT, policy=SINGLE_APPROVER)


# --------------------------------------------------------------------------- tick 1


def propose_tick() -> str:
    loop = build_loop()
    result = loop.propose(
        items=[
            Item(n=1, summary="Delete 41 orphaned S3 objects in staging",
                 payload={"bucket": "staging-artifacts", "prefix": "orphans/"},
                 detail="Last touched 2024-11. Nothing references them in the manifest."),
            Item(n=2, summary="Drop the `legacy_sessions` table",
                 payload={"table": "legacy_sessions"},
                 detail="0 reads in 90 days. Backed up to the nightly snapshot."),
            Item(n=3, summary="Rotate the reporting service account key",
                 payload={"account": "svc-reporting"}),
        ],
        approvers=[Approver("sre@example.test", role="sre", channel_id="U123")],
        subject_key="staging-cleanup-2026-08",
    )
    print(f"--- sent to {result.to} ---\n{result.body}")
    return result.id


# --------------------------------------------------------------------------- tick 2


def apply_tick() -> None:
    """A different process, hours later. It knows only the store."""
    loop = build_loop()

    # THE CHEAP GATE. No model, no interpretation — just "is there anything here".
    poll = loop.poll()
    if not poll:
        print("nothing waiting; exiting without spending anything")
        return

    for pid in poll.actionable:
        for inbound in loop.read(pid):
            print(f"reply from {inbound.sender}: {inbound.text!r}")
            print(f"  parser suggests {inbound.suggestion.to_doc()}")
            # `require_confident=True` refuses anything hedged or conditional and escalates
            # instead — the judgment call stays with a human for the replies that need one.
            loop.record_from(pid, inbound)

        report = loop.execute(pid, executor=do_the_thing)
        if report.blocked:
            print(f"still blocked: {report.blocked}")
            continue
        print(f"\nwrote {[o.n for o in report.written]}, "
              f"skipped {[o.n for o in report.outcomes if o.skipped]}")
        loop.confirm(pid, "Done — 1 and 3 applied, 2 left alone as you asked.")


def do_the_thing(item: Item) -> dict:
    """Your side of the contract. The library never learns what an item means."""
    print(f"  executing {item.n}: {item.summary}")
    return {"written": True, "detail": {"payload": item.payload}}


if __name__ == "__main__":
    pid = propose_tick()

    print("\n=== nothing is running. the approver replies whenever they get to it ===\n")
    TRANSPORT.receive(pid, "sre@example.test", "approve 1 and 3, skip 2 for now")

    apply_tick()
