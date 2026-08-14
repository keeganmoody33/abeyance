#!/usr/bin/env python3
"""A loop wired for the `abeyance` CLI, so the shell path is demonstrable end to end.

    export PYTHONPATH=examples
    python -m abeyance.cli --app cli_app:loop pending
    python -m abeyance.cli --app cli_app:loop inject --id <id> --from a@x.test --text "approve 1"
    python -m abeyance.cli --app cli_app:loop poll
    python -m abeyance.cli --app cli_app:loop read   --id <id>
    python -m abeyance.cli --app cli_app:loop record --id <id> --from a@x.test --approve 1,3
    python -m abeyance.cli --app cli_app:loop apply  --id <id> --executor cli_app:execute

State goes to `.abeyance-state/` so it survives between commands — which is the point. Swap
`JSONFileStore` for `PostgresStore` and the same commands work from any host.
"""
from __future__ import annotations

import os

from abeyance import ApprovalLoop, Approver, Item, SINGLE_APPROVER
from abeyance.adapters import JSONFileStore, MemoryTransport

STATE = os.environ.get("ABEYANCE_STATE", ".abeyance-state")

# MemoryTransport keeps this runnable with no credentials — but it is per-process, so replies
# injected in one command are not visible to the next. For a real shell walkthrough, point
# this at GmailTransport or SMTPIMAPTransport.
loop = ApprovalLoop("demo", store=JSONFileStore(STATE), transport=MemoryTransport(),
                    policy=SINGLE_APPROVER)


def execute(item: Item) -> dict:
    print(f"  executing {item.n}: {item.summary}")
    return {"written": True, "detail": {"payload": item.payload}}


def seed() -> str:
    result = loop.propose(
        items=[Item(n=i, summary=f"demo item {i}", payload={"i": i}) for i in (1, 2, 3)],
        approvers=[Approver("a@x.test", role="owner", channel_id="U1")],
        subject_key="demo")
    return result.id


if __name__ == "__main__":
    print(seed())
