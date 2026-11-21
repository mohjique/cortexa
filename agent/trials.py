"""Trial search: ClinicalTrials.gov + NCI clients, normalize, merge (F7.1-F7.3).

Both APIs are normalized into `TrialRecord`. ClinicalTrials.gov is the primary
source (status, criteria text, sites); NCI fills gaps and marks the record
`ctgov+nci`. Statuses come from `trial_search.statuses` in policy.yaml and are
filtered here too, so a policy edit changes results even on cached data.
"""

from __future__ import annotations

import os
import re
from typing import Any

from pydantic import ValidationError

from agent import http_cache
from agent.audit import audit, audited
from agent.policy import Policy, get_policy
from agent.schemas import TrialRecord, TrialSite

CTGOV_URL = "https://clinicaltrials.gov/api/v2/studies"
NCI_URL = "https://clinicaltrialsapi.cancer.gov/api/v2/trials"
CTGOV_PAGE_SIZE = 100
CTGOV_MAX_PAGES = 3
DEFAULT_CONDITION = "brain tumor"

# NCI uses its own status words. Map to the ClinicalTrials.gov enum the policy uses.
NCI_STATUS_MAP: dict[str, str] = {
    "Active": "RECRUITING",
    "Approved": "NOT_YET_RECRUITING",
    "Enrolling by Invitation": "ENROLLING_BY_INVITATION",
    "Temporarily Closed to Accrual": "SUSPENDED",
    "Closed to Accrual": "ACTIVE_NOT_RECRUITING",
    "Completed": "COMPLETED",
}

# Neither API has a structured "remote" flag. Match on title + criteria.
# TODO(policy): move to policy.yaml once the premed teammate agrees the wording.
REMOTE_PATTERNS = (
    r"\bfully remote\b",
    r"\bremote participation\b",
    r"\bdecentrali[sz]ed\b",
    r"\bhome participation\b",
    r"\bparticipat\w* (?:entirely |fully )?from home\b",
)
_REMOTE_RE = re.compile("|".join(REMOTE_PATTERNS), re.IGNORECASE)
_AGE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(year|month|week|day)s?\s*$", re.IGNORECASE)
_AGE_DIVISOR = {"year": 1, "month": 12, "week": 52.1775, "day": 365.25}


def parse_age_years(text: str | None) -> float | None:
    """'18 Years' -> 18.0, '6 Months' -> 0.5. Unparseable -> None (no limit known)."""
    if not text:
        return None
    m = _AGE_RE.match(text)
    if not m:
        return None
    return round(float(m.group(1)) / _AGE_DIVISOR[m.group(2).lower()], 2)


def is_remote(*texts: str) -> bool:
    return any(_REMOTE_RE.search(t or "") for t in texts)


# ---------------------------------------------------------------------------
# ClinicalTrials.gov
# ---------------------------------------------------------------------------


def _ctgov_sites(locations: list[dict[str, Any]], statuses: set[str]) -> list[TrialSite]:
    sites = []
    for loc in locations:
        # A closed site cannot enroll, so it must not become the nearest site.
        if loc.get("status") and loc["status"] not in statuses:
            continue
        if not loc.get("facility") or not loc.get("city"):
            continue
        geo = loc.get("geoPoint") or {}
        sites.append(
            TrialSite(
                facility=loc["facility"],
                city=loc["city"],
                state=loc.get("state"),
                zip=loc.get("zip"),
                lat=geo.get("lat"),
                lon=geo.get("lon"),
            )
        )
    return sites


def normalize_ctgov(study: dict[str, Any], fetched: http_cache.Fetched, policy: Policy) -> TrialRecord:
    ps = study["protocolSection"]
    ident = ps["identificationModule"]
    elig = ps.get("eligibilityModule", {})
    title = ident.get("briefTitle") or ident.get("officialTitle") or ident["nctId"]
    criteria = elig.get("eligibilityCriteria", "")
    return TrialRecord.model_validate(
        {
            "trial_id": ident["nctId"],
            "title": title,
            "source": "ctgov",
            "recruitment_status": ps["statusModule"]["overallStatus"],
            "phases": ps.get("designModule", {}).get("phases", []),
            "conditions": ps.get("conditionsModule", {}).get("conditions", []),
            "criteria_text": criteria,
            "min_age_years": parse_age_years(elig.get("minimumAge")),
            "max_age_years": parse_age_years(elig.get("maximumAge")),
            "remote_participation": is_remote(title, criteria),
            "sites": _ctgov_sites(
                ps.get("contactsLocationsModule", {}).get("locations", []),
                set(policy.trial_search.statuses),
            ),
            "fetched_at": fetched.fetched_at,
            "from_cache": fetched.from_cache,
        },
        context=policy.status_context(),
    )


def fetch_ctgov(
    condition: str,
    policy: Policy,
    case_id: str | None = None,
) -> tuple[list[dict[str, Any]], http_cache.Fetched]:
    # No filter.geo: there is no distance cutoff (every eligible trial is shown).
    params: dict[str, Any] = {
        "query.cond": condition,
        "filter.overallStatus": ",".join(policy.trial_search.statuses),
        "pageSize": CTGOV_PAGE_SIZE,
    }
    studies: list[dict[str, Any]] = []
    first: http_cache.Fetched | None = None
    for _ in range(CTGOV_MAX_PAGES):
        got = http_cache.get_json(
            "ctgov", CTGOV_URL, params, demo_file="ctgov/studies_brain_tumor.json", case_id=case_id
        )
        first = first or got
        studies.extend(got.data.get("studies", []))
        token = got.data.get("nextPageToken")
        if not token or got.origin == "demo_cache":
            break
        params = {**params, "pageToken": token}
    assert first is not None
    return studies, first


# ---------------------------------------------------------------------------
# NCI Clinical Trials Search
# ---------------------------------------------------------------------------


def _nci_criteria_text(unstructured: list[dict[str, Any]]) -> str:
    inc = [u["description"] for u in unstructured if u.get("inclusion_indicator")]
    exc = [u["description"] for u in unstructured if not u.get("inclusion_indicator")]
    parts = []
    if inc:
        parts.append("Inclusion Criteria:\n\n" + "\n".join(f"* {d}" for d in inc))
    if exc:
        parts.append("Exclusion Criteria:\n\n" + "\n".join(f"* {d}" for d in exc))
    return "\n\n".join(parts)


def _nci_age(value: Any) -> float | None:
    if value is None:
        return None
    value = float(value)
    return None if value >= 999 else value  # NCI uses 999 for "no maximum"


def normalize_nci(trial: dict[str, Any], fetched: http_cache.Fetched, policy: Policy) -> TrialRecord | None:
    """None when NCI's status has no ClinicalTrials.gov equivalent we know."""
    status = NCI_STATUS_MAP.get(trial.get("current_trial_status", ""))
    if status is None:
        return None
    elig = trial.get("eligibility", {})
    structured = elig.get("structured", {})
    criteria = _nci_criteria_text(elig.get("unstructured", []))
    title = trial.get("brief_title") or trial["nct_id"]
    sites = []
    for s in trial.get("sites", []):
        if not s.get("org_name") or not s.get("org_city"):
            continue
        coords = s.get("org_coordinates") or {}
        sites.append(
            TrialSite(
                facility=s["org_name"],
                city=s["org_city"],
                state=s.get("org_state_or_province"),
                zip=s.get("org_postal_code"),
                lat=coords.get("lat"),
                lon=coords.get("lon"),
            )
        )
    return TrialRecord.model_validate(
        {
            "trial_id": trial["nct_id"],
            "title": title,
            "source": "nci",
            "recruitment_status": status,
            "phases": [trial["phase"]] if isinstance(trial.get("phase"), str) else [],
            "conditions": [d["name"] for d in trial.get("diseases", []) if d.get("name")],
            "criteria_text": criteria,
            "min_age_years": _nci_age(structured.get("min_age_in_years")),
            "max_age_years": _nci_age(structured.get("max_age_in_years")),
            "remote_participation": is_remote(title, criteria),
            "sites": sites,
            "fetched_at": fetched.fetched_at,
            "from_cache": fetched.from_cache,
        },
        context=policy.status_context(),
    )


def fetch_nci(condition: str, case_id: str | None = None) -> tuple[list[dict[str, Any]], http_cache.Fetched] | None:
    """None in live mode when there is no NCI_API_KEY (NCI is optional)."""
    headers = {}
    if http_cache.source_mode() == "live":
        key = os.environ.get("NCI_API_KEY")
        if not key:
            audit("agent", "nci_skipped", {"reason": "NCI_API_KEY not set"}, case_id)
            return None
        headers["X-API-KEY"] = key
    params = {"keyword": condition, "current_trial_status": ["Active", "Approved"], "size": 50}
    got = http_cache.get_json(
        "nci", NCI_URL, params, headers=headers, demo_file="nci/trials_brain_tumor.json", case_id=case_id
    )
    return got.data.get("data", []), got


# ---------------------------------------------------------------------------
# Merge + search
# ---------------------------------------------------------------------------


def merge(ctgov: list[TrialRecord], nci: list[TrialRecord]) -> list[TrialRecord]:
    """One record per trial_id. ClinicalTrials.gov wins; NCI fills empty fields."""
    by_id: dict[str, TrialRecord] = {t.trial_id: t for t in ctgov}
    for n in nci:
        c = by_id.get(n.trial_id)
        if c is None:
            by_id[n.trial_id] = n
            continue
        by_id[n.trial_id] = c.model_copy(
            update={
                "source": "ctgov+nci",
                "criteria_text": c.criteria_text or n.criteria_text,
                "conditions": c.conditions or n.conditions,
                "phases": c.phases or n.phases,
                "min_age_years": c.min_age_years if c.min_age_years is not None else n.min_age_years,
                "max_age_years": c.max_age_years if c.max_age_years is not None else n.max_age_years,
                "remote_participation": c.remote_participation or n.remote_participation,
                "sites": c.sites or n.sites,
                "from_cache": c.from_cache and n.from_cache,
            }
        )
    return sorted(by_id.values(), key=lambda t: t.trial_id)


def _normalize_all(raw: list[dict[str, Any]], fn: Any, fetched: http_cache.Fetched, policy: Policy,
                   source: str, case_id: str | None) -> list[TrialRecord]:
    out = []
    for item in raw:
        try:
            rec = fn(item, fetched, policy)
        except (ValidationError, KeyError, TypeError, ValueError) as exc:
            # Out-of-policy status or malformed record: drop it, but say so.
            audit("agent", "trial_dropped", {"source": source, "error": str(exc)[:300]}, case_id)
            continue
        if rec is not None:
            out.append(rec)
    return out


@audited("trial_search")
def search_trials(
    condition: str = DEFAULT_CONDITION,
    case_id: str | None = None,
) -> list[TrialRecord]:
    """Recruiting trials in policy statuses, merged across both APIs.

    Nationwide: there is no distance cutoff. Callers compute distance with
    geo.nearest_site to sort and label trials, never to drop them.
    """
    policy = get_policy()
    raw, fetched = fetch_ctgov(condition, policy, case_id=case_id)
    ctgov = _normalize_all(raw, normalize_ctgov, fetched, policy, "ctgov", case_id)
    nci: list[TrialRecord] = []
    got = fetch_nci(condition, case_id)
    if got is not None:
        nci = _normalize_all(got[0], normalize_nci, got[1], policy, "nci", case_id)
    return merge(ctgov, nci)
