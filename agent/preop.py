"""Pre-op screen (glioma): flag trials that need enrollment before surgery.

Deterministic. Phrases come from `pre_surgery_detection` in policy.yaml, so
staff can change what counts without a code change (rule 8).

Two kinds of flag, both shown to the doctor and both needing an answer at the
`preop_review` gate:
- `matched_on: criteria`: a criterion line contains a policy phrase. That
  line is cited as `criterion_text` (rule 4).
- `matched_on: title_hint`: only the title suggests it (e.g. "neoadjuvant").
  Not confirmed, so the UI shows it as "possible, check criteria" and
  `criterion_text` cites the title. The window closes at surgery, so a
  possible trial is shown rather than dropped.

The case has no surgical history at pre-op, so the screen cannot rule a trial
out for "prior brain surgery". It flags the trial anyway and the doctor
decides at the gate (rule 3: unknown is never guessed).

Location: only the case's ZIP is geocoded (rule 6). Without a ZIP, or if
geocoding fails, the screen still runs with no `nearest_site`.

`tests_needed` is still empty: which biopsy answers decide which trials comes
with eligibility checking.
"""

from __future__ import annotations

import re

from agent import geo
from agent.audit import audit
from agent.policy import Policy
from agent.schemas import Origin, PreOpScreen, PreSurgeryTrial, TrialRecord
from agent.store import CaseRecord
from agent.trials import search_trials

# A header line on its own, not "meets none of the exclusion criteria below".
_EXCLUSION_HEADER = re.compile(r"^\s*exclusion criteria:?\s*$", re.IGNORECASE | re.MULTILINE)


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


def _has_title_hint(trial: TrialRecord, policy: Policy) -> bool:
    title = trial.title.lower()
    return any(h.lower() in title for h in policy.pre_surgery_detection.title_hints)


def _is_glioma_trial(trial: TrialRecord, policy: Policy) -> bool:
    words = [w.lower() for w in policy.pre_surgery_detection.condition_keywords]
    text = " ".join(trial.conditions + [trial.title]).lower()
    return any(w in text for w in words)


def find_pre_surgery_trials(
    trials: list[TrialRecord],
    policy: Policy,
    origin: Origin | None = None,
    case_id: str | None = None,
) -> list[PreSurgeryTrial]:
    found = []
    for t in trials:
        if not _is_glioma_trial(t, policy):
            continue
        criterion = pre_surgery_criterion(t, policy)
        if criterion is not None:
            matched_on, evidence = "criteria", criterion
        elif _has_title_hint(t, policy):
            matched_on, evidence = "title_hint", f"Title: {t.title}"
        else:
            continue
        site = geo.nearest_site(origin, t.sites, case_id=case_id) if origin else None
        found.append(
            PreSurgeryTrial(
                trial_id=t.trial_id,
                title=t.title,
                recruitment_status=t.recruitment_status,
                criterion_text=evidence,
                matched_on=matched_on,
                nearest_site=site,
            )
        )
    return found


def _origin(rec: CaseRecord) -> Origin | None:
    if rec.zip is None:
        return None
    try:
        return geo.geocode_zip(rec.zip, case_id=rec.case_id)
    except geo.GeoError as exc:
        # Location helps the doctor decide, but must not block the screen.
        audit("agent", "preop_location_unavailable", {"error": str(exc)}, case_id=rec.case_id)
        return None


def preop_screen_hook(rec: CaseRecord, policy: Policy) -> PreOpScreen:
    """Default pre-op hook. Searches trials (demo cache unless live) and flags pre-surgery ones."""
    origin = _origin(rec)
    radius = policy.trial_search.default_radius_miles if origin else None
    trials = search_trials(origin=origin, radius_miles=radius, case_id=rec.case_id, policy=policy)
    flagged = find_pre_surgery_trials(trials, policy, origin, rec.case_id)
    audit(
        "agent",
        "tool_call:pre_surgery_detection",
        {
            "trials_searched": len(trials),
            "flagged": [t.trial_id for t in flagged if t.matched_on == "criteria"],
            "possible": [t.trial_id for t in flagged if t.matched_on == "title_hint"],
        },
        case_id=rec.case_id,
    )
    return PreOpScreen(
        case_id=rec.case_id,
        trials_considered=len(trials),
        tests_needed=[],
        pre_surgery_trials=flagged,
        label=f"{policy.classifier.label_prefix} glioma. Biopsy results pending.",
    )
