"""Read a human's free-text reply into a *suggestion*.

The point of replying by prose is that people write "yep do 1 and 3, hold the second one,
and can you reword 4 before we commit to it". No regex reads that reliably, and this one does
not pretend to.

So the contract is deliberately weak, and the weakness is the feature:

    interpret() returns a SUGGESTION. It never records anything.

`ApprovalLoop.record()` takes explicit item numbers. Something with judgment — a person, or a
model reading the thread — decides what the reply meant and calls `record()` with the answer.
This module exists to make the easy 80% (`"approve 1,3"`) free and to *flag* the other 20%
loudly rather than guess at it. `Suggestion.confident` is False for anything the caller should
look at, and a conditional reply is never confident, because "approve 1 but change the
wording" is a request for a new draft and reading it as a yes ships text nobody agreed to.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set


# Plain string constants rather than an Enum: a caller comparing `sugg.mode == "explicit"` in
# a log filter or a notebook should not have to import anything.
MODE_ALL = "all"
MODE_ALL_EXCEPT = "all-except"
MODE_EXPLICIT = "explicit"
MODE_SKIP_ONLY = "skip-only"
MODE_BARE_NUMBERS = "bare-numbers"
MODE_AFFIRMATION = "affirmation"
MODE_REJECTION = "rejection"
MODE_CONDITIONAL = "conditional"
MODE_EMPTY = "empty"
MODE_UNPARSEABLE = "unparseable"

CONFIDENT_MODES = {MODE_ALL, MODE_ALL_EXCEPT, MODE_EXPLICIT, MODE_SKIP_ONLY, MODE_REJECTION}
"""Modes a caller may reasonably auto-apply. `MODE_BARE_NUMBERS` is excluded on purpose: a
reply of just "2" is as likely to be "let's talk about 2" as "approve 2", and
`MODE_AFFIRMATION` is excluded because "sounds good" answering a 40-item digest is a person
being polite, not a person having read forty items."""


@dataclass
class Vocabulary:
    """The words your approvers actually use. Defaults are generic; override per loop so the
    verbs match the domain ("post"/"send"/"merge"/"launch") and stop competing with nouns in
    the item text."""

    approve: Sequence[str] = ("approve", "approved", "yes", "do", "go", "ok", "okay",
                              "accept", "apply", "confirm", "ship")
    reject: Sequence[str] = ("skip", "hold", "reject", "drop", "leave", "not", "except",
                             "exclude", "no", "kill", "remove")
    affirmations: Sequence[str] = ("looks good", "lgtm", "ship it", "go ahead", "send it",
                                   "do it", "all good", "sounds good", "go for it",
                                   "perfect", "approved", "fine by me", "happy with")
    rejections: Sequence[str] = ("none of these", "no thanks", "not now", "hold all",
                                 "skip all", "reject all", "let's not", "lets not",
                                 "drop all", "none of them")
    conditionals: Sequence[str] = ("but ", "however", "except that", "if you", "can you",
                                   "could you", "instead", "reword", "rewrite", "change the",
                                   "tweak", "adjust", "before you", "first ", "clarify",
                                   "question", "what about", "why ", "not sure", "unsure",
                                   "hesitant", "let's discuss", "lets discuss")

    def __post_init__(self) -> None:
        for name in ("approve", "reject"):
            if not getattr(self, name):
                raise ValueError(f"vocabulary.{name} cannot be empty")


DEFAULT_VOCABULARY = Vocabulary()


@dataclass
class Suggestion:
    """What the parser thinks the reply meant. Advisory in every case."""

    approve: List[int] = field(default_factory=list)
    reject: List[int] = field(default_factory=list)
    mode: str = MODE_UNPARSEABLE
    conditional: bool = False
    """The reply asks for a change, poses a question, or hedges. Never auto-apply one of
    these — the right response is another turn, not a write."""

    notes: List[str] = field(default_factory=list)
    text: str = ""

    @property
    def confident(self) -> bool:
        return self.mode in CONFIDENT_MODES and not self.conditional

    @property
    def empty(self) -> bool:
        return not self.approve and not self.reject

    def to_doc(self) -> Dict[str, object]:
        return {"approve": self.approve, "reject": self.reject, "mode": self.mode,
                "conditional": self.conditional, "confident": self.confident,
                "notes": self.notes}


# --------------------------------------------------------------------------- parsing

# Digits, separators, range words, and the literal "and". It must not be `[0-9, and]+`: that
# is a character class, so it eats the leading letters of whatever verb comes next — "post 1,
# article 2" parses as approving item 1 and then swallowing the "a" of "article". A grouped
# alternation of whole tokens stops at the first real word, and carries range syntax through
# so "approve 1-4, skip 3" survives as far as `_numbers`.
_NUMRUN = r"((?:\d+|\s|,|&|-|–|—|:|and\b|to\b|through\b|thru\b)+)"
_RANGE = re.compile(r"\b(\d+)\s*(?:-|–|—|to|through|thru|:)\s*(\d+)\b")


def _numbers(fragment: str, n_max: int) -> Set[int]:
    """Every item number named in a fragment, ranges expanded, out-of-range dropped.

    Dropping out-of-range numbers silently is right here: a reply saying "approve 3, we sent
    2400 emails last week" should approve item 3, not item 2400 — and there is no item 2400
    to approve, so there is nothing to warn about.
    """
    out: Set[int] = set()
    work = fragment or ""
    for lo, hi in _RANGE.findall(work):
        lo_i, hi_i = int(lo), int(hi)
        if lo_i <= hi_i and hi_i - lo_i < 500:
            out |= set(range(lo_i, hi_i + 1))
    work = _RANGE.sub(" ", work)
    out |= {int(x) for x in re.findall(r"\d+", work)}
    return {n for n in out if 1 <= n <= n_max}


def _verb_group(words: Sequence[str]) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


def interpret(text: str, n_max: int, vocabulary: Optional[Vocabulary] = None,
              valid: Optional[Sequence[int]] = None) -> Suggestion:
    """Parse one reply. `n_max` bounds what counts as an item number; pass `valid` when the
    numbering has gaps so a reference to a non-existent item is reported rather than kept."""
    vocab = vocabulary or DEFAULT_VOCABULARY
    raw = text or ""
    t = " ".join(raw.lower().split())
    s = Suggestion(text=raw)

    if not t:
        s.mode = MODE_EMPTY
        return s

    s.conditional = any(c in t for c in vocab.conditionals)
    all_items = set(range(1, n_max + 1)) if valid is None else {n for n in valid}

    def finish(approve: Set[int], reject: Set[int], mode: str) -> Suggestion:
        if valid is not None:
            unknown = sorted((approve | reject) - all_items)
            if unknown:
                s.notes.append(f"reply names item(s) not in this proposal: {unknown}")
            approve &= all_items
            reject &= all_items
        s.approve, s.reject, s.mode = sorted(approve - reject), sorted(reject), mode
        if s.conditional and mode in CONFIDENT_MODES:
            s.notes.append("reads as conditional — treat as a request for another turn, "
                           "not an approval")
        return s

    # "all except 2 and 4" / "everything but 3"
    m = re.search(r"\b(?:all|everything|every one|the lot)\s+(?:except|but|besides|other than|apart from)\s+(.+)",
                  t)
    if m:
        skip = _numbers(m.group(1), n_max)
        return finish(all_items - skip, skip, MODE_ALL_EXCEPT)

    # a blanket no, checked before a blanket yes: "no, none of these" contains no numbers and
    # must not fall through to an affirmation match on a stray "ok".
    if any(r in t for r in vocab.rejections):
        return finish(set(), set(all_items), MODE_REJECTION)

    # "approve all" / "do all of them" / bare "all"
    approve_verbs, reject_verbs = _verb_group(vocab.approve), _verb_group(vocab.reject)
    if re.search(rf"\b(?:{approve_verbs})\b[^.!?]{{0,30}}\b(?:all|everything|the lot|them all)\b", t) \
            or t.strip(" .!") in {"all", "everything", "all of them", "them all"}:
        return finish(set(all_items), set(), MODE_ALL)

    reject_hits: Set[int] = set()
    for m in re.finditer(rf"\b(?:{reject_verbs})\b[^0-9]{{0,12}}{_NUMRUN}", t):
        reject_hits |= _numbers(m.group(1), n_max)

    approve_hits: Set[int] = set()
    for m in re.finditer(rf"\b(?:{approve_verbs})\b[^0-9]{{0,12}}{_NUMRUN}", t):
        approve_hits |= _numbers(m.group(1), n_max)
    # A rejection verb wins the number it names: "approve 1-4, skip 3" means three items.
    approve_hits -= reject_hits

    if approve_hits and reject_hits:
        return finish(approve_hits, reject_hits, MODE_EXPLICIT)
    if approve_hits:
        return finish(approve_hits, reject_hits, MODE_EXPLICIT)
    if reject_hits:
        # Only exclusions named. The safe reading is "everything else is fine" ONLY if the
        # reply looks like a sweep; otherwise it is a partial answer. We report skip-only and
        # let the caller decide — the loop does not auto-expand it into approvals.
        return finish(all_items - reject_hits, reject_hits, MODE_SKIP_ONLY)

    bare = _numbers(t, n_max)
    if bare:
        return finish(bare, set(), MODE_BARE_NUMBERS)

    if any(a in t for a in vocab.affirmations):
        return finish(set(all_items), set(), MODE_AFFIRMATION)

    return finish(set(), set(), MODE_UNPARSEABLE)
