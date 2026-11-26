"""Shared pre-surgery phrase matching (agent/presurgery.py)."""

from __future__ import annotations

import pytest

from agent import presurgery
from agent.policy import get_policy


def rules():
    return get_policy().pre_surgery_detection


def test_context_word_must_be_near_the_phrase():
    far = " ".join(["word"] * (presurgery.CONTEXT_WINDOW_WORDS + 2))
    m = presurgery.inclusion_match(f"Preoperative MRI {far} no prior radiation therapy", rules())
    assert m is not None and not m.enrollment  # possible, not firm
    m = presurgery.inclusion_match("Preoperative study treatment for 7 days", rules())
    assert m is not None and m.enrollment


@pytest.mark.parametrize("text, phrase", [
    ("Treated preoperatively with the study drug", "preoperative"),
    ("Planned for pre-operative chemoradiotherapy", "pre-operative"),
    ("Pre-op MRI within 28 days", "pre-op"),
    ("Presurgically assessed", "presurgical"),
])
def test_phrase_matches_inflected_forms(text, phrase):
    m = presurgery.inclusion_match(text, rules())
    assert m is not None and m.phrase == phrase


@pytest.mark.parametrize("text", ["Preoperativeness", "Pre-operational readiness", "Prior resectionist"])
def test_phrase_does_not_match_inside_another_word(text):
    assert presurgery.inclusion_match(text, rules()) is None
    assert presurgery.exclusion_match(text, rules()) is None


def test_earliest_phrase_wins():
    text = "Neoadjuvant dosing begins before surgery"
    m = presurgery.inclusion_match(text, rules())
    assert m is not None and m.phrase == "neoadjuvant" and m.start == 0


def test_patterns_compile_once_per_phrase_list():
    presurgery._phrase_pattern.cache_clear()
    for _ in range(5):
        presurgery.inclusion_match("Must enroll before surgery", rules())
    assert presurgery._phrase_pattern.cache_info().misses == 1
