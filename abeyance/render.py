"""The default plain-text digest, and the receipt.

Plain text on purpose. The reply is the API, and a plain-text message is the one format that
comes back quotable and parseable from every client, including the phone the approver is
actually holding. An HTML digest looks better and produces replies wrapped in `<div>`s.

Three things the body must always contain, each of which is a lesson rather than a
preference:

  * **the numbers, close to the summary** — people reply "1 and 3", so the number has to be
    the most findable thing on the line.
  * **what happens if they say nothing** — an approver who does not know the proposal expires
    treats it as a to-do rather than a decision, and a batch of real work dies at the
    deadline with nobody having decided anything.
  * **who else is being asked** — on a multi-approver track, "you are one of two yeses" is
    the difference between waiting for the other person and assuming someone else has it.

Subclass or replace wholesale; the state machine never reads what is rendered here.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from .models import ExecutionReport, Item, Proposal

RULE = "-" * 68


def clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


class PlainTextRenderer:
    """Reasonable defaults. Every string a human reads is in this file — change it here."""

    def __init__(self, *, product: str = "", reply_hint: str = "",
                 what_happens_next: str = "") -> None:
        self.product = product
        """Optional prefix for the subject line, e.g. "[deploy]"."""
        self.reply_hint = reply_hint or (
            'Reply in plain English — "approve 1 and 3, skip 2" — or say what you would '
            "change.")
        self.what_happens_next = what_happens_next

    # ----------------------------------------------------------------- subject

    def subject(self, p: Proposal) -> str:
        n = len(p.actionable_items)
        who = ("you approve" if len(p.approvers) <= 1
               else f"all {len(p.approvers)} of you approve")
        label = " ".join(x for x in (self.product, p.subject_key) if x).strip()
        head = f"[{label}] " if label else ""
        track = f" ({p.track})" if p.track else ""
        return f"{head}{n} item{'s' if n != 1 else ''} to approve{track} — reply to decide ({who})"

    # ----------------------------------------------------------------- body

    def body(self, p: Proposal, *, expire_days: float = 7.0) -> str:
        L: List[str] = []
        L += self._header(p)
        L.append("")

        actionable = [i for i in p.items if not i.advisory]
        advisory = [i for i in p.items if i.advisory]

        if not actionable and not advisory:
            L.append("Nothing cleared the bar this cycle.")
        for item in actionable:
            L += self._item_block(item)

        if advisory:
            L.append(RULE)
            L.append(f"Also considered, no action proposed ({len(advisory)}):")
            for item in advisory:
                L.append(f"  · {item.summary}")
            L.append("")
            L.append("Listed so you can see what was weighed and set aside. Approving one of")
            L.append("these changes nothing on its own.")
            L.append("")

        L += self._footer(p, expire_days)
        return "\n".join(L).rstrip() + "\n"

    # ----------------------------------------------------------------- pieces

    def _header(self, p: Proposal) -> List[str]:
        L = []
        title = p.subject_key or "Pending decisions"
        L.append(title if not p.track else f"{title} — {p.track}")
        L.append("")
        L.append(self.reply_hint)
        others = [a for a in p.approvers.values()]
        if len(others) > 1:
            names = ", ".join(a.display_name or a.address for a in others)
            L.append("")
            L.append(f"This one needs every one of you: {names}. Nothing is written until")
            L.append("everyone has answered, and if you disagree on an item it is left alone")
            L.append("and raised with a human rather than decided by majority.")
        return L

    def _item_block(self, item: Item) -> List[str]:
        L = [RULE, f"  {item.n}. {item.summary}"]
        if item.detail:
            L.append("")
            for line in item.detail.rstrip().splitlines():
                L.append(f"       {line}")
        L.append("")
        return L

    def _footer(self, p: Proposal, expire_days: float) -> List[str]:
        L = [RULE]
        if self.what_happens_next:
            L.append(self.what_happens_next)
            L.append("")
        days = int(expire_days) if float(expire_days).is_integer() else expire_days
        L.append(f"If nobody replies within {days} days this expires and nothing is done.")
        L.append("Replying restarts that clock, so a conversation in progress will not")
        L.append("time out under you.")
        return L

    # ----------------------------------------------------------------- receipt

    def receipt(self, p: Proposal, report: ExecutionReport,
                *, audience_note: str = "") -> "tuple[str, str]":
        """The leg most approval systems skip: telling people what their yes actually did.

        Sent as a FRESH thread rather than a reply, so a reader who was not part of the
        approval conversation gets a clean standalone note instead of a wall of quoted
        history. Without this an approver has no way to learn that their decision landed, and
        the next thing they do is stop trusting the loop.
        """
        written = report.written
        subject = (f"[{self.product}] " if self.product else "") + \
                  f"{p.subject_key or 'Applied'} — {len(written)} change(s) applied"

        L = [f"{p.subject_key or 'This proposal'} — here is what changed, and why.", ""]
        if not written:
            L.append("Nothing was written this cycle.")
            L.append("")
        for o in written:
            item = p.item(o.n)
            L.append(f"  • {item.summary if item else f'item {o.n}'}")
            for k, v in (o.detail or {}).items():
                L.append(f"      {k}: {clip(str(v), 200)}")
            L.append("")

        if report.refused:
            L.append("Refused — the target moved since this was drafted, so it will be")
            L.append("re-proposed rather than forced:")
            for o in report.refused:
                L.append(f"  · item {o.n}: {o.error}")
            L.append("")

        deadlocked = [n for n, v in report.verdicts.items() if v.value == "deadlocked"]
        if deadlocked:
            L.append(f"Left alone — you disagreed on item(s) {deadlocked}. Nothing was")
            L.append("written for those and a human has been asked to break the tie.")
            L.append("")

        if audience_note:
            L.append(audience_note)
        return subject, "\n".join(L).rstrip() + "\n"


def render_escalation(events: Sequence["object"]) -> str:
    """One plain block summarizing why a human is being pulled in. Kept out of the renderer
    class because escalations go to an owner, not to the approvers, and the two audiences
    want different registers."""
    if not events:
        return "Nothing needs attention.\n"
    L = ["The approval loop could not resolve these on its own:", ""]
    for e in events:
        d = e.to_doc() if hasattr(e, "to_doc") else dict(e)  # type: ignore[arg-type]
        L.append(f"  [{d.get('kind')}] {d.get('subject_key') or d.get('proposal_id')}")
        if d.get("detail"):
            L.append(f"      {d['detail']}")
        if d.get("items"):
            L.append(f"      items: {d['items']}")
        L.append("")
    return "\n".join(L).rstrip() + "\n"
