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


def test_title_hint_alone_never_flags():
    t = trial(title="[SYNTHETIC] Neoadjuvant window-of-opportunity study in glioblastoma")
    assert find_pre_surgery_trials([t], get_policy()) == []


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
        update={"inclusion_phrases": ["neoadjuvant"], "exclusion_phrases": ["xenotransplant"]}
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
    with pytest.raises(RuntimeError):
        workflow.rescreen_preop("c1", "dr-synthetic-01")
    with pytest.raises(WrongGate):
        workflow.rescreen_preop("c1", "dr-synthetic-01")  # agent hasn't finished


def test_rescreen_only_at_preop_review(case_store):
    from agent.tests.test_workflow import new_case

    new_case(case_store)
    with pytest.raises(WrongGate, match="rescreen only"):
        workflow.rescreen_preop("c1", "dr-synthetic-01")
