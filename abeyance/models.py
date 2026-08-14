"""Core value types. Pure data — no I/O, no vendor concepts, no model calls.

Everything here is JSON round-trippable, because a proposal has to survive the thing that
makes this library different from every other human-in-the-loop approval tool: **no process
is alive while it waits.** The propose run exits. The state is the only continuity between
it and an apply run that may happen days later, on a different machine.

That constraint is why `Item.payload` is opaque. The library never learns what a proposal
*does* — it carries the payload through the wait and hands it back to a caller-supplied
executor once the approval math says it cleared. Keeping the domain out of here is what
lets one state machine serve a copy-edit approval and a production-database migration.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Status(str, Enum):
    """Where a proposal is in its life.

    A str-Enum so it serializes as itself and a stored doc stays human-readable.
    """

    AWAITING_REPLY = "awaiting_reply"
    """Sent; nobody has answered yet."""

    PARTIALLY_APPROVED = "partially_approved"
    """At least one approver answered, at least one has not. Not a terminal state."""

    CLARIFYING = "clarifying"
    """We asked a follow-up question and are waiting on the answer. Counts against turns."""

    EXECUTED = "executed"
    """Terminal. The approved items ran. Re-execution is refused without `force`."""

    DEADLOCKED = "deadlocked"
    """Terminal-ish. Approvers disagreed on at least one item. Nothing was written for the
    disputed items; a human owner has to break the tie."""

    STALLED = "stalled"
    """Terminal. The conversation hit the turn cap without resolving. Escalated."""

    EXPIRED = "expired"
    """Terminal. Nobody answered in time. Nothing was written."""


OPEN_STATUSES = (Status.AWAITING_REPLY, Status.PARTIALLY_APPROVED, Status.CLARIFYING)
"""Statuses that can still absorb a reply. Everything else is settled."""


class Verdict(str, Enum):
    """The per-item outcome of the approval math. See `verdict.py` for how each is reached."""

    APPROVED = "approved"
    REJECTED = "rejected"
    DEADLOCKED = "deadlocked"
    WAITING = "waiting"
    UNREACHABLE = "unreachable"
    """Enough approvers have declined or gone silent that the threshold can no longer be met.
    Distinct from REJECTED: nobody vetoed it, it just cannot pass. Reported separately so a
    caller can re-propose rather than treat it as a decision."""


class Escalation(str, Enum):
    """Why a human owner is being pulled in. The owner is *off* the approval path and *on*
    the exception path — they are not a required approver, they are who hears about the
    cases the machine refuses to resolve on its own."""

    DEADLOCK = "deadlock"
    STALL = "stall"
    EXPIRY = "expiry"
    EXECUTION_REFUSED = "execution_refused"
    UNKNOWN_SENDER = "unknown_sender"
    AMBIGUOUS_REPLY = "ambiguous_reply"


# --------------------------------------------------------------------------- items


@dataclass
class Item:
    """One numbered thing a human says yes or no to.

    `n` is the number the human types in a reply, so it is stable for the life of the
    proposal and 1-based — people do not count from zero in prose.
    """

    n: int
    summary: str
    """One line. This is what the approver actually reads before deciding."""

    payload: Dict[str, Any] = field(default_factory=dict)
    """Opaque to this library. Whatever your executor needs to carry out the item."""

    detail: str = ""
    """Optional longer rendering — evidence, a diff, the proposed text."""

    advisory: bool = False
    """Approving it changes nothing; it is a recommendation. Kept in the digest because
    hiding advisory items is how a reviewer loses the thread of what the system considered,
    but the executor is never called for one."""

    def to_doc(self) -> Dict[str, Any]:
        return {"n": self.n, "summary": self.summary, "payload": self.payload,
                "detail": self.detail, "advisory": self.advisory}

    @classmethod
    def from_doc(cls, d: Dict[str, Any]) -> "Item":
        return cls(n=int(d["n"]), summary=d.get("summary", ""), payload=d.get("payload") or {},
                   detail=d.get("detail", ""), advisory=bool(d.get("advisory")))


# --------------------------------------------------------------------------- approvers


@dataclass
class Approver:
    """One person whose answer counts, and their running ledger.

    `address` is the identity the transport authenticates a reply by — an email address for
    the mail transports. It is lowercased on construction because a reply header's casing is
    not under our control and a case-sensitive lookup silently turns a real approval into
    `unknown_sender`.
    """

    address: str
    role: str = ""
    """Free-form label — "csm", "owner", "sre". Renderers and policies may key off it."""

    display_name: str = ""
    channel_id: str = ""
    """Optional out-of-band handle for nudges (a Slack user id, a phone number)."""

    approved: List[int] = field(default_factory=list)
    rejected: List[int] = field(default_factory=list)
    replied_at: Optional[str] = None
    raw: Optional[str] = None
    """The reply verbatim. Kept because the audit question is never "what did the parser
    decide", it is "what did the person actually write"."""

    nudges: int = 0

    def __post_init__(self) -> None:
        self.address = (self.address or "").strip().lower()

    @property
    def has_replied(self) -> bool:
        return self.replied_at is not None

    def to_doc(self) -> Dict[str, Any]:
        return {"address": self.address, "role": self.role, "display_name": self.display_name,
                "channel_id": self.channel_id, "approved": sorted(self.approved),
                "rejected": sorted(self.rejected), "replied_at": self.replied_at,
                "raw": self.raw, "nudges": self.nudges}

    @classmethod
    def from_doc(cls, d: Dict[str, Any]) -> "Approver":
        return cls(address=d["address"], role=d.get("role", ""),
                   display_name=d.get("display_name", ""), channel_id=d.get("channel_id", ""),
                   approved=list(d.get("approved") or []), rejected=list(d.get("rejected") or []),
                   replied_at=d.get("replied_at"), raw=d.get("raw"), nudges=int(d.get("nudges") or 0))


# --------------------------------------------------------------------------- transport I/O


@dataclass
class Reply:
    """One inbound message, already attributed to a sender by the transport."""

    message_id: str
    sender: str
    text: str
    epoch: int = 0

    def __post_init__(self) -> None:
        self.sender = (self.sender or "").strip().lower()


@dataclass
class Sent:
    """What a transport returns after a successful send. `thread_id` is the correlation key
    for the whole conversation and becomes the proposal's id."""

    message_id: str
    thread_id: str
    header_message_id: str = ""
    """The RFC-5322 Message-ID, needed to thread a later reply. Empty for transports with no
    such concept."""


# --------------------------------------------------------------------------- proposal


@dataclass
class Proposal:
    """A batch of numbered items, a set of approvers, and everything needed to resume.

    Two timestamps, and the difference between them is load-bearing:

      `sent_epoch`           when we asked. Never moves.
      `last_activity_epoch`  when anything last happened on this thread. Expiry runs off
                             THIS one, so a conversation in progress cannot time out
                             mid-sentence. Anchoring expiry on send time is the bug that
                             kills live negotiations at the deadline.
    """

    id: str
    subject_key: str = ""
    """What the proposal is *about* in your domain — a client slug, a repo, a tenant. Used to
    correlate a proposal with its cursor, and to group in listings."""

    track: str = ""
    """Optional lane within a subject. Two tracks let one run ask different audiences for
    different levels of consent — e.g. an advisory record vs. a change to machine behaviour."""

    subject: str = ""
    """The rendered subject line, kept so a threaded reply can reuse it."""

    items: List[Item] = field(default_factory=list)
    approvers: Dict[str, Approver] = field(default_factory=dict)
    status: Status = Status.AWAITING_REPLY

    sent_epoch: int = 0
    last_activity_epoch: int = 0
    turns: int = 1

    sent_message_id: str = ""
    last_outbound_id: str = ""
    """Anchors "everything newer than our last outbound". Updated on every send, including
    clarification turns, or a multi-turn thread re-reads its own history as inbound."""

    last_outbound_header_id: str = ""
    seen_reply_ids: List[str] = field(default_factory=list)
    """Inbound message ids already ingested. Belt to `last_outbound_id`'s braces: recording a
    decision without sending anything leaves the outbound anchor unmoved, so without this the
    same reply would resurface as new on every poll."""

    context: Dict[str, Any] = field(default_factory=dict)
    """Caller's own metadata, carried through the wait untouched."""

    history: List[Dict[str, Any]] = field(default_factory=list)
    """Append-only event log for the audit trail."""

    # ----------------------------------------------------------------- helpers

    @property
    def actionable_items(self) -> List[Item]:
        """Items an executor could actually run. Advisory items are excluded here rather than
        at execute time so the approval math and the digest agree on the denominator."""
        return [i for i in self.items if not i.advisory]

    @property
    def item_numbers(self) -> set:
        return {i.n for i in self.items}

    @property
    def max_n(self) -> int:
        return max((i.n for i in self.items), default=0)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def waiting_on(self) -> List[str]:
        return sorted(a.address for a in self.approvers.values() if not a.has_replied)

    def item(self, n: int) -> Optional[Item]:
        return next((i for i in self.items if i.n == n), None)

    def approver(self, address: str) -> Optional[Approver]:
        return self.approvers.get((address or "").strip().lower())

    def log(self, event: str, **fields: Any) -> None:
        self.history.append({"event": event, "at": int(time.time()), **fields})

    def touch(self, now: Optional[int] = None) -> None:
        self.last_activity_epoch = int(now if now is not None else time.time())

    # ----------------------------------------------------------------- serialization

    def to_doc(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "subject_key": self.subject_key,
            "track": self.track,
            "subject": self.subject,
            "items": [i.to_doc() for i in self.items],
            "approvers": {k: v.to_doc() for k, v in self.approvers.items()},
            "status": self.status.value,
            "sent_epoch": self.sent_epoch,
            "last_activity_epoch": self.last_activity_epoch,
            "turns": self.turns,
            "sent_message_id": self.sent_message_id,
            "last_outbound_id": self.last_outbound_id,
            "last_outbound_header_id": self.last_outbound_header_id,
            "seen_reply_ids": list(self.seen_reply_ids),
            "context": self.context,
            "history": self.history,
        }

    @classmethod
    def from_doc(cls, d: Dict[str, Any]) -> "Proposal":
        return cls(
            id=d["id"],
            subject_key=d.get("subject_key", ""),
            track=d.get("track", ""),
            subject=d.get("subject", ""),
            items=[Item.from_doc(x) for x in d.get("items") or []],
            approvers={k: Approver.from_doc(v) for k, v in (d.get("approvers") or {}).items()},
            status=Status(d.get("status", Status.AWAITING_REPLY.value)),
            sent_epoch=int(d.get("sent_epoch") or 0),
            last_activity_epoch=int(d.get("last_activity_epoch") or 0),
            turns=int(d.get("turns") or 1),
            sent_message_id=d.get("sent_message_id", ""),
            last_outbound_id=d.get("last_outbound_id", ""),
            last_outbound_header_id=d.get("last_outbound_header_id", ""),
            seen_reply_ids=list(d.get("seen_reply_ids") or []),
            context=d.get("context") or {},
            history=list(d.get("history") or []),
        )


# --------------------------------------------------------------------------- results


@dataclass
class ItemOutcome:
    """What an executor did with one approved item.

    Exactly one of `written` / `skipped` / `error` should be meaningful. `error` is not an
    exception: an executor that refuses an item (the document moved under it, an anchor
    vanished) is behaving correctly, and the proposal should record the refusal and carry on
    with the rest rather than abort the batch.
    """

    n: int
    written: bool = False
    skipped: str = ""
    error: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.written and not self.error

    def to_doc(self) -> Dict[str, Any]:
        return {"n": self.n, "written": self.written, "skipped": self.skipped,
                "error": self.error, "detail": self.detail}


@dataclass
class ExecutionReport:
    proposal_id: str
    dry_run: bool
    verdicts: Dict[int, Verdict]
    outcomes: List[ItemOutcome] = field(default_factory=list)
    blocked: str = ""
    """Non-empty when nothing ran — e.g. approvers still outstanding. A blocked report is not
    a failure; it is the normal answer to "is it time yet?"."""

    @property
    def written(self) -> List[ItemOutcome]:
        return [o for o in self.outcomes if o.written]

    @property
    def refused(self) -> List[ItemOutcome]:
        return [o for o in self.outcomes if o.error]

    def to_doc(self) -> Dict[str, Any]:
        return {"proposal_id": self.proposal_id, "dry_run": self.dry_run,
                "blocked": self.blocked,
                "verdicts": {str(k): v.value for k, v in self.verdicts.items()},
                "outcomes": [o.to_doc() for o in self.outcomes]}


@dataclass
class EscalationEvent:
    kind: Escalation
    proposal_id: str
    subject_key: str = ""
    detail: str = ""
    items: List[int] = field(default_factory=list)

    def to_doc(self) -> Dict[str, Any]:
        return {"kind": self.kind.value, "proposal_id": self.proposal_id,
                "subject_key": self.subject_key, "detail": self.detail, "items": self.items}
