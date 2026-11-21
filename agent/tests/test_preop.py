"""Pre-surgery trial detection (pre-op screen) and the doctor's rescreen action."""

import pytest

from agent import workflow
from agent.policy import get_policy
from agent.preop import find_pre_surgery_trials, pre_surgery_criterion
from agent.schemas import TrialRecord
from agent.trials import search_trials
from agent.workflow import AgentHooks, WrongGate


def trial(**over) -> TrialRecord:
    base = {
        "trial_id": "NCT99000099",
        "title": "[SYNTHETIC] Test trial in glioblastoma",
        "source": "ctgov",
        "recruitment_status": "RECRUITING",
        "conditions": ["Glioblastoma"],
        "criteria_text": "Inclusion Criteria:\n\n* Age 18 or older\n\nExclusion Criteria:\n\n* Pregnancy",
        "remote_participation": False,
        "fetched_at": "2026-11-21T14:00:00Z",
        "from_cache": True,
    }
    return TrialRecord.model_validate(base | over, context=get_policy().status_context())


def test_demo_cache_flags_only_the_window_trial():
    flagged = find_pre_surgery_trials(search_trials(), get_policy())
    assert [t.trial_id for t in flagged] == ["NCT99000003"]
    assert flagged[0].criterion_text == "Must enroll before planned surgical resection"  # rule 4
    assert flagged[0].recruitment_status == "NOT_YET_RECRUITING"


def test_exclusion_phrase_alone_flags_and_cites_it():
    t = trial(criteria_text="Inclusion Criteria:\n* Adults\nExclusion Criteria:\n* Prior resection of the tumor")
    assert pre_surgery_criterion(t, get_policy()) == "Prior resection of the tumor"


def test_prior_resection_in_inclusion_is_not_a_pre_surgery_trial():
    # "prior resection" as an inclusion means a recurrent-tumor trial, the opposite case.
    t = trial(criteria_text="Inclusion Criteria:\n* Prior resection required\nExclusion Criteria:\n* None")
    assert pre_surgery_criterion(t, get_policy()) is None


def test_title_hint_alone_is_shown_as_possible():
    # Not confirmed by any criterion, but the window closes at surgery, so show it.
    t = trial(title="[SYNTHETIC] Neoadjuvant window-of-opportunity study in glioblastoma")
    [flag] = find_pre_surgery_trials([t], get_policy())
    assert flag.matched_on == "title_hint"
    assert flag.criterion_text == f"Title: {t.title}"


def test_criteria_match_wins_over_title_hint():
    [flag] = find_pre_surgery_trials(search_trials(), get_policy())
    assert flag.trial_id == "NCT99000003" and flag.matched_on == "criteria"


def test_mid_sentence_exclusion_mention_does_not_split_inclusion():
    t = trial(
        criteria_text="Inclusion Criteria:\n* Meets none of the exclusion criteria below\n"
        "* Must enroll before surgery\n\nExclusion Criteria:\n* Pregnancy"
    )
    assert pre_surgery_criterion(t, get_policy()) == "Must enroll before surgery"


def test_nearest_site_from_case_zip(case_store):
    from agent.preop import preop_screen_hook

    rec = case_store.create("c1", zip="77840")
    screen = preop_screen_hook(rec, get_policy())
    site = screen.pre_surgery_trials[0].nearest_site
    assert site is not None and site.city == "Austin" and site.distance_miles > 0


def test_flags_sorted_nearest_first_no_site_last(monkeypatch):
    from agent import geo
    from agent.schemas import NearestSite, Origin

    # Trials are processed in input order, so hand out distances in that order.
    ids = ["NCT99000091", "NCT99000092", "NCT99000093"]
    distances = iter([900.0, None, 40.0])

    def fake_nearest_site(*args, **kwargs):
        d = next(distances)
        return None if d is None else NearestSite(city="X", distance_miles=d)

    monkeypatch.setattr(geo, "nearest_site", fake_nearest_site)
    trials = [trial(trial_id=t, title="[SYNTHETIC] Neoadjuvant glioblastoma study") for t in ids]
    origin = Origin(zip="77840", lat=30.6, lon=-96.3)
    flagged = find_pre_surgery_trials(trials, get_policy(), origin=origin)
    assert [f.trial_id for f in flagged] == ["NCT99000093", "NCT99000091", "NCT99000092"]


def test_preop_search_has_no_radius(case_store, monkeypatch):
    import agent.preop as preop

    seen = {}

    def spy(**kw):
        seen.update(kw)
        return search_trials()

    monkeypatch.setattr(preop, "search_trials", spy)
    preop.preop_screen_hook(case_store.create("c1", zip="77840"), get_policy())
    assert seen.get("radius_miles") is None and seen.get("origin") is None


def test_no_zip_means_no_nearest_site(case_store):
    from agent.preop import preop_screen_hook

    rec = case_store.create("c1")
    assert preop_screen_hook(rec, get_policy()).pre_surgery_trials[0].nearest_site is None


def test_bad_zip_lookup_does_not_block_the_screen(case_store, audit_log):
    from agent.preop import preop_screen_hook

    rec = case_store.create("c1", zip="00001")  # not in the demo cache
    screen = preop_screen_hook(rec, get_policy())
    assert screen.pre_surgery_trials and screen.pre_surgery_trials[0].nearest_site is None
    assert "preop_location_unavailable" in [e.action for e in audit_log.read("c1")]


def test_non_glioma_trial_is_skipped():
    t = trial(
        conditions=["Meningioma"],
        title="[SYNTHETIC] Meningioma study",
        criteria_text="Inclusion Criteria:\n* Must enroll before surgery",
    )
    assert find_pre_surgery_trials([t], get_policy()) == []


def test_policy_phrases_change_behavior_without_code():
    policy = get_policy()
    rules = policy.pre_surgery_detection.model_copy(
        update={
            "inclusion_phrases": ["neoadjuvant"],
            "exclusion_phrases": ["xenotransplant"],
            "title_hints": [],
        }
    )
    edited = policy.model_copy(update={"pre_surgery_detection": rules})
    assert find_pre_surgery_trials(search_trials(), edited) == []


# -- rescreen -----------------------------------------------------------------


@pytest.fixture
def glioma_at_review(case_store):
    from agent.tests.test_workflow import decide, new_case

    old = workflow.get_hooks()
    workflow.set_hooks(AgentHooks())  # real pre-op screen
    new_case(case_store)
    decide("confirm_bucket", {"bucket": "glioma", "overrode_model": False})
    yield
    workflow.set_hooks(old)


def test_rescreen_runs_the_screen_again(glioma_at_review, audit_log):
    rec = workflow.rescreen_preop("c1", "dr-synthetic-01", "status check")
    assert rec.state.stage == "preop_screen" and rec.state.waiting_on == "doctor"
    assert [t.trial_id for t in rec.preop.pre_surgery_trials] == ["NCT99000003"]
    entry = next(e for e in audit_log.read("c1") if e.action == "preop_rescreen_requested")
    assert entry.actor == "doctor" and entry.detail["previous_flags"] == ["NCT99000003"]


def test_rescreen_failure_blocks_the_gate_until_retry(glioma_at_review):
    def boom(rec, policy):
        raise RuntimeError("api down")

    workflow.set_hooks(AgentHooks(preop_screen=boom))
    rec = workflow.rescreen_preop("c1", "dr-synthetic-01")  # saved request, failed screen
    assert rec.state.waiting_on == "agent" and rec.preop is None
    with pytest.raises(WrongGate):
        workflow.rescreen_preop("c1", "dr-synthetic-01")  # agent hasn't finished


def test_rescreen_only_at_preop_review(case_store):
    from agent.tests.test_workflow import new_case

    new_case(case_store)
    with pytest.raises(WrongGate, match="rescreen only"):
        workflow.rescreen_preop("c1", "dr-synthetic-01")
