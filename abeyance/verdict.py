"""The approval math: per-item, given every approver's ledger and one policy.

This is the only place in the library that decides whether something may happen, and it is
deliberately a pure function of (proposal, policy). No clock, no store, no transport. You can
read the whole decision procedure in one screen, and a test can drive it to any state without
standing anything up — which matters, because "who was allowed to write this" is the question
you will be asked after an incident.

The five outcomes, and why `DEADLOCKED` and `UNREACHABLE` are not folded into `REJECTED`:

  APPROVED     enough yeses, no blocking no. Run it.
  REJECTED     a decision was made against it. Do not run it, do not re-ask.
  DEADLOCKED   people with equal standing disagreed. Do not run it, and *tell a human* —
               this is the one outcome the machine must not resolve on its own.
  UNREACHABLE  no veto, but enough approvers have declined or the threshold can no longer be
               met. Nobody blocked it; it simply cannot pass. Worth re-proposing.
  WAITING      not enough information yet. Ask again later.
"""
from __future__ import annotations

from typing import Dict, List, Set, Tuple

from .models import Item, Proposal, Verdict
from .policy import ApprovalPolicy


def _tally(proposal: Proposal, n: int) -> Tuple[Set[str], Set[str], Set[str]]:
    """(yes, no, silent) address sets for item `n`.

    An approver who replied but mentioned neither approve nor reject for this item counts as
    silent, not as a no. Reading an omission as a rejection would turn "I only had time to
    look at the first three" into six rejections.
    """
    yes, no, silent = set(), set(), set()
    for addr, a in proposal.approvers.items():
        if n in a.approved:
            yes.add(addr)
        elif n in a.rejected:
            no.add(addr)
        else:
            silent.add(addr)
    return yes, no, silent


def verdict_for(proposal: Proposal, item: Item, policy: ApprovalPolicy) -> Verdict:
    """Decide one item."""
    if item.advisory:
        # An advisory item has no write to gate. Approving it is a signal, not a permission,
        # so it never blocks a batch and never reaches an executor.
        return Verdict.APPROVED

    if not proposal.approvers:
        return Verdict.WAITING

    yes, no, silent = _tally(proposal, item.n)
    needed = policy.required_yeses(len(proposal.approvers))

    if policy.veto and no:
        # Someone with standing said no. If someone else said yes, that is a genuine
        # disagreement and the machine refuses to pick a side.
        return Verdict.DEADLOCKED if yes else Verdict.REJECTED

    if len(yes) >= needed:
        return Verdict.APPROVED

    # Can it still get there? Who counts as "might still say yes" is the `silence_after_reply`
    # question: under "abstain" an approver's reply was their answer, so an item they passed
    # over cannot reach the threshold and is reported UNREACHABLE instead of hanging as
    # WAITING until the whole proposal expires with real decisions inside it.
    if policy.silence_after_reply == "waiting":
        open_voices = len(silent)
    else:
        open_voices = len([a for a in silent if not proposal.approvers[a].has_replied])
    if len(yes) + open_voices < needed:
        return Verdict.REJECTED if no else Verdict.UNREACHABLE

    return Verdict.WAITING


def verdicts(proposal: Proposal, policy: ApprovalPolicy) -> Dict[int, Verdict]:
    """Decide every item. Keys are item numbers, in the proposal's own numbering."""
    return {i.n: verdict_for(proposal, i, policy) for i in proposal.items}


# --------------------------------------------------------------------------- summaries


class VerdictSummary:
    """A read-only view over a verdict map, so callers stop re-deriving the same groupings.

    `settled` is the property that drives the loop: while anything is WAITING the proposal
    stays open, and the apply tick has nothing to do.
    """

    def __init__(self, table: Dict[int, Verdict]) -> None:
        self.table = dict(table)

    def _of(self, v: Verdict) -> List[int]:
        return sorted(n for n, x in self.table.items() if x is v)

    @property
    def approved(self) -> List[int]:
        return self._of(Verdict.APPROVED)

    @property
    def rejected(self) -> List[int]:
        return self._of(Verdict.REJECTED)

    @property
    def deadlocked(self) -> List[int]:
        return self._of(Verdict.DEADLOCKED)

    @property
    def waiting(self) -> List[int]:
        return self._of(Verdict.WAITING)

    @property
    def unreachable(self) -> List[int]:
        return self._of(Verdict.UNREACHABLE)

    @property
    def settled(self) -> bool:
        """True when no further reply could change any item's outcome."""
        return not self.waiting

    @property
    def has_deadlock(self) -> bool:
        return bool(self.deadlocked)

    def to_doc(self) -> Dict[str, str]:
        return {str(n): v.value for n, v in sorted(self.table.items())}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"<VerdictSummary approved={self.approved} rejected={self.rejected} "
                f"deadlocked={self.deadlocked} waiting={self.waiting} "
                f"unreachable={self.unreachable}>")


def summarize(proposal: Proposal, policy: ApprovalPolicy) -> VerdictSummary:
    return VerdictSummary(verdicts(proposal, policy))
