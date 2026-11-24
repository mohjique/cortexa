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

Location: only the case's ZIP is geocoded (rule 6). Distance is for sorting
and display only, never a filter: the search is nationwide and flags are
ordered nearest first, with no-site flags last. Without a ZIP, or if
geocoding fails, the screen still runs with no `nearest_site`.

`tests_needed` lists what the biopsy must answer. It reads the INCLUSION
bullets of every glioma trial (same filter as the flags) with
`agent.concepts.detect`, the module the eligibility checker uses, so the
screen and the checker read a criterion the same way. Per test:
- `trial_ids` / `decides_trials`: trials whose criterion states a readable
  requirement ("IDH-mutant", "MGMT unmethylated", "WHO grade 3-4").
- `mention_trial_ids` / `mentions_trials`: trials that mention the test but
  state no readable requirement ("IDH status will be collected"). Never
  counted as deciding (rule 3); shown as "check criteria".
- `evidence`: per trial, the criterion text and the requirement read (rule 4).
Tests are ordered by trials decided, then trials mentioning, descending.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from agent import geo
from agent.audit import audit
from agent.policy import Policy
from agent.concepts import detect
from agent.eligibility import split_criteria
from agent.schemas import (
    Origin,
    PreOpScreen,
    PreSurgeryTrial,
    TestEvidence,
    TestField,
    TestNeeded,
    TrialRecord,
)
from agent.store import CaseRecord
from agent.trials import search_trials

# A header line on its own, not "meets none of the exclusion criteria below".
_EXCLUSION_HEADER = re.compile(r"^\s*exclusion criteria:?\s*$", re.IGNORECASE | re.MULTILINE)


def _split_criteria(text: str) -> tuple[str, str]:
    """(inclusion section, exclusion section). No header = all inclusion."""
    parts = _EXCLUSION_HEADER.split(text, maxsplit=1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def _matching_line(section: str, matches: Callable[[str], str | None]) -> str | None:
    """The first criterion line `matches` accepts, cleaned up for display."""
    for line in section.splitlines():
        if matches(line):
            cleaned = line.strip().lstrip("*-• ").strip()
            if cleaned:
                return cleaned
    return None


def pre_surgery_criterion(trial: TrialRecord, policy: Policy) -> str | None:
    """The criterion text that marks this trial as enroll-before-surgery, or None.

    Same matching as the eligibility checker: whole-word phrases, and an
    inclusion phrase only in a line about enrolling or study treatment.
    """
    rules = policy.pre_surgery_detection
    inclusion, exclusion = _split_criteria(trial.criteria_text)
    return _matching_line(inclusion, rules.inclusion_phrase_in) or _matching_line(
        exclusion, rules.exclusion_phrase_in
    )


def _has_title_hint(trial: TrialRecord, policy: Policy) -> bool:
    title = trial.title.lower()
    return any(h in title for h in policy.pre_surgery_detection.title_hints)


def _is_glioma_trial(trial: TrialRecord, policy: Policy) -> bool:
    text = " ".join(trial.conditions + [trial.title]).lower()
    return any(w in text for w in policy.pre_surgery_detection.condition_keywords)


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
    # Nearest first; no known site (remote, no coordinates, no ZIP) goes last.
    found.sort(
        key=lambda f: (f.nearest_site is None, f.nearest_site.distance_miles if f.nearest_site else 0.0)
    )
    return found


# Biopsy tests the screen lists, in tie-break order, with their display labels.
TEST_LABELS: dict[TestField, str] = {
    "idh": "IDH mutation status",
    "codeletion_1p19q": "1p/19q codeletion",
    "mgmt": "MGMT promoter methylation",
    "grade": "WHO grade",
    "h3k27m": "H3 K27M alteration",
    "braf": "BRAF alteration",
}


def find_tests_needed(trials: list[TrialRecord], policy: Policy) -> list[TestNeeded]:
    """What the biopsy must answer, and which glioma trials each answer decides.

    Inclusion bullets only. A hit with a readable value is a requirement; a hit
    with value None only marks the trial as "mentions, check criteria".
    """
    # field -> trial_id -> {requires (or None): first criterion text citing it}
    cites: dict[TestField, dict[str, dict[str | None, str]]] = {f: {} for f in TEST_LABELS}
    for t in trials:
        if not _is_glioma_trial(t, policy):
            continue
        for c in split_criteria(t.criteria_text):
            if c.type != "inclusion":
                continue
            for hit in detect(c.text):
                if hit.name in cites:
                    per_trial = cites[hit.name].setdefault(t.trial_id, {})  # type: ignore[index]
                    per_trial.setdefault(hit.value, c.text)

    tests = []
    for field, by_trial in cites.items():
        deciding = sorted(tid for tid, vals in by_trial.items() if any(v is not None for v in vals))
        mentioning = sorted(tid for tid in by_trial if tid not in deciding)
        if not deciding and not mentioning:
            continue
        evidence = [
            TestEvidence(trial_id=tid, requires=req, criterion_text=text)
            for tid in deciding
            for req, text in by_trial[tid].items()
            if req is not None
        ] + [
            TestEvidence(trial_id=tid, requires=None, criterion_text=by_trial[tid][None])
            for tid in mentioning
        ]
        tests.append(
            TestNeeded(
                field=field,
                label=TEST_LABELS[field],
                current_value="pending",  # pre-op: no pathology yet
                decides_trials=len(deciding),
                trial_ids=deciding,
                mentions_trials=len(mentioning),
                mention_trial_ids=mentioning,
                evidence=evidence,
            )
        )
    order = list(TEST_LABELS)
    tests.sort(key=lambda x: (-x.decides_trials, -x.mentions_trials, order.index(x.field)))
    return tests


def tests_needed_summary(tests: list[TestNeeded]) -> dict[str, dict]:
    """Per-test counts for the audit log: trials deciding, mentioning, and per requirement."""
    out: dict[str, dict] = {}
    for t in tests:
        requires: dict[str, int] = {}
        for e in t.evidence:
            if e.requires is not None:
                requires[e.requires] = requires.get(e.requires, 0) + 1
        out[t.field] = {"decides": t.decides_trials, "mentions": t.mentions_trials, "requires": requires}
    return out


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
    # Nationwide on purpose: the ZIP only fills in nearest_site (no distance cutoff).
    trials = search_trials(case_id=rec.case_id, policy=policy)
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
    tests = find_tests_needed(trials, policy)
    audit(
        "agent",
        "tool_call:preop_tests_needed",
        {
            "glioma_trials": sum(_is_glioma_trial(t, policy) for t in trials),
            "tests": tests_needed_summary(tests),
        },
        case_id=rec.case_id,
    )
    return PreOpScreen(
        case_id=rec.case_id,
        trials_considered=len(trials),
        tests_needed=tests,
        pre_surgery_trials=flagged,
        label=f"{policy.classifier.label_prefix} glioma. Biopsy results pending.",
    )
