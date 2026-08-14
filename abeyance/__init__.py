"""abeyance — human approval gates for agents that are not running.

Every other human-in-the-loop approval library models consent as an *interrupt*: an agent is
mid-run, it pauses on a tool call, a human answers, it resumes. That needs a live process
holding the wait, which is fine for an interactive session and impossible for unattended work.
The normal case there is that the approver is asleep and the machine that asked has exited.

Here the wait is not a paused process. It is a row in a store.

    propose  ──▶ send ──▶ [process exits]  ··· hours or days ···  poll ──▶ record ──▶ execute

A propose tick renders a batch of numbered items, sends one digest, persists, and terminates.
A separate, deterministic tick — no model calls, by construction — later notices a reply and
carries out exactly what cleared the approval math. Different hosts, different days, no
session, no held connection.

Quickstart:

    from abeyance import ApprovalLoop, Item, Approver, UNANIMOUS
    from abeyance.adapters import MemoryStore, MemoryTransport

    loop = ApprovalLoop("deploys", store=MemoryStore(), transport=MemoryTransport(),
                        policy=UNANIMOUS)
    loop.propose(
        items=[Item(n=1, summary="Drop the legacy sessions table")],
        approvers=[Approver("dba@example.com", role="dba"),
                   Approver("lead@example.com", role="lead")],
        subject_key="prod-migration-114")

    # ... later, in a scheduled tick that costs no tokens ...
    if loop.poll():
        for pid in loop.poll().actionable:
            for inbound in loop.read(pid):
                loop.record_from(pid, inbound)
            loop.execute(pid, executor=run_migration)
"""
from __future__ import annotations

from .cursor import Cursor, CursorRun, DueGate, DueVerdict, TriggerResult
from .errors import (AlreadyExecuted, ConfigurationError, CursorNotCommittable, AbeyanceError,
                     NoApproversError, PolicyError, ProposalNotFound, TransportError,
                     UnknownApprover)
from .interpret import DEFAULT_VOCABULARY, Suggestion, Vocabulary, interpret
from .loop import (ApprovalLoop, Executor, InboundReply, NudgeResult, PollResult, ProposeResult)
from .models import (Approver, Escalation, EscalationEvent, ExecutionReport, Item, ItemOutcome,
                     Proposal, Reply, Sent, Status, Verdict)
from .policy import ALL, ANY_ONE, SINGLE_APPROVER, UNANIMOUS, ApprovalPolicy, majority
from .ports import Clock, FrozenClock, Notifier, Renderer, Store, SystemClock, Transport
from .render import PlainTextRenderer, render_escalation
from .verdict import VerdictSummary, summarize, verdict_for, verdicts

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # loop
    "ApprovalLoop", "Executor", "InboundReply", "PollResult", "ProposeResult", "NudgeResult",
    # models
    "Item", "Approver", "Proposal", "Reply", "Sent", "Status", "Verdict",
    "ItemOutcome", "ExecutionReport", "Escalation", "EscalationEvent",
    # policy
    "ApprovalPolicy", "UNANIMOUS", "ANY_ONE", "SINGLE_APPROVER", "majority", "ALL",
    # verdict
    "verdicts", "verdict_for", "summarize", "VerdictSummary",
    # interpret
    "interpret", "Suggestion", "Vocabulary", "DEFAULT_VOCABULARY",
    # cursor
    "DueGate", "Cursor", "CursorRun", "DueVerdict", "TriggerResult",
    # ports
    "Store", "Transport", "Notifier", "Clock", "Renderer", "SystemClock", "FrozenClock",
    # render
    "PlainTextRenderer", "render_escalation",
    # errors
    "AbeyanceError", "ConfigurationError", "NoApproversError", "PolicyError",
    "ProposalNotFound", "UnknownApprover", "AlreadyExecuted", "TransportError",
    "CursorNotCommittable",
]
