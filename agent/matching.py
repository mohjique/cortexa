"""Trial matching: shortlist (F9) and no-match plan (F10).

`match_case` is the workflow's matching hook. It geocodes the patient's ZIP
(rule 6: ZIP only), searches trials, runs the eligibility checker on every
trial and keeps every one that passes. A trial passes when no criterion is
`not_met` and none waits on a `pending` result (interfaces.md).

Passing trials are split into three groups, the way coordinators sort them:
"eligible" (any unknowns are routine screening-visit items), "needs_test"
(an unknown on a key field from `trial_search.key_unknown_fields`, such as
IDH or a CLIA TMB result; the group names the test that would decide it) and
"needs_review" (no clinical inclusion criterion was confirmed automatically,
only demographics such as age or exclusions that do not apply, so the doctor
must read the criteria; common for
live trials whose free text the rule library does not cover).

There is no distance cutoff. The search is nationwide, and distance only
sorts the shortlist: nearest first, trials with no known distance (remote-only,
or no site coordinates listed) last. No trial is dropped for lacking a
location. The patient and doctor decide whether travel is worth it. Drive
times come from one batched OSRM table request, not one request per trial.

If nothing passes, it builds the no-match plan from
`trial_search.no_match_plan` in policy.yaml: near misses, an in-app alert for
the care team (rule 5: never the patient) and a tumor board log entry.

The mutation check is not read here. The workflow attaches it to each
shortlist trial as context only (rule 10).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from agent import geo
from agent.audit import audit
from agent.eligibility import Facts, check_trial, is_pending, passes, summarize
from agent.policy import Policy
from agent.schemas import (
    AlertQuery,
    EligibilityResult,
    NearestSite,
    NearMiss,
    NoMatchPlan,
    Origin,
    SearchSummary,
    Shortlist,
    ShortlistTrial,
    TrialAlert,
    TrialRecord,
    TrialSite,
    TumorBoardEntry,
    SHORTLIST_GROUP_ORDER,
    distance_sort_key,
)
from agent.store import CaseRecord
from agent.trials import search_trials

# Live search term per confirmed bucket. Demo mode reads the fixed cache.
CONDITION_FOR_BUCKET = {
    "glioma": "glioma",
    "meningioma": "meningioma",
    "pituitary": "pituitary tumor",
}

# A trial that fails only on one of these is for a different tumor or
# disease stage (new vs recurrent, another hormone), or needed enrollment
# before a surgery that already happened, or enrolls only the other sex. None
# can change, so it is not a near miss. The alert still catches new trials.
NOT_NEAR_MISS_FIELDS = frozenset(
    {"confirmed_bucket", "diagnosis", "stage", "recurrent", "pituitary.secreting_hormone", "sex"}
)


# Rows on these fields are demographics, not clinical criteria: a trial whose
# only met rows are these goes to "needs_review", never "eligible".
DEMOGRAPHIC_FIELDS = frozenset({"age", "sex"})


@dataclass
class Checked:
    trial: TrialRecord
    rows: list[EligibilityResult]
    site: NearestSite | None  # straight-line only until shortlisted
    ok: bool  # eligibility.passes(rows), computed once
    best: tuple[float, TrialSite] | None = None  # closest site, for the drive-time batch


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sort_key(c: Checked) -> tuple[tuple[bool, float], str]:
    """The Shortlist schema's order (nearest first, unknown last), then trial id."""
    return (distance_sort_key(c.site), c.trial.trial_id)


def needs_test_fields(rows: list[EligibilityResult], key_fields: list[str]) -> list[str]:
    """Key fields (policy order) that some row leaves unknown."""
    unknown = {r.evidence_field for r in rows if r.status == "unknown"}
    return [f for f in key_fields if f in unknown]


def shortlist_group(rows: list[EligibilityResult], key_fields: list[str]) -> tuple[str, list[str]]:
    """(group, needs_test_fields) for a passing trial."""
    # Only a met inclusion is evidence the trial fits; a met exclusion just means it does not apply.
    if not any(r.status == "met" and r.criterion_type == "inclusion" and r.evidence_field not in DEMOGRAPHIC_FIELDS
               for r in rows):
        return "needs_review", []
    tests = needs_test_fields(rows, key_fields)
    return ("needs_test" if tests else "eligible"), tests


def _shortlist_trial(c: Checked, site: NearestSite | None, policy: Policy) -> ShortlistTrial:
    group, tests = shortlist_group(c.rows, policy.trial_search.key_unknown_fields)
    status = c.trial.recruitment_status
    return ShortlistTrial.model_validate(
        {
            "trial_id": c.trial.trial_id,
            "title": c.trial.title,
            "recruitment_status": status,
            "status_label": policy.trial_search.status_labels[status],
            "remote_participation": c.trial.remote_participation,
            "nearest_site": site,
            "summary": summarize(c.rows),
            "eligibility": c.rows,
            "group": group,
            "needs_test_fields": tests,
        },
        context=policy.status_context(),
    )


def _near_miss(c: Checked) -> NearMiss | None:
    failing = [r for r in c.rows if r.status == "not_met"]
    pending = [r for r in c.rows if is_pending(r)]
    if len(failing) == 1:
        if failing[0].evidence_field in NOT_NEAR_MISS_FIELDS:
            return None
        kind, blocking = "fails_one_rule", failing + pending
    elif not failing and pending:
        kind, blocking = "pending_result", pending
    else:
        return None
    return NearMiss(
        trial_id=c.trial.trial_id,
        title=c.trial.title,
        near_miss_type=kind,
        blocking=blocking,
        nearest_site=c.site,
    )


def _alert(rec: CaseRecord, now: datetime) -> TrialAlert:
    r = rec.results
    assert r is not None
    markers: dict[str, str | int] = {}
    for k in ("grade", "idh", "codeletion_1p19q", "mgmt"):
        v = getattr(r, k)
        if v not in (None, "pending", "unknown"):
            markers[k] = v
    if r.pituitary and r.pituitary.secreting_hormone not in ("pending", "unknown"):
        markers["secreting_hormone"] = r.pituitary.secreting_hormone
    return TrialAlert(
        alert_id=f"alert-{rec.case_id}-{now.strftime('%Y%m%d%H%M%S')}",
        query=AlertQuery(bucket=r.confirmed_bucket, markers=markers, zip=r.zip),
        notify="care_team_in_app",
        created_at=now,
    )


def check_all(trials: list[TrialRecord], facts: Facts, origin: Origin, policy: Policy,
              case_id: str) -> list[Checked]:
    out = []
    for t in trials:
        # No site coordinates: kept, with no distance, and sorted last (distance never filters).
        best = geo.closest(origin, t.sites)
        site = geo.as_nearest(best) if best else None
        rows = check_trial(t, facts, policy=policy, case_id=case_id)
        out.append(Checked(t, rows, site, passes(rows), best))
    return sorted(out, key=_sort_key)


def _with_drive_times(hits: list[Checked], origin: Origin, case_id: str) -> list[NearestSite | None]:
    """Nearest site per hit, with drive time from one batched OSRM request."""
    located = [c for c in hits if c.best is not None]
    minutes = geo.drive_minutes_many(origin, [c.best[1] for c in located], case_id=case_id)  # type: ignore[index]
    by_id = {c.trial.trial_id: geo.as_nearest(c.best, m) for c, m in zip(located, minutes)}  # type: ignore[arg-type]
    return [by_id.get(c.trial.trial_id) for c in hits]


def match_case(rec: CaseRecord, policy: Policy) -> Shortlist | NoMatchPlan:
    """Workflow matching hook: a shortlist, or the no-match plan."""
    r = rec.results
    if r is None:
        raise ValueError("matching needs doctor-entered results")
    case_id = rec.case_id
    ts = policy.trial_search
    nm = ts.no_match_plan

    searched_at = _now()
    origin = geo.geocode_zip(r.zip, case_id=case_id)  # rule 6: ZIP only; for distance, never a filter
    condition = CONDITION_FOR_BUCKET.get(r.confirmed_bucket, "brain tumor")
    trials = search_trials(condition=condition, case_id=case_id)  # nationwide
    checked = check_all(trials, Facts.from_results(r), origin, policy, case_id)

    hits = [c for c in checked if c.ok]
    if hits:
        shortlist = Shortlist(
            case_id=case_id,
            origin=origin,
            statuses_searched=list(ts.statuses),
            trials=sorted(
                (_shortlist_trial(c, s, policy) for c, s in zip(hits, _with_drive_times(hits, origin, case_id))),
                key=lambda t: (SHORTLIST_GROUP_ORDER[t.group], distance_sort_key(t.nearest_site),
                               t.trial_id),
            ),
        )
        audit("agent", "shortlist_built",
              {"trials": [t.trial_id for t in shortlist.trials],
               "needs_test": {t.trial_id: t.needs_test_fields for t in shortlist.trials
                              if t.group == "needs_test"}},
              case_id=case_id)
        return shortlist

    # --- no-match plan (F10): no eligible trial anywhere in the searched statuses ---
    near_misses: list[NearMiss] = []
    if nm.show_near_misses:
        near_misses = [m for m in (_near_miss(c) for c in checked) if m is not None]
    now = _now()
    alert = _alert(rec, now) if nm.alert_on_new_trials else None
    board = (TumorBoardEntry(ts=now, case_id=case_id, entry="No current match", searched_at=searched_at)
             if nm.log_for_tumor_board else None)
    plan = NoMatchPlan(
        case_id=case_id,
        searched=SearchSummary(statuses=list(ts.statuses), match_count=0),
        near_misses=near_misses,
        alert=alert,
        tumor_board_log=board,
    )
    if alert:
        audit("agent", "trial_alert_created", {"alert_id": alert.alert_id, "notify": alert.notify},
              case_id=case_id)
    if board:
        audit("agent", "tumor_board_log", {"entry": board.entry}, case_id=case_id)
    audit("agent", "no_match_plan_built", {"near_misses": [m.trial_id for m in near_misses]},
          case_id=case_id)
    return plan
