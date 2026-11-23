"""F7: trial search, cache fallback, geocode, nearest site."""

from __future__ import annotations


import httpx
import pytest
from pydantic import ValidationError

from agent import geo, http_cache, policy, trials
from agent.schemas import Origin, TrialSite

ORIGIN = Origin(zip="77840", lat=30.628, lon=-96.3344)


@pytest.fixture
def live(monkeypatch, tmp_path):
    """Live mode with a private disk cache and a fake HTTP server."""
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


@pytest.fixture
def policy_with(tmp_path):
    """Swap in a policy.yaml edited by `edit(text) -> text`."""
    old = policy._default

    def make(edit):
        path = tmp_path / "policy.yaml"
        path.write_text(edit(policy.POLICY_PATH.read_text(encoding="utf-8")), encoding="utf-8")
        policy.set_policy_store(policy.PolicyStore(path))

    yield make
    policy.set_policy_store(old)


def demo_studies():
    return http_cache.load_demo("ctgov/studies_brain_tumor.json")


# --- demo mode -------------------------------------------------------------


def test_demo_search_merges_both_sources():
    found = trials.search_trials()
    assert [t.trial_id for t in found] == [f"NCT990000{i:02d}" for i in range(1, 10)]
    assert all(t.source == "ctgov+nci" and t.from_cache for t in found)
    assert all(t.title.startswith("[SYNTHETIC]") for t in found)
    status = {t.trial_id: t.recruitment_status for t in found}
    assert status["NCT99000003"] == status["NCT99000006"] == "NOT_YET_RECRUITING"
    assert [t.trial_id for t in found if t.remote_participation] == ["NCT99000008"]


def test_search_is_audited(audit_log):
    trials.search_trials(case_id="demo-001")
    actions = [e.action for e in audit_log.read("demo-001")]
    assert "trial_search" in actions


def test_policy_status_edit_changes_results(policy_with):
    policy_with(lambda t: t.replace("statuses: [RECRUITING, NOT_YET_RECRUITING]", "statuses: [RECRUITING]"))
    ids = {t.trial_id for t in trials.search_trials()}
    assert "NCT99000003" not in ids and "NCT99000006" not in ids
    assert "NCT99000001" in ids


def test_demo_distances_match_readme():
    by_id = {t.trial_id: t for t in trials.search_trials()}
    near = geo.nearest_site(ORIGIN, by_id["NCT99000002"].sites)  # Houston + Dallas
    assert (near.city, near.distance_miles, near.drive_minutes) == ("Houston", 84.6, 105)
    far = geo.nearest_site(ORIGIN, by_id["NCT99000007"].sites)
    assert (far.city, far.distance_miles, far.drive_minutes) == ("New Orleans", 376.1, 360)


# --- normalize / merge -----------------------------------------------------


@pytest.mark.parametrize(
    "text, years",
    [("18 Years", 18.0), ("6 Months", 0.5), ("75 years", 75.0), (None, None), ("N/A", None)],
)
def test_parse_age(text, years):
    assert trials.parse_age_years(text) == years


@pytest.mark.parametrize(
    "value, want",
    [("ALL", "ALL"), ("FEMALE", "FEMALE"), ("MALE", "MALE"), ("BOTH", "ALL"), ("female", "FEMALE"),
     (None, None), ("", None), ("OTHER", None), (1, None)],
)
def test_parse_sex(value, want):
    assert trials.parse_sex(value) == want


def test_ctgov_sex_eligibility_parsed():
    fetched = http_cache.Fetched({}, True, http_cache.demo_manifest_time(), "demo_cache")
    study = demo_studies()["studies"][0]
    assert trials.normalize_ctgov(study, fetched, policy.get_policy()).sex_eligibility == "ALL"
    study["protocolSection"]["eligibilityModule"]["sex"] = "FEMALE"
    assert trials.normalize_ctgov(study, fetched, policy.get_policy()).sex_eligibility == "FEMALE"
    del study["protocolSection"]["eligibilityModule"]["sex"]
    assert trials.normalize_ctgov(study, fetched, policy.get_policy()).sex_eligibility is None  # never guessed


def test_nci_sex_eligibility_parsed_and_merged():
    fetched = http_cache.Fetched({}, True, http_cache.demo_manifest_time(), "demo_cache")
    nci = http_cache.load_demo("nci/trials_brain_tumor.json")["data"][0]
    assert trials.normalize_nci(nci, fetched, policy.get_policy()).sex_eligibility == "ALL"  # BOTH
    nci["eligibility"]["structured"]["sex"] = "MALE"
    n = trials.normalize_nci(nci, fetched, policy.get_policy())
    assert n.sex_eligibility == "MALE"
    study = next(s for s in demo_studies()["studies"]
                 if s["protocolSection"]["identificationModule"]["nctId"] == n.trial_id)
    del study["protocolSection"]["eligibilityModule"]["sex"]
    c = trials.normalize_ctgov(study, fetched, policy.get_policy())
    assert trials.merge([c], [n])[0].sex_eligibility == "MALE"  # NCI fills an empty ctgov value


def test_demo_trials_accept_all_sexes():
    assert {t.sex_eligibility for t in trials.search_trials()} == {"ALL"}


def test_closed_site_is_not_kept():
    study = demo_studies()["studies"][1]  # Houston + Dallas
    study["protocolSection"]["contactsLocationsModule"]["locations"][0]["status"] = "COMPLETED"
    fetched = http_cache.Fetched({}, True, http_cache.demo_manifest_time(), "demo_cache")
    rec = trials.normalize_ctgov(study, fetched, policy.get_policy())
    assert [s.city for s in rec.sites] == ["Dallas"]


def test_out_of_policy_status_is_dropped_and_logged(monkeypatch, audit_log):
    data = demo_studies()
    data["studies"][0]["protocolSection"]["statusModule"]["overallStatus"] = "COMPLETED"
    monkeypatch.setattr(http_cache, "load_demo", lambda rel, _orig=http_cache.load_demo:
                        data if rel.startswith("ctgov") else _orig(rel))
    found = {t.trial_id: t for t in trials.search_trials()}
    assert found["NCT99000001"].source == "nci"  # NCI copy survives, ctgov copy dropped
    assert any(e.action == "trial_dropped" for e in audit_log.read())


def test_nci_only_trial_builds_criteria_text():
    nci = http_cache.load_demo("nci/trials_brain_tumor.json")["data"][0]
    fetched = http_cache.Fetched({}, True, http_cache.demo_manifest_time(), "demo_cache")
    rec = trials.normalize_nci(nci, fetched, policy.get_policy())
    assert rec.criteria_text.startswith("Inclusion Criteria:")
    assert "Exclusion Criteria:\n\n* Prior treatment with an IDH inhibitor" in rec.criteria_text
    assert rec.max_age_years is None  # NCI 999 = no maximum


def test_unknown_nci_status_skipped():
    nci = dict(http_cache.load_demo("nci/trials_brain_tumor.json")["data"][0], current_trial_status="Withdrawn")
    fetched = http_cache.Fetched({}, True, http_cache.demo_manifest_time(), "demo_cache")
    assert trials.normalize_nci(nci, fetched, policy.get_policy()) is None


# --- live mode + cache -----------------------------------------------------


def test_live_success_saves_cache_without_api_key(live, monkeypatch, tmp_path):
    state, calls = live
    monkeypatch.setenv("NCI_API_KEY", "secret-key-123")
    nci = http_cache.load_demo("nci/trials_brain_tumor.json")
    state["handler"] = lambda req: httpx.Response(
        200, json=nci if "cancer.gov" in str(req.url) else demo_studies()
    )
    found = trials.search_trials()
    assert len(found) == 9 and not any(t.from_cache for t in found)
    ct = next(r for r in calls if "clinicaltrials.gov" in str(r.url))
    assert ct.url.params["filter.overallStatus"] == "RECRUITING,NOT_YET_RECRUITING"
    assert "filter.geo" not in ct.url.params  # no distance cutoff
    assert next(r for r in calls if "cancer.gov" in str(r.url)).headers["X-API-KEY"] == "secret-key-123"
    saved = list((tmp_path / "cache").rglob("*.json"))
    assert len(saved) == 2
    assert all("secret-key-123" not in p.read_text(encoding="utf-8") for p in saved)


def test_live_failure_uses_disk_cache_then_demo(live, audit_log):
    state, _ = live
    state["handler"] = lambda req: httpx.Response(200, json=demo_studies())
    trials.fetch_ctgov("brain tumor", policy.get_policy())  # warms disk cache

    state["handler"] = lambda req: httpx.Response(503)
    _, got = trials.fetch_ctgov("brain tumor", policy.get_policy())
    assert got.origin == "disk_cache" and got.from_cache

    _, got = trials.fetch_ctgov("glioma", policy.get_policy())  # never cached
    assert got.origin == "demo_cache"
    fallbacks = [e.detail["to"] for e in audit_log.read() if e.action == "api_fallback"]
    assert fallbacks == ["disk_cache", "demo_cache"]


def test_live_without_nci_key_skips_nci(live, monkeypatch, audit_log):
    state, calls = live
    monkeypatch.delenv("NCI_API_KEY", raising=False)
    state["handler"] = lambda req: httpx.Response(200, json=demo_studies())
    found = trials.search_trials()
    assert all(t.source == "ctgov" for t in found)
    assert not any("cancer.gov" in str(r.url) for r in calls)
    assert any(e.action == "nci_skipped" for e in audit_log.read())


def test_bad_source_mode(monkeypatch):
    monkeypatch.setenv("CORTEXA_TRIAL_SOURCE", "offline")
    with pytest.raises(ValueError):
        http_cache.source_mode()


# --- geo -------------------------------------------------------------------


def test_haversine_known_distance():
    # College Station -> Houston medical center, matches the demo README
    assert round(geo.haversine_miles(30.628, -96.3344, 29.7079, -95.4012), 1) == 84.6


@pytest.mark.parametrize("bad", ["123 Main St, College Station TX", "7784", "77840-1234", "abcde"])
def test_geocode_rejects_anything_but_a_zip(bad):
    with pytest.raises(ValidationError):
        geo.geocode_zip(bad)


def test_geocode_demo_and_unknown_zip():
    assert geo.geocode_zip("77840") == ORIGIN
    with pytest.raises(geo.GeoError):
        geo.geocode_zip("10001")


def test_geocode_live_sends_only_zip(live):
    state, calls = live
    state["handler"] = lambda req: httpx.Response(200, json=[{"lat": "30.6", "lon": "-96.3"}])
    o = geo.geocode_zip("77840")
    assert (o.lat, o.lon) == (30.6, -96.3)
    assert dict(calls[0].url.params) == {"postalcode": "77840", "country": "us", "format": "jsonv2", "limit": "1"}


def test_osrm_down_gives_no_drive_time(live):
    site = TrialSite(facility="X", city="Houston", state="TX", zip="77030", lat=29.7079, lon=-95.4012)
    near = geo.nearest_site(ORIGIN, [site])
    assert near.distance_miles == 84.6 and near.drive_minutes is None


def test_osrm_live_route(live):
    state, _ = live
    state["handler"] = lambda req: httpx.Response(200, json={"code": "Ok", "routes": [{"duration": 6300}]})
    site = TrialSite(facility="X", city="Houston", zip="77030", lat=29.7079, lon=-95.4012)
    assert geo.drive_minutes(ORIGIN, site) == 105


def test_demo_drive_time_only_from_demo_origin():
    site = TrialSite(facility="X", city="Houston", zip="77030", lat=29.7079, lon=-95.4012)
    other = Origin(zip="78229", lat=29.507, lon=-98.575)
    assert geo.drive_minutes(other, site) is None


def test_nearest_site_none_without_coordinates():
    assert geo.nearest_site(ORIGIN, [TrialSite(facility="X", city="Y")]) is None
