"""Pre-op screen (glioma): flag trials that need enrollment before surgery.

Deterministic. Phrases come from `pre_surgery_detection` in policy.yaml, so
staff can change what counts without a code change (rule 8). Every flag cites
the criterion line it matched (rule 4). A title hint alone never flags a trial.

The case has no surgical history at pre-op, so the screen cannot rule a trial
out for "prior brain surgery". It flags the trial anyway and the doctor
decides at the `preop_review` gate (rule 3: unknown is never guessed).

`tests_needed` is still empty: which biopsy answers decide which trials comes
with eligibility checking.
"""

from __future__ import annotations

import re

from agent.audit import audit
from agent.policy import Policy
from agent.schemas import PreOpScreen, PreSurgeryTrial, TrialRecord
from agent.store import CaseRecord
from agent.trials import search_trials

_EXCLUSION_HEADER = re.compile(r"exclusion criteria:?", re.IGNORECASE)


def _split_criteria(text: str) -> tuple[str, str]:
    """(inclusion section, exclusion section). No header = all inclusion."""
    parts = _EXCLUSION_HEADER.split(text, maxsplit=1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def _matching_line(section: str, phrases: list[str]) -> str | None:
    """The first criterion line containing any phrase, cleaned up for display."""
    for line in section.splitlines():
        low = line.lower()
        if any(p.lower() in low for p in phrases):
            cleaned = line.strip().lstrip("*-• ").strip()
            if cleaned:
                return cleaned
    return None


def pre_surgery_criterion(trial: TrialRecord, policy: Policy) -> str | None:
    """The criterion text that marks this trial as enroll-before-surgery, or None."""
    rules = policy.pre_surgery_detection
    inclusion, exclusion = _split_criteria(trial.criteria_text)
    return _matching_line(inclusion, rules.inclusion_phrases) or _matching_line(
        exclusion, rules.exclusion_phrases
    )


def _is_glioma_trial(trial: TrialRecord, policy: Policy) -> bool:
    words = [w.lower() for w in policy.pre_surgery_detection.condition_keywords]
    text = " ".join(trial.conditions + [trial.title]).lower()
    return any(w in text for w in words)


def find_pre_surgery_trials(trials: list[TrialRecord], policy: Policy) -> list[PreSurgeryTrial]:
    found = []
    for t in trials:
        if not _is_glioma_trial(t, policy):
            continue
        criterion = pre_surgery_criterion(t, policy)
        if criterion is None:
            continue
        found.append(
            PreSurgeryTrial(
                trial_id=t.trial_id,
                title=t.title,
                recruitment_status=t.recruitment_status,
                criterion_text=criterion,
            )
        )
    return found


def preop_screen_hook(rec: CaseRecord, policy: Policy) -> PreOpScreen:
    """Default pre-op hook. Searches trials (demo cache unless live) and flags pre-surgery ones."""
    trials = search_trials(case_id=rec.case_id)
    flagged = find_pre_surgery_trials(trials, policy)
    audit(
        "agent",
        "tool_call:pre_surgery_detection",
        {"trials_searched": len(trials), "flagged": [t.trial_id for t in flagged]},
        case_id=rec.case_id,
    )
    return PreOpScreen(
        case_id=rec.case_id,
        trials_considered=len(trials),
        tests_needed=[],
        pre_surgery_trials=flagged,
        label=f"{policy.classifier.label_prefix} glioma. Biopsy results pending.",
    )
