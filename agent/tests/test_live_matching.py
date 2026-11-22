"""Live-data matching fixes: registry age limits, the needs_review group,
trials without site coordinates, and batched OSRM drive times."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from agent import geo, http_cache, matching, policy
from agent.eligibility import Facts, check_trial
from agent.schemas import Origin, Shortlist, ShortlistTrial, TrialRecord, TrialSite

FIXTURES = Path(__file__).parent / "fixtures"
ORIGIN = Origin(zip="77840", lat=30.628, lon=-96.3344)
SITE_A = TrialSite(facility="Site A", city="Houston", state="TX", lat=29.7079, lon=-95.4012)
SITE_B = TrialSite(facility="Site B", city="Dallas", state="TX", lat=32.8121, lon=-96.8383)


def trial(**over) -> TrialRecord:
    base = json.loads((FIXTURES / "trial_record.json").read_text(encoding="utf-8"))
    return TrialRecord.model_validate(base | over)


def facts(age: int | None = 63) -> Facts:
    return Facts(stage="post_pathology", bucket="glioma", age=age)


@pytest.fixture
def live(monkeypatch, tmp_path):
    monkeypatch.setenv("CORTEXA_TRIAL_SOURCE", "live")
    monkeypatch.setattr(http_cache, "LIVE_CACHE_DIR", tmp_path / "cache")
    calls: list[httpx.Request] = []
    state = {"handler": lambda req: httpx.Response(503)}

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return state["handler"](req)

    http_cache.set_client_factory(lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    yield state, calls
    http_cache.set_client_factory(None)


# --- fix 1: structured registry age limits ----------------------------------------


@pytest.mark.parametrize("lo,hi,age,want", [
    (None, 19, 63, "not_met"),   # pediatric trial, adult patient
    (18, None, 63, "met"),
    (18, 75, 80, "not_met"),
    (0.5, 25, 20, "met"),        # "6 Months" parsed to 0.5 years
    (18, None, None, "unknown"),  # never guessed (rule 3)
])
def test_registry_age_limits_become_a_row(lo, hi, age, want):
    rows = check_trial(trial(min_age_years=lo, max_age_years=hi, criteria_text=""), facts(age))
    (row,) = [r for r in rows if r.evidence_field == "age"]
    assert row.status == want
    assert "trial registry age limits" in row.criterion_text  # rule 4: cites what it read


def test_no_registry_age_row_when_text_already_decides_age():
    text = "Inclusion Criteria:\n* Age 18 years or older\n"
    rows = check_trial(trial(min_age_years=18, criteria_text=text), facts())
    age_rows = [r for r in rows if r.evidence_field == "age"]
    assert len(age_rows) == 1 and "registry" not in age_rows[0].criterion_text


def test_no_registry_age_row_without_limits():
    rows = check_trial(trial(min_age_years=None, max_age_years=None, criteria_text=""), facts())
    assert all(r.evidence_field != "age" for r in rows)


# --- fix 2: needs_review when no clinical criterion was confirmed ----------------


def test_only_age_met_is_needs_review_not_eligible():
    text = "Inclusion Criteria:\n* Something the rule library does not cover\n"
    rows = check_trial(trial(min_age_years=18, criteria_text=text), facts())
    group, tests = matching.shortlist_group(rows, policy.get_policy().trial_search.key_unknown_fields)
    assert (group, tests) == ("needs_review", [])


def test_met_exclusion_alone_is_needs_review():
    """'No prior bevacizumab' passing an exclusion says nothing about fit."""
    text = "Exclusion Criteria:\n* Prior bevacizumab\n"
    f = Facts(stage="post_pathology", bucket="glioma", age=63, prior_treatments=())
    rows = check_trial(trial(min_age_years=18, criteria_text=text), f)
    assert any(r.status == "met" and r.criterion_type == "exclusion" for r in rows)
    group, _ = matching.shortlist_group(rows, policy.get_policy().trial_search.key_unknown_fields)
    assert group == "needs_review"


def test_needs_review_sorts_after_other_groups():
    sl = json.loads((FIXTURES / "shortlist.json").read_text(encoding="utf-8"))
    first = sl["trials"][0]
    review = first | {"trial_id": "NCT99000098", "group": "needs_review", "needs_test_fields": [],
                      "eligibility": [r | {"trial_id": "NCT99000098"} for r in first["eligibility"]]}
    Shortlist.model_validate(sl | {"trials": [first, review]})
    with pytest.raises(ValueError, match="grouped"):
        Shortlist.model_validate(sl | {"trials": [review, first]})


# --- fix 4: no site coordinates is kept, sorted last ------------------------------


def test_trial_without_coordinates_is_kept_and_sorted_last():
    pol = policy.get_policy()
    near = trial(trial_id="NCT99000090", criteria_text="")
    nowhere = trial(trial_id="NCT99000091", criteria_text="",
                    sites=[{"facility": "Site X", "city": "Austin", "state": "TX"}])
    checked = matching.check_all([nowhere, near], facts(), ORIGIN, pol, case_id="t")
    assert [c.trial.trial_id for c in checked] == ["NCT99000090", "NCT99000091"]
    assert checked[1].site is None and checked[1].best is None


def test_shortlist_trial_allows_no_site_when_not_remote():
    sl = json.loads((FIXTURES / "shortlist.json").read_text(encoding="utf-8"))
    t = sl["trials"][0] | {"nearest_site": None, "remote_participation": False}
    ShortlistTrial.model_validate(t)


# --- fix 3: one OSRM table request for many sites ---------------------------------


def test_drive_times_one_table_request(live):
    state, calls = live
    state["handler"] = lambda req: httpx.Response(
        200, json={"code": "Ok", "durations": [[0, 6000, None]]})
    nowhere = TrialSite(facility="Site X", city="Austin")
    out = geo.drive_minutes_many(ORIGIN, [SITE_A, nowhere, SITE_B], case_id="t")
    assert out == [100, None, None]  # B: OSRM found no route
    assert len(calls) == 1
    assert "/table/v1/driving/" in calls[0].url.path
    assert calls[0].url.path.count(";") == 2  # origin + 2 sites with coordinates


def test_drive_times_chunked(live, monkeypatch):
    state, calls = live
    monkeypatch.setattr(geo, "OSRM_TABLE_CHUNK", 2)

    def handler(req):
        n = req.url.path.count(";")
        return httpx.Response(200, json={"code": "Ok", "durations": [[0] + [60.0] * n]})

    state["handler"] = handler
    out = geo.drive_minutes_many(ORIGIN, [SITE_A, SITE_B, SITE_A, SITE_B, SITE_A], case_id="t")
    assert out == [1] * 5 and len(calls) == 3


def test_drive_times_osrm_down_gives_none(live):
    out = geo.drive_minutes_many(ORIGIN, [SITE_A, SITE_B], case_id="t")  # handler returns 503
    assert out == [None, None]
