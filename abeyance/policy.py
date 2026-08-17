"""How many yeses, from whom, and how long we wait.

One object holds every knob that decides whether a proposal passes, because these settings
are not independent: a 7-day expiry with a 24-hour first nudge is a coherent policy, and a
1-hour expiry with the same nudge schedule is a policy that nudges nobody. Keeping them
together makes an incoherent combination visible at the definition site, and `validate()`
refuses the ones that are provably broken.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence, Tuple, Union

ALL = "all"
"""Sentinel for `threshold`: every approver must say yes."""


@dataclass(frozen=True)
class ApprovalPolicy:
    """The rules of consent for one loop.

    `threshold` — how many yeses an item needs. `ALL` (the default) means unanimity, and
    unanimity is the default on purpose: this library exists for actions whose cost of being
    wrong is higher than the cost of waiting. Loosen it deliberately, per loop.

    `veto` — whether one explicit no blocks an item that others approved. With `veto=True`
    (default) a split becomes `DEADLOCKED`: **nothing is written and a human owner is told.**
    That is the correct outcome for a disagreement between two people who both had standing
    to decide — silently taking the majority view would launder a real disagreement into a
    write. With `veto=False`, a no is simply not a yes, and the threshold decides.
    """

    threshold: Union[int, str] = ALL
    veto: bool = True

    expire_after_days: float = 7.0
    """Measured from `last_activity_epoch`, not from send. A live conversation does not
    expire; a silent one does."""

    max_turns: int = 3
    """Outbound messages on one thread before it is declared `STALLED` and escalated. Counts
    the original proposal, so `max_turns=3` allows two clarification rounds."""

    silence_after_reply: str = "abstain"
    """What an item an approver *did not mention* means, once they have replied.

    `"abstain"` (default) — their reply was their answer. An item they did not approve cannot
    reach the threshold, so it comes back `UNREACHABLE` rather than `WAITING`. This is what
    makes an unattended loop terminate: without it, a five-item digest answered with
    "approve 1 and 3" leaves three items indistinguishable from "she has not read it yet",
    and the whole proposal sits until it expires with real decisions dying inside it.

    `"waiting"` — they may still speak about it. Keeps the item open. Correct only if you
    have a human driving follow-ups; on a cron it means waiting forever.

    Either way `ask()` reopens the question for whoever it addresses, so a clarification round
    is not blocked by this setting."""

    nudge_after_hours: Tuple[float, ...] = (24.0, 72.0)
    """Hours of silence before the Nth nudge. `len()` of this is the real nudge cap; it is
    checked against `nudge_cap` in `validate()` so the two cannot disagree."""

    nudge_cap: int = 2

    require_known_sender: bool = True
    """A reply from an address that is not an approver is never counted. When True the loop
    also raises it as an `UNKNOWN_SENDER` escalation rather than dropping it silently — a
    forwarded thread where the wrong person answers is exactly the case you want to hear
    about, not the case you want swallowed."""

    allow_self_approval: bool = True
    """Set False to refuse a proposal whose only approver is the sending identity. Guards the
    degenerate configuration where a loop asks itself for permission."""

    roles_required: Sequence[str] = field(default_factory=tuple)
    """Optional: role labels that must each be represented among the approvers at propose
    time. Catches the misconfiguration where a two-yes policy is handed one person."""

    # ----------------------------------------------------------------- derived

    def required_yeses(self, n_approvers: int) -> int:
        if self.threshold == ALL:
            return n_approvers
        t = int(self.threshold)
        return max(1, min(t, n_approvers))

    def nudge_due_at_hours(self, nudges_sent: int) -> float:
        """Hours of silence at which nudge number `nudges_sent + 1` becomes due, or `inf` if
        this approver has had all the nudges they are going to get."""
        if nudges_sent >= self.nudge_cap or nudges_sent >= len(self.nudge_after_hours):
            return float("inf")
        return float(self.nudge_after_hours[nudges_sent])

    def validate(self) -> None:
        if self.threshold != ALL and int(self.threshold) < 1:
            raise ValueError("threshold must be ALL or >= 1")
        if self.silence_after_reply not in ("abstain", "waiting"):
            raise ValueError("silence_after_reply must be 'abstain' or 'waiting'")
        if self.expire_after_days <= 0:
            raise ValueError("expire_after_days must be positive")
        if self.max_turns < 1:
            raise ValueError("max_turns must be >= 1")
        if self.nudge_cap > len(self.nudge_after_hours):
            raise ValueError(
                f"nudge_cap={self.nudge_cap} but only {len(self.nudge_after_hours)} nudge "
                "times are defined — the extra nudges could never fire")
        horizon = self.expire_after_days * 24
        late = [h for h in self.nudge_after_hours[: self.nudge_cap] if h >= horizon]
        if late:
            raise ValueError(
                f"nudge(s) scheduled at {late}h but the proposal expires at {horizon}h — "
                "those nudges would never be sent")


UNANIMOUS = ApprovalPolicy()
"""Every approver must say yes; one no deadlocks the item. The safe default."""

ANY_ONE = ApprovalPolicy(threshold=1, veto=False)
"""First yes carries; a no is just an abstention. For low-stakes batches where you want a
decision from whoever gets there first."""

SINGLE_APPROVER = ApprovalPolicy(threshold=1, veto=True, nudge_after_hours=(24.0,), nudge_cap=1)
"""One person, one nudge. The shape of most existing single-owner loops."""


def majority(n_approvers: int, **overrides) -> ApprovalPolicy:
    """A policy needing a strict majority of a fixed panel. Convenience for the common case
    where the panel size is known at configuration time."""
    return ApprovalPolicy(threshold=(n_approvers // 2) + 1, veto=False, **overrides)


# --------------------------------------------------------------------------- cases


@dataclass(frozen=True)
class CasePolicy:
    """The knobs for a case: how long a dispatch may take, how many tries, how long authority
    lasts, and who has to have decided.

    Separate from `ApprovalPolicy` because they answer different questions — that one is about
    consent among people, this one is about coordinating contributors and expiring authority.
    A case that asks humans for a decision uses both.
    """

    dispatch_lease_seconds: int = 0
    """How long a dispatched worker has before the dispatch is presumed lost. `0` (default)
    means "derive it from the capability's own `timeout_seconds`", which is the honest source:
    the worker declares how long it needs and the lease follows.

    Set explicitly only to override every capability at once. Too short and healthy workers get
    duplicated; too long and a container that never booted looks like work in progress for
    hours. Both are visible failures, which is better than the invisible one — no lease at all,
    where a lost dispatch waits forever and the case just never finishes.
    """

    lease_grace_seconds: int = 60
    """Added to the derived lease. Covers image pull and boot, which are not the worker's
    declared runtime but do count against the clock."""

    max_attempts: int = 3
    """Dispatches per request before it is FAILED and escalated. A failed request blocks
    authorization — see `ContributionRequest.blocks_authorization`."""

    authorization_ttl_seconds: int = 86_400
    """How long a derived authorization stays valid. `0` means no expiry, which should be rare:
    authority that never goes stale is authority nobody re-checks."""

    min_deciders: int = 1
    """How many standing-carrying decisions authority needs. Raise it for irreversible work;
    the approval layer's own threshold handles the multi-approver conversation, and this is the
    case-level backstop for "two different people, not two replies from one"."""

    required_standing: Sequence[str] = field(default_factory=tuple)
    """Standing labels that must be present among the deciders. Catches the case where the
    right number of people decided and none of them was the one who was supposed to."""

    expire_after_days: float = 14.0
    """Measured from last activity, like the approval layer. A case nobody contributes to is
    dead and should say so."""

    max_derived_requests: int = 25
    """Hard cap on requests a case may accumulate, including dynamically warranted ones. This
    is the runaway guard: a rule that keeps warranting work — directly, or in a loop with
    another rule — would otherwise spend money forever. Hitting the cap blocks the case and
    escalates rather than quietly truncating, because a silently truncated case looks
    thoroughly investigated.
    """

    def validate(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.min_deciders < 1:
            raise ValueError("min_deciders must be >= 1")
        if self.expire_after_days <= 0:
            raise ValueError("expire_after_days must be positive")
        if self.max_derived_requests < 1:
            raise ValueError("max_derived_requests must be >= 1")
        if self.dispatch_lease_seconds < 0 or self.lease_grace_seconds < 0:
            raise ValueError("lease durations cannot be negative")

    def lease_for(self, timeout_seconds: int) -> int:
        """The lease a dispatch of a worker with this declared timeout gets."""
        base = self.dispatch_lease_seconds or int(timeout_seconds or 600)
        return base + self.lease_grace_seconds


CASE_DEFAULT = CasePolicy()
"""One decider, evidence must be complete, authority good for a day."""

CASE_IRREVERSIBLE = CasePolicy(min_deciders=2, authorization_ttl_seconds=3_600,
                               max_attempts=2)
"""Two deciders, and authority that goes stale in an hour so nothing runs on a day-old yes."""
