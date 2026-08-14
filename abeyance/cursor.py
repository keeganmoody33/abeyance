"""Watermarks and the due-gate: which subjects should be proposed on at all, and when it is
safe to say "we have handled everything up to here".

An abeyance loop needs an answer to two questions before it renders anything:

  **Is this subject due?**  Running every subject on a fixed schedule wastes the expensive
  half of the tick on subjects with nothing new, and a fixed lookback window double-reads
  fast-moving subjects while missing slow ones. Triggers plus a per-subject watermark
  ("everything since *your* last run") handle both.

  **May the watermark move?**  This is the dangerous one. A cursor that advances after a
  failed send — or after a proposal that was rendered but never persisted — skips that window
  **forever, and silently**. There is no error, no retry, no gap in a log. The window simply
  never existed.

`CursorRun` makes that structurally impossible rather than documented. You declare what has
to be true before the watermark may move; `advance()` refuses while any of it is outstanding.
The rule stops being a comment somebody has to remember and becomes an exception somebody
cannot miss.

    run = gate.begin("acme")
    run.require("ledger", "the append-only record of what we read")
    run.require("digest", "the approval email")
    ...
    run.satisfied("ledger", ref=commit_sha)
    run.satisfied("digest", ref=proposal.id)
    run.advance(marks={"last_event_ts": ts})     # raises unless BOTH are satisfied
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

from .errors import CursorNotCommittable
from .ports import Clock, Store, SystemClock


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_days(iso_ts: Optional[str], now: int) -> Optional[float]:
    if not iso_ts:
        return None
    try:
        t = datetime.strptime(iso_ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (now - t.timestamp()) / 86400.0


# --------------------------------------------------------------------------- cursor


@dataclass
class Cursor:
    """One subject's high-water marks.

    `marks` is a free-form dict — a message timestamp, a list of ids already consumed, a
    changelog offset. The library never interprets them; it only guards *when* they may
    change. `seen` holds bounded id sets for sources with no orderable cursor.
    """

    subject: str
    marks: Dict[str, Any] = field(default_factory=dict)
    seen: Dict[str, List[str]] = field(default_factory=dict)
    last_run: Optional[str] = None
    runs: int = 0

    def remember(self, bucket: str, ids: Sequence[str], cap: int = 500) -> List[str]:
        """Add ids to a bounded seen-set, newest kept. Bounded because an unbounded id list in
        a state doc grows until the write itself becomes the problem."""
        cur = list(self.seen.get(bucket) or [])
        cur.extend(str(i) for i in ids)
        deduped = list(dict.fromkeys(cur))[-cap:]
        self.seen[bucket] = deduped
        return deduped

    def unseen(self, bucket: str, ids: Sequence[str]) -> List[str]:
        known = set(self.seen.get(bucket) or [])
        return [str(i) for i in ids if str(i) not in known]

    def to_doc(self) -> Dict[str, Any]:
        return {"subject": self.subject, "marks": self.marks, "seen": self.seen,
                "last_run": self.last_run, "runs": self.runs}

    @classmethod
    def from_doc(cls, d: Dict[str, Any]) -> "Cursor":
        return cls(subject=d["subject"], marks=dict(d.get("marks") or {}),
                   seen={k: list(v) for k, v in (d.get("seen") or {}).items()},
                   last_run=d.get("last_run"), runs=int(d.get("runs") or 0))


# --------------------------------------------------------------------------- triggers


@dataclass
class TriggerResult:
    fired: bool
    reason: str = ""
    marks: Dict[str, Any] = field(default_factory=dict)
    """Anything the trigger already learned, handed forward so the caller does not fetch it
    twice."""


Trigger = Callable[[str, Cursor], TriggerResult]
"""(subject, cursor) -> did something happen worth proposing about?

Triggers are yours; the library only sequences them. Keep the cheap ones cheap: the gate runs
on every tick over every subject, and a trigger that costs a paginated API crawl turns a free
gate into the expensive thing it exists to avoid. Mark those `expensive=True` on
`register()` and they run only on a deep pass.
"""


@dataclass
class DueVerdict:
    subject: str
    due: bool = False
    trigger: str = ""
    reasons: List[str] = field(default_factory=list)
    blocked: str = ""
    """Non-empty means the subject *cannot* run — a missing approver, an unresolvable target.
    Distinct from `due=False`: not-due is a healthy quiet subject, blocked is a
    misconfiguration that will never resolve itself and should be reported."""

    marks: Dict[str, Any] = field(default_factory=dict)
    last_run: Optional[str] = None
    age_days: Optional[float] = None

    def to_doc(self) -> Dict[str, Any]:
        return {"subject": self.subject, "due": self.due, "trigger": self.trigger,
                "reasons": self.reasons, "blocked": self.blocked, "marks": self.marks,
                "last_run": self.last_run,
                "age_days": None if self.age_days is None else round(self.age_days, 1)}


# --------------------------------------------------------------------------- the gate


class DueGate:
    """Decides which subjects to spend the expensive half of a tick on.

    The floor sweep is not optional and defaults on. Trigger-only scheduling means a subject
    whose trigger source breaks — a renamed channel, a revoked token, an integration quietly
    returning empty — is never due again, and *looks exactly like a quiet subject*. The floor
    turns that silence into a run.
    """

    def __init__(self, store: Store, *, name: str, floor_days: float = 14.0,
                 clock: Optional[Clock] = None) -> None:
        self.store = store
        self.name = name
        self.floor_days = floor_days
        self.clock = clock or SystemClock()
        self._triggers: List[Dict[str, Any]] = []
        self._preconditions: List[Callable[[str, Cursor], Optional[str]]] = []

    @property
    def kind(self) -> str:
        return f"{self.name}:cursor"

    # ----------------------------------------------------------------- registration

    def register(self, name: str, trigger: Trigger, *, expensive: bool = False) -> "DueGate":
        self._triggers.append({"name": name, "fn": trigger, "expensive": expensive})
        return self

    def precondition(self, check: Callable[[str, Cursor], Optional[str]]) -> "DueGate":
        """A check that can *block* a subject. Return a reason string to block, None to pass.

        Use it for things that make a run pointless rather than merely quiet — no approver
        configured, no reachable target. Blocked subjects are reported, never silently
        skipped, because a subject that is permanently blocked and permanently quiet are the
        same shape in a log and very different problems.
        """
        self._preconditions.append(check)
        return self

    # ----------------------------------------------------------------- cursors

    def cursor(self, subject: str) -> Cursor:
        doc = self.store.get(self.kind, subject)
        return Cursor.from_doc(doc) if doc else Cursor(subject=subject)

    def save(self, cursor: Cursor) -> None:
        self.store.put(self.kind, cursor.subject, cursor.to_doc())

    def all_cursors(self) -> Dict[str, Cursor]:
        return {k: Cursor.from_doc(v) for k, v in self.store.items(self.kind).items()}

    # ----------------------------------------------------------------- evaluation

    def evaluate(self, subject: str, *, deep: bool = False) -> DueVerdict:
        """Is this subject due, and why? Read-only — evaluating never moves a watermark."""
        now = self.clock.now()
        cur = self.cursor(subject)
        v = DueVerdict(subject=subject, last_run=cur.last_run,
                       age_days=_age_days(cur.last_run, now))

        for check in self._preconditions:
            blocked = check(subject, cur)
            if blocked:
                v.blocked = blocked
                return v

        for t in self._triggers:
            if t["expensive"] and not deep:
                continue
            # An expensive trigger is skipped once something cheap has already made the
            # subject due — the answer cannot change and the call costs real money.
            if t["expensive"] and v.due:
                v.reasons.append(f"{t['name']}: skipped, already due")
                continue
            try:
                r = t["fn"](subject, cur)
            except Exception as e:  # noqa: BLE001 - one broken source must not blind the gate
                v.reasons.append(f"{t['name']}: failed — {str(e)[:120]}")
                continue
            if r.fired:
                v.due = True
                v.trigger = v.trigger or t["name"]
                v.reasons.append(f"{t['name']}: {r.reason}" if r.reason else t["name"])
                v.marks.update(r.marks or {})
            elif r.reason:
                v.reasons.append(f"{t['name']}: {r.reason}")

        age = v.age_days
        if age is None or age >= self.floor_days:
            v.due = True
            v.trigger = v.trigger or "floor-sweep"
            v.reasons.append("never run" if age is None
                             else f"{age:.0f}d since last run (floor {self.floor_days:g}d)")
        return v

    def evaluate_all(self, subjects: Sequence[str], *, deep: bool = False) -> List[DueVerdict]:
        return [self.evaluate(s, deep=deep) for s in subjects]

    def due(self, subjects: Sequence[str], *, deep: bool = False) -> List[DueVerdict]:
        return [v for v in self.evaluate_all(subjects, deep=deep) if v.due and not v.blocked]

    # ----------------------------------------------------------------- runs

    def begin(self, subject: str) -> "CursorRun":
        return CursorRun(self, self.cursor(subject))


class CursorRun:
    """A guarded window. The watermark moves only when everything you declared has happened.

    Nothing here is clever; it is a checklist the code cannot skip. That is the point — the
    rule it enforces ("commit the record and send the ask before you claim to have handled
    this window") is one every implementation gets right on the good path and wrong on the
    path where the send throws.
    """

    def __init__(self, gate: DueGate, cursor: Cursor) -> None:
        self.gate = gate
        self.cursor = cursor
        self._required: Dict[str, str] = {}
        self._satisfied: Dict[str, Any] = {}
        self._committed = False

    # ----------------------------------------------------------------- declarations

    def require(self, name: str, why: str = "") -> "CursorRun":
        self._required[name] = why
        return self

    def satisfied(self, name: str, ref: Any = True) -> "CursorRun":
        if name not in self._required:
            raise KeyError(f"{name!r} was never required on this run — declare it with "
                           "require() so the checklist is visible where the run starts")
        self._satisfied[name] = ref
        return self

    def failed(self, name: str, reason: str = "") -> "CursorRun":
        """Explicitly mark a precondition as failed. Equivalent to never satisfying it, but it
        records *why* in the cursor's history, which is what you want when a run is
        investigated a week later."""
        self._satisfied.pop(name, None)
        self.cursor.marks.setdefault("_failures", []).append(
            {"step": name, "reason": reason, "at": _iso(self.gate.clock.now())})
        # Persisted immediately. `advance()` will raise after this, so anything not written
        # here is lost — and "why did this window not commit" is exactly the question asked a
        # week later, when the only surviving evidence is what the cursor recorded.
        self.gate.save(self.cursor)
        return self

    @property
    def outstanding(self) -> List[str]:
        return sorted(set(self._required) - set(self._satisfied))

    @property
    def committable(self) -> bool:
        return not self.outstanding

    # ----------------------------------------------------------------- commit

    def advance(self, *, marks: Optional[Dict[str, Any]] = None,
                seen: Optional[Dict[str, Sequence[str]]] = None,
                seen_cap: int = 500) -> Cursor:
        """Move the watermark. Raises unless every declared precondition is satisfied."""
        if self._committed:
            raise CursorNotCommittable(f"cursor for {self.cursor.subject!r} already advanced "
                                       "on this run")
        if self.outstanding:
            raise CursorNotCommittable(
                f"refusing to advance the cursor for {self.cursor.subject!r}: "
                f"{self.outstanding} did not complete. Advancing now would skip this window "
                "permanently and silently. Fix the failure and re-run — the window is still "
                "unread, which is the recoverable state.")
        self.cursor.marks.update(marks or {})
        for bucket, ids in (seen or {}).items():
            self.cursor.remember(bucket, list(ids), cap=seen_cap)
        self.cursor.last_run = _iso(self.gate.clock.now())
        self.cursor.runs += 1
        self.gate.save(self.cursor)
        self._committed = True
        return self.cursor

    def abandon(self, reason: str = "") -> None:
        """End the run without moving the watermark. The normal outcome of a failed tick, and
        naming it makes "we deliberately did not advance" legible in the code."""
        self.cursor.marks.setdefault("_abandoned", []).append(
            {"reason": reason, "at": _iso(self.gate.clock.now())})
        self.gate.save(self.cursor)

    # ----------------------------------------------------------------- context manager

    def __enter__(self) -> "CursorRun":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None and not self._committed:
            self.abandon(reason=f"{exc_type.__name__}: {exc}")
        return False
