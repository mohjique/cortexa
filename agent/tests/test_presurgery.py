"""Shared pre-surgery phrase matching (agent/presurgery.py)."""

from __future__ import annotations

import pytest

from agent import presurgery
from agent.policy import get_policy


def rules():
    return get_policy().pre_surgery_detection


def test_context_word_must_be_near_the_phrase():
    far = " ".join(["word"] * (rules().inclusion_context_window_words + 2))
    m = presurgery.inclusion_match(f"Preoperative MRI {far} no prior radiation therapy", rules())
    assert m is not None and not m.enrollment  # possible, not firm
    m = presurgery.inclusion_match("Preoperative study treatment for 7 days", rules())
    assert m is not None and m.enrollment


@pytest.mark.parametrize("text", [
    "Before planned surgical resection, MRI within 14 days",  # "planned" is inside the phrase
    "Window of opportunity cohort: MRI within 14 days",  # "window" is inside the phrase
    "Neoadjuvant MRI within 14 days",  # the phrase is a context word too
    "Pre-operative imaging must show a lesion; untreated patients",  # "treat" only inside "untreated"
    "Preoperative pretreatment labs",
])
def test_phrase_is_not_its_own_context_and_words_match_at_word_start(text):
    m = presurgery.inclusion_match(text, rules())
    assert m is not None and not m.enrollment


@pytest.mark.parametrize("text", [
    "Patients must be planned for preoperative chemoradiotherapy",
    "Treated preoperatively with radiotherapy",
    "Neoadjuvant immunotherapy for 2 weeks",
])
def test_listed_compounds_still_count_as_enrollment_wording(text):
    m = presurgery.inclusion_match(text, rules())
    assert m is not None and m.enrollment


def test_window_size_comes_from_policy():
    """Rule 8: staff can widen or narrow the window in policy.yaml."""
    from agent import policy as pol
    text = "Preoperative MRI one two three four five study treatment"  # 5 words between
    assert presurgery.inclusion_match(text, rules()).enrollment
    narrow = pol.POLICY_PATH.read_text(encoding="utf-8").replace(
        "inclusion_context_window_words: 8", "inclusion_context_window_words: 3")
    assert presurgery.inclusion_match(text, pol.parse_policy(narrow).pre_surgery_detection).enrollment is False


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
