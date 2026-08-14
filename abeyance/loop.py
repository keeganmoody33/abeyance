"""The state machine. Propose, wait with nothing running, resume on a reply, execute.

The shape, and the one design decision everything else follows from:

    propose tick  ──▶  send  ──▶  [ PROCESS EXITS ]
                                        │
                     hours or days, no process, no held connection, no open session
                                        │
    apply tick    ──▶  cheap gate ──▶ replies? ──▶ record ──▶ execute ──▶ receipt

The design decision everything else follows from: **consent is separate from execution.**

Durable agent runtimes already survive process death — LangGraph resumes an `interrupt()` from
a checkpointer, Temporal takes a signal against durable workflow state — and where the work
lives inside one of those, its own approval primitive is the right tool. What this class owns
is the case where the runtime that asked should not own the wait, or where there is no runtime
at all: the consent record is a first-class object with its own lifecycle, and any worker that
can reach the store can carry it forward.

That is why every piece of continuity here is explicit rather than implied by a live frame:
the proposal is durable, replies are attributed to a sender, execute is idempotent, and the
gate that decides whether to spend anything (`poll`) is deterministic and free of model calls
by construction — an hourly tick over a hundred open proposals costs a hundred API reads and
zero tokens.

Concurrency scope: `record()` is idempotent per approver and `execute()` refuses an
already-executed proposal, but store writes are last-write-wins and two apply workers can both
pass the status check before either executes. Run one apply worker at a time, or make the
executor idempotent. See `docs/ARCHITECTURE.md`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Union

from .errors import (AlreadyExecuted, ConfigurationError, NoApproversError, PolicyError,
                     ProposalNotFound, TransportError, UnknownApprover)
from .interpret import Suggestion, Vocabulary, interpret
from .models import (Approver, Escalation, EscalationEvent, ExecutionReport, Item, ItemOutcome,
                     OPEN_STATUSES, Proposal, Reply, Status, Verdict)
from .policy import ApprovalPolicy, UNANIMOUS
from .ports import Clock, Notifier, Renderer, Store, SystemClock, Transport
from .render import PlainTextRenderer
from .verdict import VerdictSummary, summarize

log = logging.getLogger("abeyance")

Executor = Callable[[Item], Union[ItemOutcome, Dict[str, Any], None]]
"""Your side of the contract: given an approved item, do the thing and say what happened.

May return an `ItemOutcome`, a plain dict with the same keys, or None (treated as written).
Raising is allowed and is recorded as a refusal for that item — one bad item does not abort
the batch, because a batch that aborts halfway leaves the approver with no idea which of
their yeses took effect.
"""


# --------------------------------------------------------------------------- results


@dataclass
class ProposeResult:
    proposal: Optional[Proposal]
    sent: bool
    subject: str
    body: str
    to: List[str]
    skipped: str = ""

    @property
    def id(self) -> str:
        return self.proposal.id if self.proposal else ""


@dataclass
class InboundReply:
    """A reply, plus what the parser makes of it. Nothing is recorded by reading."""

    reply: Reply
    known_approver: bool
    suggestion: Suggestion

    @property
    def sender(self) -> str:
        return self.reply.sender

    @property
    def text(self) -> str:
        return self.reply.text

    def to_doc(self) -> Dict[str, Any]:
        return {"message_id": self.reply.message_id, "from": self.reply.sender,
                "epoch": self.reply.epoch, "text": self.reply.text,
                "known_approver": self.known_approver,
                "suggestion": self.suggestion.to_doc()}


@dataclass
class PollResult:
    """What the cheap gate found.

    `actionable` is the only field a scheduled runner needs: empty means exit without
    spending anything. That is the normal outcome of most ticks, and making it cheap is what
    lets the loop run hourly for months.
    """

    actionable: List[str] = field(default_factory=list)
    quiet: List[str] = field(default_factory=list)
    expired: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    checked: int = 0

    def __bool__(self) -> bool:
        return bool(self.actionable)

    def to_doc(self) -> Dict[str, Any]:
        return {"actionable": self.actionable, "quiet": self.quiet, "expired": self.expired,
                "errors": self.errors, "checked": self.checked}


@dataclass
class NudgeResult:
    sent: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)

    def to_doc(self) -> Dict[str, Any]:
        return {"sent": self.sent, "skipped": self.skipped}


def _default_escalate(event: EscalationEvent) -> None:
    """Loud by default. An escalation that nobody sees is the failure this library is trying
    to avoid, so the fallback is a WARNING on a named logger rather than a silent append to a
    list you have to remember to read."""
    log.warning("escalation[%s] %s %s", event.kind.value, event.subject_key or event.proposal_id,
                event.detail)


# --------------------------------------------------------------------------- the loop


class ApprovalLoop:
    """One named approval loop: one policy, one audience shape, one store namespace.

    Run several — a low-stakes single-approver loop and a two-yes loop for irreversible work
    — over the same store and transport. The name keys their state apart.
    """

    def __init__(
        self,
        name: str,
        *,
        store: Store,
        transport: Transport,
        policy: ApprovalPolicy = UNANIMOUS,
        notifier: Optional[Notifier] = None,
        renderer: Optional[Renderer] = None,
        clock: Optional[Clock] = None,
        vocabulary: Optional[Vocabulary] = None,
        on_escalate: Optional[Callable[[EscalationEvent], None]] = None,
    ) -> None:
        if not name:
            raise ConfigurationError("a loop needs a name — it namespaces its state")
        try:
            policy.validate()
        except ValueError as e:
            raise PolicyError(str(e)) from e

        self.name = name
        self.store = store
        self.transport = transport
        self.policy = policy
        self.notifier = notifier
        self.renderer = renderer or PlainTextRenderer(product=name)
        self.clock = clock or SystemClock()
        self.vocabulary = vocabulary
        self._escalate = on_escalate or _default_escalate

    # ----------------------------------------------------------------- storage

    @property
    def kind(self) -> str:
        return f"{self.name}:proposal"

    def _now(self) -> int:
        return self.clock.now()

    def _iso(self) -> str:
        return datetime.fromtimestamp(self._now(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def save(self, p: Proposal) -> None:
        self.store.put(self.kind, p.id, p.to_doc())

    def get(self, proposal_id: str) -> Proposal:
        doc = self.store.get(self.kind, proposal_id)
        if doc is None:
            raise ProposalNotFound(f"{self.name}: no proposal {proposal_id!r}")
        return Proposal.from_doc(doc)

    def all(self) -> List[Proposal]:
        return [Proposal.from_doc(d) for d in self.store.items(self.kind).values()]

    def pending(self) -> List[Proposal]:
        """Open proposals, oldest first. A store read only — safe to call from anything."""
        return sorted((p for p in self.all() if p.is_open), key=lambda p: p.sent_epoch)

    def summary(self, p: Proposal) -> VerdictSummary:
        return summarize(p, self.policy)

    def escalate(self, kind: Escalation, p: Proposal, detail: str = "",
                 items: Optional[Sequence[int]] = None) -> EscalationEvent:
        event = EscalationEvent(kind=kind, proposal_id=p.id, subject_key=p.subject_key,
                                detail=detail, items=list(items or []))
        p.log("escalated", kind=kind.value, detail=detail, items=list(items or []))
        try:
            self._escalate(event)
        except Exception:  # noqa: BLE001 - a broken handler must not eat the loop
            log.exception("escalation handler raised for %s", p.id)
        return event

    # ----------------------------------------------------------------- propose

    def propose(
        self,
        items: Sequence[Item],
        approvers: Sequence[Approver],
        *,
        subject_key: str = "",
        track: str = "",
        subject: Optional[str] = None,
        body: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        cc: Sequence[str] = (),
        dry_run: bool = False,
        send_empty: bool = False,
        to_override: Optional[Sequence[str]] = None,
    ) -> ProposeResult:
        """Render the digest, send it, and persist everything needed to resume.

        Order matters and is not negotiable: **send first, then store.** If the store write
        fails after a successful send we raise, and the operator sees an asked-but-unrecorded
        proposal — recoverable. The other order gives a recorded proposal nobody was ever
        asked about, which waits, nudges nobody, and expires as a lie.
        """
        if not approvers:
            raise NoApproversError(
                f"{self.name}: refusing to send a proposal with no approvers — it could only "
                "ever expire")

        by_addr: Dict[str, Approver] = {}
        for a in approvers:
            if not a.address:
                raise NoApproversError(f"{self.name}: approver with no address")
            by_addr[a.address] = a

        if self.policy.roles_required:
            have = {a.role for a in by_addr.values()}
            missing = [r for r in self.policy.roles_required if r not in have]
            if missing:
                raise ConfigurationError(
                    f"{self.name}: policy requires role(s) {missing} among approvers, got "
                    f"{sorted(have)}")

        if not self.policy.allow_self_approval:
            me = (self.transport.address or "").lower()
            if me and set(by_addr) == {me}:
                raise ConfigurationError(
                    f"{self.name}: the only approver is the sending identity ({me}) — this "
                    "loop would be asking itself for permission")

        actionable = [i for i in items if not i.advisory]
        if not actionable and not send_empty:
            return ProposeResult(proposal=None, sent=False, subject="", body="",
                                 to=sorted(by_addr), skipped="no actionable items")

        now = self._now()
        draft = Proposal(
            id="(unsent)", subject_key=subject_key, track=track,
            items=list(items), approvers=by_addr, status=Status.AWAITING_REPLY,
            sent_epoch=now, last_activity_epoch=now, turns=1, context=dict(context or {}),
        )
        subj = subject if subject is not None else self.renderer.subject(draft)
        text = body if body is not None else self.renderer.body(
            draft, expire_days=self.policy.expire_after_days)
        draft.subject = subj
        to = list(to_override) if to_override else sorted(by_addr)

        if dry_run:
            return ProposeResult(proposal=draft, sent=False, subject=subj, body=text, to=to,
                                 skipped="dry run")

        try:
            sent = self.transport.send(to=to, subject=subj, body=text, cc=cc)
        except Exception as e:  # noqa: BLE001
            raise TransportError(f"{self.name}: send failed, nothing recorded: {e}") from e

        draft.id = sent.thread_id
        draft.sent_message_id = sent.message_id
        draft.last_outbound_id = sent.message_id
        draft.last_outbound_header_id = sent.header_message_id
        draft.log("proposed", to=to, items=len(actionable), approvers=sorted(by_addr))
        self.save(draft)
        return ProposeResult(proposal=draft, sent=True, subject=subj, body=text, to=to)

    # ----------------------------------------------------------------- the cheap gate

    def poll(self, *, proposal_ids: Optional[Sequence[str]] = None,
             expire: bool = True) -> PollResult:
        """Is there anything worth waking up for?

        **This must stay free of model calls.** It is the whole economic argument for holding
        work in abeyance: an hourly tick that costs a few API reads can run for months, and one
        that costs a model invocation per open proposal cannot. It reads state, reads the
        transport, and returns ids. It does not interpret, record, or execute.

        `expire=True` also settles anything past its deadline, because the sweep is free once
        you are already holding the open set.
        """
        res = PollResult()
        open_props = self.pending() if proposal_ids is None else [
            p for p in (self.get(i) for i in proposal_ids) if p.is_open]

        for p in open_props:
            res.checked += 1
            if expire and self._expire_if_due(p):
                res.expired.append(p.id)
                continue
            try:
                replies = self._fetch_new(p)
            except Exception as e:  # noqa: BLE001 - one dead thread must not blind the rest
                res.errors[p.id] = str(e)[:200]
                log.warning("poll: fetch failed for %s: %s", p.id, e)
                continue
            if any(r.sender in p.approvers for r in replies):
                res.actionable.append(p.id)
            elif replies:
                # Someone who is not an approver spoke. Worth a human's eyes, not a write.
                if self.policy.require_known_sender:
                    who = sorted({r.sender for r in replies} - set(p.approvers))
                    self.escalate(Escalation.UNKNOWN_SENDER, p,
                                  detail=f"reply from non-approver(s): {who}")
                    self.save(p)
                res.quiet.append(p.id)
            else:
                res.quiet.append(p.id)
        return res

    def _fetch_new(self, p: Proposal) -> List[Reply]:
        exclude = {i for i in
                   (set(p.seen_reply_ids) | {p.sent_message_id, p.last_outbound_id}) if i}
        replies = self.transport.fetch_replies(p.id, exclude_ids=exclude)
        me = (self.transport.address or "").lower()
        # Drop our own by SENDER as well as by id. A message we sent but failed to record —
        # a crash between the send and the state write — has an id we do not know about, and
        # would otherwise come back looking like an inbound reply. An apply tick would then
        # read our own proposal text as somebody's approval.
        return [r for r in replies
                if r.message_id not in exclude and r.sender != me]

    # ----------------------------------------------------------------- read

    def read(self, proposal_id: str) -> List[InboundReply]:
        """Fetch the new replies and parse each into a suggestion. Records nothing.

        Reading and deciding are separate calls on purpose. The parser is a convenience for
        the unambiguous majority; the decision about what a person meant belongs to something
        with judgment, and making that an explicit second call is what stops a regex from
        quietly becoming the authority on consent.
        """
        p = self.get(proposal_id)
        valid = sorted(p.item_numbers)
        out = []
        for r in self._fetch_new(p):
            sugg = interpret(r.text, p.max_n, vocabulary=self.vocabulary, valid=valid)
            out.append(InboundReply(reply=r, known_approver=r.sender in p.approvers,
                                    suggestion=sugg))
        return out

    # ----------------------------------------------------------------- record

    def record(
        self,
        proposal_id: str,
        sender: str,
        *,
        approve: Iterable[int] = (),
        reject: Iterable[int] = (),
        raw: Optional[str] = None,
        reply_ids: Iterable[str] = (),
    ) -> Proposal:
        """Write one approver's decision into state.

        Idempotent per approver: calling it again for the same person replaces their ledger
        rather than appending, so an apply tick that runs twice on the same reply reaches the
        same state. `reply_ids` marks the messages this decision came from as consumed; pass
        them or the same reply resurfaces on the next poll.
        """
        p = self.get(proposal_id)
        who = (sender or "").strip().lower()
        a = p.approver(who)
        if a is None:
            raise UnknownApprover(
                f"{self.name}: {who!r} is not an approver on {proposal_id} "
                f"(expected one of {sorted(p.approvers)})")

        valid = p.item_numbers
        req_a = {int(n) for n in approve}
        req_r = {int(n) for n in reject}
        unknown = sorted((req_a | req_r) - valid)
        if unknown:
            raise ValueError(f"{self.name}: item(s) {unknown} are not in proposal {proposal_id}")
        overlap = req_a & req_r
        if overlap:
            raise ValueError(f"{self.name}: item(s) {sorted(overlap)} both approved and "
                             "rejected in one decision")

        a.approved = sorted(req_a)
        a.rejected = sorted(req_r)
        a.raw = raw if raw is not None else a.raw
        a.replied_at = self._iso()

        p.seen_reply_ids = sorted(set(p.seen_reply_ids) | {str(i) for i in reply_ids if i})
        p.touch(self._now())
        p.status = (Status.AWAITING_REPLY
                    if all(x.has_replied for x in p.approvers.values())
                    else Status.PARTIALLY_APPROVED)
        p.log("recorded", approver=who, approved=a.approved, rejected=a.rejected)
        self.save(p)
        return p

    def record_from(self, proposal_id: str, inbound: InboundReply, *,
                    require_confident: bool = True) -> Optional[Proposal]:
        """Record straight from a parsed reply, refusing anything the parser is not sure of.

        The convenience path for unattended runs that accept regex-level confidence for
        low-stakes loops. `require_confident=True` (default) declines conditional, bare-number,
        affirmation-only, and unparseable replies and escalates instead, which keeps the
        judgment call with a human for exactly the replies that need one.
        """
        if not inbound.known_approver:
            p = self.get(proposal_id)
            self.escalate(Escalation.UNKNOWN_SENDER, p,
                          detail=f"reply from {inbound.sender}, not an approver")
            self.save(p)
            return None
        s = inbound.suggestion
        if require_confident and not s.confident:
            p = self.get(proposal_id)
            self.escalate(Escalation.AMBIGUOUS_REPLY, p,
                          detail=f"{inbound.sender}: mode={s.mode} conditional={s.conditional} "
                                 f"— needs a human read: {s.text[:160]!r}")
            self.save(p)
            return None
        return self.record(proposal_id, inbound.sender, approve=s.approve, reject=s.reject,
                           raw=s.text, reply_ids=[inbound.reply.message_id])

    def dismiss(self, proposal_id: str, reply_ids: Iterable[str], note: str = "") -> Proposal:
        """Mark replies as read without treating them as a decision — a thank-you, an
        out-of-office, a question you answered elsewhere. Without this they resurface every
        hour and the loop looks permanently actionable."""
        p = self.get(proposal_id)
        p.seen_reply_ids = sorted(set(p.seen_reply_ids) | {str(i) for i in reply_ids if i})
        p.touch(self._now())
        p.log("dismissed", note=note, ids=list(reply_ids))
        self.save(p)
        return p

    # ----------------------------------------------------------------- converse

    def ask(self, proposal_id: str, question: str, *,
            to: Optional[Sequence[str]] = None,
            reply_ids: Iterable[str] = ()) -> Optional[Proposal]:
        """Send a clarification on the same thread and count a turn.

        On hitting the turn cap the proposal goes `STALLED` and is escalated **instead of**
        sending. A loop that keeps asking is worse than one that gives up and says so: the
        approver stops reading, and the next real proposal is ignored too.
        """
        p = self.get(proposal_id)
        if not p.is_open:
            raise AlreadyExecuted(f"{self.name}: {proposal_id} is {p.status.value}")
        if p.turns >= self.policy.max_turns:
            p.status = Status.STALLED
            self.escalate(Escalation.STALL, p,
                          detail=f"hit the {self.policy.max_turns}-turn cap without resolving")
            self.save(p)
            return None

        sent = self.transport.send(
            to=list(to) if to else sorted(p.approvers),
            subject=f"Re: {p.subject}" if p.subject else "Re: your approval",
            body=question, thread_id=p.id, in_reply_to=p.last_outbound_header_id or None)
        # Asking again reopens the question for whoever we asked. Their ledger is kept — a
        # partial answer is not discarded — but they count as awaited again, so items they
        # passed over stop reading as abstentions under `silence_after_reply="abstain"`.
        reopened = [a.lower() for a in to] if to else list(p.approvers)
        for addr in reopened:
            approver = p.approver(addr)
            if approver is not None:
                approver.replied_at = None

        p.turns += 1
        p.status = Status.CLARIFYING
        p.last_outbound_id = sent.message_id
        p.last_outbound_header_id = sent.header_message_id or p.last_outbound_header_id
        p.seen_reply_ids = sorted(set(p.seen_reply_ids) | {str(i) for i in reply_ids if i})
        p.touch(self._now())
        p.log("asked", turn=p.turns, reopened=reopened)
        self.save(p)
        return p

    def confirm(self, proposal_id: str, body: str) -> Proposal:
        """Reply in-thread with what happened. Cheap, and it closes the loop for the person
        who answered — they asked for something and want to see it land."""
        p = self.get(proposal_id)
        sent = self.transport.send(to=sorted(p.approvers),
                                   subject=f"Re: {p.subject}" if p.subject else "Re: approval",
                                   body=body, thread_id=p.id,
                                   in_reply_to=p.last_outbound_header_id or None)
        p.last_outbound_id = sent.message_id
        p.last_outbound_header_id = sent.header_message_id or p.last_outbound_header_id
        p.touch(self._now())
        p.log("confirmed")
        self.save(p)
        return p

    def receipt(self, proposal_id: str, report: ExecutionReport, *,
                to: Sequence[str], audience_note: str = "",
                dry_run: bool = False) -> Dict[str, Any]:
        """Tell a (possibly different) audience what changed, as a FRESH thread.

        Deliberately not a cc onto the approval thread: a reader who was not part of the
        decision should get a standalone note, not a wall of quoted negotiation.
        """
        p = self.get(proposal_id)
        render = getattr(self.renderer, "receipt", None)
        if render is None:
            raise ConfigurationError("renderer has no receipt()")
        subject, body = render(p, report, audience_note=audience_note)
        if dry_run:
            return {"to": list(to), "subject": subject, "body": body, "sent": False}
        sent = self.transport.send(to=list(to), subject=subject, body=body)
        p.log("receipt", to=list(to), thread=sent.thread_id)
        self.save(p)
        return {"to": list(to), "subject": subject, "thread_id": sent.thread_id, "sent": True}

    # ----------------------------------------------------------------- execute

    def execute(self, proposal_id: str, executor: Executor, *,
                dry_run: bool = False, force: bool = False) -> ExecutionReport:
        """Run the items that cleared, and only those.

        Blocks while anything is still WAITING — the point of a threshold is that it is not
        met until it is met. `force=True` executes what is currently approved and abandons the
        rest; it exists for the operator who has decided to stop waiting, and it is recorded
        in history as such.
        """
        p = self.get(proposal_id)
        if p.status is Status.EXECUTED and not force:
            raise AlreadyExecuted(
                f"{self.name}: {proposal_id} already executed — refusing to re-run. Pass "
                "force=True only if you have confirmed the first run did not take effect.")

        s = self.summary(p)
        report = ExecutionReport(proposal_id=p.id, dry_run=dry_run, verdicts=dict(s.table))

        if s.waiting and not force:
            report.blocked = "not every approver has replied"
            report.outcomes = []
            log.info("execute: %s blocked, waiting on %s", p.id, p.waiting_on)
            return report

        for item in p.items:
            v = s.table.get(item.n, Verdict.WAITING)
            if item.advisory:
                report.outcomes.append(ItemOutcome(n=item.n, skipped="advisory — writes nothing"))
                continue
            if v is not Verdict.APPROVED:
                report.outcomes.append(ItemOutcome(n=item.n, skipped=v.value))
                continue
            if dry_run:
                report.outcomes.append(ItemOutcome(n=item.n, skipped="dry run",
                                                   detail={"would_execute": True}))
                continue
            report.outcomes.append(self._run_one(executor, item))

        if not dry_run:
            p.status = Status.DEADLOCKED if s.has_deadlock else Status.EXECUTED
            p.touch(self._now())
            p.log("executed", written=[o.n for o in report.written],
                  refused=[o.n for o in report.refused], forced=bool(force))
            if s.has_deadlock:
                self.escalate(Escalation.DEADLOCK, p, items=s.deadlocked,
                              detail="approvers disagreed — nothing written for these items")
            if report.refused:
                self.escalate(Escalation.EXECUTION_REFUSED, p,
                              items=[o.n for o in report.refused],
                              detail="; ".join(f"{o.n}: {o.error}" for o in report.refused)[:400])
            self.save(p)
        return report

    @staticmethod
    def _run_one(executor: Executor, item: Item) -> ItemOutcome:
        try:
            out = executor(item)
        except Exception as e:  # noqa: BLE001
            # A refusal is data, not a crash. One item that cannot be applied — its target
            # moved, an anchor vanished — must not abandon the other yeses in the batch.
            return ItemOutcome(n=item.n, error=f"{type(e).__name__}: {e}"[:300])
        if out is None:
            return ItemOutcome(n=item.n, written=True)
        if isinstance(out, ItemOutcome):
            out.n = item.n
            return out
        if isinstance(out, dict):
            return ItemOutcome(n=item.n, written=bool(out.get("written", not out.get("error"))),
                               skipped=str(out.get("skipped") or ""),
                               error=str(out.get("error") or ""),
                               detail=out.get("detail") or
                               {k: v for k, v in out.items()
                                if k not in {"written", "skipped", "error", "n", "detail"}})
        return ItemOutcome(n=item.n, written=True, detail={"result": str(out)[:300]})

    # ----------------------------------------------------------------- nudge & sweep

    def nudge(self, *, proposal_id: Optional[str] = None, dry_run: bool = False,
              message: Optional[Callable[[Proposal, Approver], str]] = None) -> NudgeResult:
        """Poke approvers who have gone quiet, on the schedule in the policy.

        Capped, and capped per approver per proposal. An uncapped nudge is how a helpful
        reminder becomes the reason someone filters your loop into a folder.
        """
        res = NudgeResult()
        props = [self.get(proposal_id)] if proposal_id else self.pending()
        now = self._now()

        for p in props:
            if not p.is_open:
                continue
            quiet_h = (now - (p.last_activity_epoch or p.sent_epoch)) / 3600.0
            dirty = False
            for addr, a in sorted(p.approvers.items()):
                if a.has_replied:
                    continue
                due_at = self.policy.nudge_due_at_hours(a.nudges)
                if quiet_h < due_at:
                    res.skipped.append({"proposal": p.id, "to": addr,
                                        "reason": "not due", "quiet_hours": round(quiet_h, 1),
                                        "due_at_hours": due_at})
                    continue
                text = (message(p, a) if message else self._default_nudge(p))
                rec = {"proposal": p.id, "subject_key": p.subject_key, "to": addr,
                       "channel_id": a.channel_id, "nudge_number": a.nudges + 1,
                       "quiet_hours": round(quiet_h, 1)}
                if dry_run:
                    rec["dry_run"] = True
                    res.sent.append(rec)
                    continue
                if self.notifier is None or not a.channel_id:
                    rec["error"] = ("no notifier configured" if self.notifier is None
                                    else "approver has no channel_id")
                    res.skipped.append(rec)
                    continue
                try:
                    self.notifier.notify(a.channel_id, text, address=addr,
                                         context={"proposal_id": p.id,
                                                  "subject_key": p.subject_key})
                except Exception as e:  # noqa: BLE001
                    rec["error"] = str(e)[:200]
                    res.skipped.append(rec)
                    continue
                a.nudges += 1
                dirty = True
                p.log("nudged", approver=addr, number=a.nudges)
                res.sent.append(rec)
            if dirty:
                self.save(p)
        return res

    def _default_nudge(self, p: Proposal) -> str:
        n = len(p.actionable_items)
        days = self.policy.expire_after_days
        what = p.subject_key or "a proposal"
        return (f"Still waiting on you for {what} — {n} item{'s' if n != 1 else ''} to "
                f"approve. Nothing is written until you answer, and it expires "
                f"{days:g} days after the last activity.")

    def sweep(self) -> Dict[str, List[str]]:
        """Settle everything past its deadline. Safe to run on any tick; costs one store read.

        Expiry is the honest end state for a proposal nobody answered, and it has to *say so*
        — an expiry that passes silently is indistinguishable from a healthy quiet week, and
        the work in it dies unnoticed. Every expiry raises an escalation.
        """
        out: Dict[str, List[str]] = {"expired": [], "stalled": []}
        for p in self.pending():
            if self._expire_if_due(p):
                out["expired"].append(p.id)
            elif p.turns > self.policy.max_turns:
                p.status = Status.STALLED
                self.escalate(Escalation.STALL, p, detail="turn cap exceeded")
                self.save(p)
                out["stalled"].append(p.id)
        return out

    def _expire_if_due(self, p: Proposal) -> bool:
        cutoff = self._now() - self.policy.expire_after_days * 86400
        # last_activity, NOT sent_epoch. A conversation in progress must not time out.
        anchor = p.last_activity_epoch or p.sent_epoch
        if anchor and anchor < cutoff:
            p.status = Status.EXPIRED
            unanswered = p.waiting_on
            self.escalate(Escalation.EXPIRY, p,
                          detail=f"expired unanswered after {self.policy.expire_after_days:g}d; "
                                 f"no reply from {unanswered}",
                          items=[i.n for i in p.actionable_items])
            self.save(p)
            return True
        return False
