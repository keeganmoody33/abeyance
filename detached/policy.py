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
