"""Reply parsing. The bar is not "understands English" — it is "is right when it claims to be
confident, and says so when it is not"."""
from __future__ import annotations

import pytest

from detached import Vocabulary, interpret
from detached.interpret import (MODE_AFFIRMATION, MODE_ALL, MODE_ALL_EXCEPT, MODE_BARE_NUMBERS,
                                MODE_EMPTY, MODE_EXPLICIT, MODE_REJECTION, MODE_SKIP_ONLY,
                                MODE_UNPARSEABLE)


def test_explicit_approve_and_skip():
    s = interpret("approve 1 and 3, skip 2", 4)
    assert s.approve == [1, 3]
    assert s.reject == [2]
    assert s.mode == MODE_EXPLICIT
    assert s.confident


def test_all_except():
    s = interpret("all except 2 and 4", 5)
    assert s.approve == [1, 3, 5]
    assert s.reject == [2, 4]
    assert s.mode == MODE_ALL_EXCEPT


def test_bare_all():
    assert interpret("all", 3).approve == [1, 2, 3]
    assert interpret("approve all of them", 3).mode == MODE_ALL


def test_ranges():
    s = interpret("approve 1-4, skip 3", 6)
    assert s.approve == [1, 2, 4]
    assert s.reject == [3]


def test_range_words():
    assert interpret("do 2 through 5", 6).approve == [2, 3, 4, 5]


def test_verb_boundary_does_not_eat_the_next_word():
    """The regression that a character class `[0-9, and]+` causes: it swallows the leading
    letters of the next verb, so "post 1, article 2" loses item 2 entirely."""
    vocab = Vocabulary(approve=("post", "article", "approve"), reject=("skip",))
    s = interpret("post 1, article 2", 3, vocabulary=vocab)
    assert s.approve == [1, 2]


def test_rejection_verb_wins_a_contested_number():
    s = interpret("approve 1,2,3 but skip 2", 3)
    assert 2 not in s.approve
    assert s.reject == [2]


def test_skip_only_is_reported_as_such():
    """Only exclusions named. The parser reports the reading; it is the caller's call whether
    "skip 2" means "and do the rest"."""
    s = interpret("skip 2", 4)
    assert s.mode == MODE_SKIP_ONLY
    assert s.reject == [2]


def test_conditional_is_never_confident():
    """The most important negative case. "approve 1 but reword it" is a request for another
    draft. Reading it as a yes ships text nobody agreed to."""
    s = interpret("approve 1 but can you reword the second line first", 3)
    assert s.conditional
    assert not s.confident
    assert any("conditional" in n for n in s.notes)


def test_question_is_conditional():
    s = interpret("what about the third one? not sure it's right", 3)
    assert s.conditional
    assert not s.confident


def test_affirmation_alone_is_not_confident():
    """"Sounds good" answering a forty-item digest is politeness, not forty decisions."""
    s = interpret("looks good to me", 40)
    assert s.mode == MODE_AFFIRMATION
    assert s.approve == list(range(1, 41))
    assert not s.confident


def test_blanket_rejection_beats_a_stray_ok():
    s = interpret("no thanks, none of these are ok right now", 3)
    assert s.mode == MODE_REJECTION
    assert s.approve == []
    assert s.reject == [1, 2, 3]


def test_bare_numbers_are_flagged_low_confidence():
    s = interpret("2", 4)
    assert s.mode == MODE_BARE_NUMBERS
    assert s.approve == [2]
    assert not s.confident, "a lone number is as likely to mean 'let's talk about 2'"


def test_out_of_range_numbers_are_dropped_not_kept():
    s = interpret("approve 2, we sent 2400 emails last week", 5)
    assert s.approve == [2]


def test_valid_set_reports_unknown_items():
    s = interpret("approve 1 and 7", 9, valid=[1, 2, 3])
    assert s.approve == [1]
    assert any("not in this proposal" in n for n in s.notes)


def test_empty_and_unparseable():
    assert interpret("", 3).mode == MODE_EMPTY
    assert interpret("thanks!!", 3).mode == MODE_UNPARSEABLE
    assert not interpret("thanks!!", 3).confident


def test_custom_vocabulary_matches_the_domain():
    vocab = Vocabulary(approve=("merge", "ship"), reject=("revert", "hold"))
    s = interpret("merge 1 and 2, hold 3", 3, vocabulary=vocab)
    assert s.approve == [1, 2]
    assert s.reject == [3]


def test_empty_vocabulary_is_refused():
    with pytest.raises(ValueError):
        Vocabulary(approve=())
