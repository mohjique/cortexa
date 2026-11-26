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


def test_phrase_without_enrollment_wording_is_flagged_as_possible():
    """Live NCT05084430 wording. It may time a measurement, or the trial may dose
    before surgery; only the doctor can tell, so it is shown, not dropped."""
    lesion = "Preoperatively, the lesion must be ≥1.0 cm in diameter as determined by MRI."
    t = trial(criteria_text=f"Inclusion Criteria:\n* {lesion}\n"
              "* Preoperative MRI must show a contrast-enhancing lesion\n"
              "Exclusion Criteria:\n* Pregnancy")
    [flag] = find_pre_surgery_trials([t], get_policy())
    assert flag.matched_on == "phrase_only" and flag.criterion_text == lesion


def test_firm_match_wins_over_an_earlier_possible_one():
    t = trial(criteria_text="Inclusion Criteria:\n* Preoperative MRI required\n"
              "* Patients will receive the study drug before surgery\nExclusion Criteria:\n* Pregnancy")
    [flag] = find_pre_surgery_trials([t], get_policy())
    assert flag.matched_on == "criteria"
    assert flag.criterion_text == "Patients will receive the study drug before surgery"


def test_wrapped_bullet_matches_like_the_eligibility_checker():
    """Phrase and enrollment wording on different lines of one bullet still count."""
    t = trial(criteria_text="Inclusion Criteria:\n* Patients will be enrolled and\n  begin dosing before surgery\n"
              "Exclusion Criteria:\n* Pregnancy")
    [flag] = find_pre_surgery_trials([t], get_policy())
    assert flag.matched_on == "criteria"
    assert flag.criterion_text == "Patients will be enrolled and begin dosing before surgery"


@pytest.mark.parametrize("text", ["No prior resections of the tumor", "Prior brain surgeries"])
def test_exclusion_phrase_matches_plurals(text):
    t = trial(criteria_text=f"Inclusion Criteria:\n* Adults\nExclusion Criteria:\n* {text}")
    assert pre_surgery_criterion(t, get_policy()) == text


def test_exclusion_phrase_matches_whole_words_only():
    t = trial(criteria_text="Inclusion Criteria:\n* Adults\nExclusion Criteria:\n* Prior resectionist consult")
    assert pre_surgery_criterion(t, get_policy()) is None


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


# -- tests_needed (what the biopsy must answer) --------------------------------


def _live(*ids: str) -> list[TrialRecord]:
    from agent import live_eval

    fx = live_eval.load_fixtures()
    return [
        trial(
            trial_id=f["trial_id"],
            title=f["title"],
            conditions=f["conditions"],
            criteria_text=f["criteria_text"],
            recruitment_status=f["overall_status"],
        )
        for f in (fx[i] for i in ids)
    ]


def _by_field(tests) -> dict:
    return {t.field: t for t in tests}


def test_live_idh_and_grade_counts():
    from agent.preop import find_tests_needed

    # IDH-mutant grade 2 (safusidenib), IDH-wildtype grade 4 (P134 cells),
    # IDH-mutant grade 3 + 1p/19q codeleted, and an IDH-mutant LGG trial that
    # only says "low grade" (grade mentioned, no readable requirement).
    ids = ("NCT07712757", "NCT07318818", "NCT07832123", "NCT03922555")
    tests = _by_field(find_tests_needed(_live(*ids), get_policy()))

    idh = tests["idh"]
    assert idh.trial_ids == sorted(ids) and idh.decides_trials == 4
    req = {e.trial_id: e.requires for e in idh.evidence}
    assert req == {"NCT07712757": "mutant", "NCT07318818": "wildtype", "NCT07832123": "mutant", "NCT03922555": "mutant"}

    grade = tests["grade"]
    assert grade.trial_ids == ["NCT07318818", "NCT07712757", "NCT07832123"]
    assert grade.mention_trial_ids == ["NCT03922555"]
    assert {e.trial_id: e.requires for e in grade.evidence if e.requires} == {
        "NCT07318818": "4-4",
        "NCT07712757": "2-2",
        "NCT07832123": "3-3",
    }


def test_every_listed_trial_cites_its_inclusion_criterion():
    from agent.preop import find_tests_needed

    trials = _live("NCT07476794", "NCT07888647")
    for t in find_tests_needed(trials, get_policy()):
        for e in t.evidence:
            source = next(x for x in trials if x.trial_id == e.trial_id)
            inclusion = source.criteria_text.split("Exclusion Criteria")[0]
            # The bullet text is whitespace-normalized; compare the same way.
            assert " ".join(e.criterion_text.split()) in " ".join(inclusion.split())


def test_value_none_hits_are_mentions_not_requirements():
    from agent.preop import find_tests_needed

    t = trial(
        criteria_text="Inclusion Criteria:\n* IDH mutation status and MGMT promoter methylation status will be collected\n"
        "* Glioma of any grade\n\nExclusion Criteria:\n* IDH-mutant glioma",
    )
    tests = _by_field(find_tests_needed([t], get_policy()))
    for field in ("idh", "mgmt", "grade"):
        assert tests[field].decides_trials == 0 and tests[field].trial_ids == []
        assert tests[field].mention_trial_ids == ["NCT99000099"]
        assert [e.requires for e in tests[field].evidence] == [None]


def test_exclusion_bullets_and_non_glioma_trials_are_ignored():
    from agent.preop import find_tests_needed

    excl_only = trial(criteria_text="Inclusion Criteria:\n* Adults\n\nExclusion Criteria:\n* IDH-mutant tumor")
    meningioma = trial(
        trial_id="NCT99000098",
        conditions=["Meningioma"],
        title="[SYNTHETIC] Meningioma study",
        criteria_text="Inclusion Criteria:\n* WHO grade 2 or 3 meningioma",
    )
    assert find_tests_needed([excl_only, meningioma], get_policy()) == []


def test_tests_ordered_by_trials_decided():
    from agent.preop import find_tests_needed

    tests = find_tests_needed(_live("NCT07476794", "NCT07894549", "NCT07416188", "NCT05544526"), get_policy())
    # IDH 2, MGMT 2 (tie: fixed order), grade 1, H3 K27M 0 decided (1 mention).
    assert [(t.field, t.decides_trials) for t in tests] == [("idh", 2), ("mgmt", 2), ("grade", 1), ("h3k27m", 0)]
    assert tests[-1].mentions_trials == 1


def test_demo_cache_screen_matches_fixture(case_store, audit_log):
    import json
    from pathlib import Path

    from agent.preop import preop_screen_hook

    screen = preop_screen_hook(case_store.create("demo-001"), get_policy())
    fixture = json.loads((Path(__file__).parent / "fixtures" / "preop_screen.json").read_text(encoding="utf-8"))
    assert [t.model_dump(mode="json") for t in screen.tests_needed] == fixture["tests_needed"]
    assert screen.label == "Suspected glioma. Biopsy results pending."  # rule 1
    [entry] = [e for e in audit_log.read("demo-001") if e.action == "tool_call:preop_tests_needed"]
    assert entry.detail["tests"]["idh"] == {"decides": 4, "mentions": 0, "requires": {"mutant": 2, "wildtype": 2}}
    assert entry.detail["tests"]["grade"]["mentions"] == 1


def test_all_live_fixtures_summary():
    # Regression on the 25 real trials (24 pass the glioma filter). H3 K27M and
    # BRAF have no readable value in concepts.py, so they are mentions only.
    from agent import live_eval
    from agent.preop import find_tests_needed, tests_needed_summary

    tests = find_tests_needed(_live(*live_eval.load_fixtures()), get_policy())
    counts = {f: (v["decides"], v["mentions"]) for f, v in tests_needed_summary(tests).items()}
    assert counts == {
        "idh": (12, 4),
        "grade": (12, 3),
        "mgmt": (4, 1),
        "codeletion_1p19q": (3, 1),
        "h3k27m": (0, 4),
        "braf": (0, 1),
    }
    assert tests_needed_summary(tests)["idh"]["requires"] == {"mutant": 5, "wildtype": 7}
