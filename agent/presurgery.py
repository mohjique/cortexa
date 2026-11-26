"""Pre-surgery phrase matching, shared by the pre-op screen and the eligibility checker.

Both read a criterion bullet the same way, so a bullet the screen flags is the
bullet the checker treats as pre-surgery, and the reverse (`agent.preop`,
`agent.eligibility`). Phrases come from `pre_surgery_detection` in policy.yaml
(rule 8).

Matching leans toward flagging. A missed flag means the doctor is never told
about a trial that needs enrollment before surgery, and that window closes at
surgery. A false flag costs the doctor one answer at the `preop_review` gate,
and after pathology the checker only ever makes a pre-surgery bullet
`unknown`, never `not_met`, so a false flag cannot hide a trial.

- A phrase matches whole words plus a common suffix: "preoperative" matches
  "preoperatively", "prior resection" matches "prior resections", "prior
  brain surgery" matches "prior brain surgeries", but "prior resection" does
  not match "prior resectionist".
- An inclusion phrase with enrollment or study-treatment wording
  (`inclusion_context_words`) within `CONTEXT_WINDOW_WORDS` words is firm
  (`enrollment=True`). Without it the match still counts, as possible:
  "Preoperatively, the lesion must be >= 1 cm" may time a measurement, or it
  may be a window-of-opportunity trial that doses before surgery. Only the
  doctor can tell.
- Context words match anywhere inside a word ("therap" in "radiotherapy",
  "treat" in "treated"), since the window already keeps them local.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from agent.policy import PreSurgeryPolicy

# How many words either side of a phrase are searched for enrollment wording.
CONTEXT_WINDOW_WORDS = 8

# Suffixes a phrase may take and still match: -ly, -s, -es.
_SUFFIX = r"(?:ly|e?s)?"
_WORD = re.compile(r"\w+")


@dataclass(frozen=True)
class PhraseMatch:
    phrase: str  # the policy phrase, as listed
    start: int  # span of the matched text in the bullet
    end: int
    enrollment: bool  # enrollment wording nearby (always True for an exclusion phrase)


def _inflected(phrase: str) -> str:
    """`phrase` as a pattern that also takes a suffix ("surgery" -> "surgeries" too)."""
    if phrase.endswith("y"):
        return f"{re.escape(phrase[:-1])}(?:y|ies)"
    return f"{re.escape(phrase)}{_SUFFIX}"


@lru_cache(maxsize=64)
def _phrase_pattern(phrases: tuple[str, ...]) -> re.Pattern[str]:
    """One compiled pattern per phrase list; list order decides which phrase wins."""
    alts = "|".join(f"(?P<p{i}>{_inflected(p)})" for i, p in enumerate(phrases))
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.IGNORECASE)


@lru_cache(maxsize=64)
def _context_pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile("|".join(re.escape(w) for w in words), re.IGNORECASE)


def _first(text: str, phrases: list[str]) -> tuple[str, int, int] | None:
    """The earliest phrase in `text` as (phrase, start, end); ties go to list order."""
    m = _phrase_pattern(tuple(phrases)).search(text)
    if m is None:
        return None
    i = next(int(k[1:]) for k, v in m.groupdict().items() if v is not None)
    return phrases[i], m.start(), m.end()


def _window(text: str, start: int, end: int) -> str:
    """The phrase plus `CONTEXT_WINDOW_WORDS` words on each side."""
    words = list(_WORD.finditer(text))
    before = [w for w in words if w.end() <= start]
    after = [w for w in words if w.start() >= end]
    lo = before[-CONTEXT_WINDOW_WORDS].start() if len(before) >= CONTEXT_WINDOW_WORDS else 0
    hi = after[CONTEXT_WINDOW_WORDS - 1].end() if len(after) >= CONTEXT_WINDOW_WORDS else len(text)
    return text[lo:hi]


def inclusion_match(text: str, rules: PreSurgeryPolicy) -> PhraseMatch | None:
    """A pre-surgery phrase in an inclusion bullet, firm or possible, or None."""
    hit = _first(text, rules.inclusion_phrases)
    if hit is None:
        return None
    phrase, start, end = hit
    context = _context_pattern(tuple(rules.inclusion_context_words))
    near = context.search(_window(text, start, end)) is not None
    return PhraseMatch(phrase, start, end, enrollment=near)


def exclusion_match(text: str, rules: PreSurgeryPolicy) -> PhraseMatch | None:
    """A pre-surgery phrase in an exclusion bullet ("prior resection"), or None.

    An exclusion phrase says the trial needs an untouched tumor, so it is firm
    on its own.
    """
    hit = _first(text, rules.exclusion_phrases)
    if hit is None:
        return None
    phrase, start, end = hit
    return PhraseMatch(phrase, start, end, enrollment=True)
