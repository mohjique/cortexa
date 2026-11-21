"""Trial matching: shortlist (F9) and no-match plan (F10).

`match_case` is the workflow's matching hook. It geocodes the patient's ZIP
(rule 6: ZIP only), searches trials, runs the eligibility checker on every
trial and keeps those that pass within the default radius. A trial passes
when no criterion is `not_met` and none waits on a `pending` result
(interfaces.md). Remote trials only count in the widened search.

If nothing passes, it builds the no-match plan from
`trial_search.no_match_plan` in policy.yaml: widened radius (plus remote
trials), near misses, an in-app alert for the care team (rule 5: never the
patient) and a tumor board log entry.

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
    TumorBoardEntry,
    WidenedSearch,
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
# before a surgery that already happened. None can change, so it is not a
# near miss. The alert still catches new trials.
NOT_NEAR_MISS_FIELDS = frozenset(
    {"confirmed_bucket", "diagnosis", "stage", "recurrent", "pituitary.secreting_hormone"}
)


@dataclass
class Checked:
    trial: TrialRecord
    rows: list[EligibilityResult]
    site: NearestSite | None  # straight-line only until shortlisted

    @property
    def miles(self) -> float | None:
        return self.site.distance_miles if self.site else None

    def in_scope(self, radius: float, include_remote: bool) -> bool:
        if self.site is not None and self.site.distance_miles <= radius:
            return True
        return include_remote and self.trial.remote_participation


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sort_key(c: Checked) -> tuple[float, str]:
    return (c.miles if c.miles is not None else float("inf"), c.trial.trial_id)


def _shortlist_trial(c: Checked, origin: Origin, policy: Policy, case_id: str) -> ShortlistTrial:
    site = c.site
    if site is not None:
        # Drive time only for trials that are shown (one OSRM call each).
        site = geo.nearest_site(origin, c.trial.sites, case_id=case_id)
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


def _alert(rec: CaseRecord, radius: float, now: datetime) -> TrialAlert:
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
        query=AlertQuery(bucket=r.confirmed_bucket, markers=markers, zip=r.zip, radius_miles=radius),
        notify="care_team_in_app",
        created_at=now,
    )


def check_all(trials: list[TrialRecord], facts: Facts, origin: Origin, case_id: str) -> list[Checked]:
    out = []
    for t in trials:
        rows = check_trial(t, facts, case_id=case_id)
        site = geo.nearest_site(origin, t.sites, with_drive_time=False, case_id=case_id)
        if site is None and not t.remote_participation:
            audit("agent", "trial_skipped", {"trial_id": t.trial_id, "reason": "no site with coordinates"},
                  case_id=case_id)
            continue
        out.append(Checked(t, rows, site))
    return sorted(out, key=_sort_key)


def match_case(rec: CaseRecord, policy: Policy) -> Shortlist | NoMatchPlan:
    """Workflow matching hook: a shortlist, or the no-match plan."""
    r = rec.results
    if r is None:
        raise ValueError("matching needs doctor-entered results")
    case_id = rec.case_id
    ts = policy.trial_search
    nm = ts.no_match_plan
    radius = ts.default_radius_miles

    searched_at = _now()
    origin = geo.geocode_zip(r.zip, case_id=case_id)  # rule 6: ZIP only
    condition = CONDITION_FOR_BUCKET.get(r.confirmed_bucket, "brain tumor")
    wide = max(radius, nm.widened_radius_miles)
    trials = search_trials(condition=condition, origin=origin, radius_miles=wide, case_id=case_id)
    checked = check_all(trials, Facts.from_results(r), origin, case_id)

    hits = [c for c in checked if passes(c.rows) and c.in_scope(radius, include_remote=False)]
    if hits:
        shortlist = Shortlist(
            case_id=case_id,
            origin=origin,
            radius_miles=radius,
            statuses_searched=list(ts.statuses),
            trials=[_shortlist_trial(c, origin, policy, case_id) for c in hits],
        )
        audit("agent", "shortlist_built", {"trials": [t.trial_id for t in shortlist.trials],
                                           "radius_miles": radius}, case_id=case_id)
        return shortlist

    # --- no-match plan (F10) ---
    widened = [c for c in checked if passes(c.rows) and c.in_scope(wide, nm.include_remote_trials)]
    near_misses: list[NearMiss] = []
    if nm.show_near_misses:
        misses = (_near_miss(c) for c in checked
                  if not passes(c.rows) and c.in_scope(wide, nm.include_remote_trials))
        near_misses = [m for m in misses if m is not None]
    now = _now()
    alert = _alert(rec, wide, now) if nm.alert_on_new_trials else None
    board = (TumorBoardEntry(ts=now, case_id=case_id, entry="No current match", searched_at=searched_at)
             if nm.log_for_tumor_board else None)
    plan = NoMatchPlan(
        case_id=case_id,
        searched=SearchSummary(radius_miles=radius, statuses=list(ts.statuses), match_count=0),
        widened=WidenedSearch(
            radius_miles=wide,
            include_remote_trials=nm.include_remote_trials,
            trials=[_shortlist_trial(c, origin, policy, case_id) for c in widened],
        ),
        near_misses=near_misses,
        alert=alert,
        tumor_board_log=board,
    )
    if alert:
        audit("agent", "trial_alert_created", {"alert_id": alert.alert_id, "notify": alert.notify},
              case_id=case_id)
    if board:
        audit("agent", "tumor_board_log", {"entry": board.entry}, case_id=case_id)
    audit("agent", "no_match_plan_built", {
        "widened_trials": [t.trial_id for t in plan.widened.trials] if plan.widened else [],
        "near_misses": [m.trial_id for m in near_misses],
    }, case_id=case_id)
    return plan
