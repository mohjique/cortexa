import json
from pathlib import Path

import pytest

from agent import store, workflow
from agent.schemas import (
    ClassifierOutput,
    ConfirmBucketRecord,
    DoctorDecisionAdapter,
    MutationCheckSkipped,
    PreOpScreen,
    Shortlist,
)
from agent.workflow import (
    AgentHooks as _RealHooks,
    InvalidDecision,
    WrongGate,
    stub_preop_screen,
    submit_decision,
)


def AgentHooks(**kw):
    """Hooks with stub matching unless a test sets its own (state machine tests)."""
    kw.setdefault("matching", workflow.stub_matching)
    return _RealHooks(**kw)


FIXTURES = Path(__file__).parent / "fixtures"
CID = "c1"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def stub_hooks():
    # Mechanics tests use a screen that flags nothing; test_preop.py covers the real one.
    old = workflow.get_hooks()
    workflow.set_hooks(AgentHooks(preop_screen=stub_preop_screen))
    yield
    workflow.set_hooks(old)


def new_case(case_store, suspected="glioma"):
    case_store.create(CID)
    out = fixture("classifier_output.json") | {"case_id": CID, "suspected_bucket": suspected}
    return workflow.attach_classification(CID, ClassifierOutput.model_validate(out))


def decide(gate, decision):
    if gate == "preop_review" and "screen_id" not in decision:
        # The doctor reviews the screen currently on the case.
        rec = store.get_store().get(CID)
        decision = decision | {"screen_id": rec.preop.screen_id if rec.preop else "none"}
    return submit_decision(
        DoctorDecisionAdapter.validate_python(
            {
                "case_id": CID,
                "gate": gate,
                "decision": decision,
                "doctor_id": "dr-synthetic-01",
                "ts": "2026-11-21T14:05:40Z",
            }
        )
    )


def glioma_results(**over):
    return fixture("results_glioma.json") | {"case_id": CID} | over


def pituitary_meds_results():
    return fixture("results_pituitary_meds.json") | {"case_id": CID}


def shortlist_hook(rec, policy):
    data = fixture("shortlist.json") | {"case_id": rec.case_id}
    return Shortlist.model_validate(data, context=policy.status_context())


def preop_with_trial_hook(rec, policy):
    data = fixture("preop_screen.json") | {"case_id": rec.case_id}
    return PreOpScreen.model_validate(data, context=policy.status_context())


PREOP_OK = {"tests_reviewed": True, "pre_surgery_trials": []}


def actions(audit_log):
    return [e.action for e in audit_log.read(CID)]


# -- intake -----------------------------------------------------------------


def test_classification_moves_to_confirm_bucket(case_store):
    rec = new_case(case_store)
    assert rec.state.stage == "confirm_bucket" and rec.state.waiting_on == "doctor"
    assert rec.state.suspected_bucket == "glioma"


# -- branch rules -----------------------------------------------------------


def test_no_tumor_stops(case_store):
    new_case(case_store, suspected="no_tumor")
    rec = decide("confirm_bucket", {"bucket": "no_tumor", "overrode_model": False})
    assert rec.state.stage == "stopped"


def test_meningioma_watch_goes_to_rescan(case_store):
    new_case(case_store, suspected="meningioma")
    decide("confirm_bucket", {"bucket": "meningioma", "overrode_model": False})
    rec = decide("watch_or_treat", {"choice": "watch", "rescan_in_months": 6})
    assert rec.state.stage == "rescan" and rec.state.treatment_path == "watch"


def test_meningioma_treat_waits_on_pathology(case_store):
    new_case(case_store, suspected="meningioma")
    decide("confirm_bucket", {"bucket": "meningioma", "overrode_model": False})
    rec = decide("watch_or_treat", {"choice": "treat"})
    assert rec.state.stage == "awaiting_pathology" and rec.state.waiting_on == "pathology"
    assert rec.state.treatment_path == "surgery"


def test_glioma_runs_preop_then_waits_on_doctor(case_store):
    new_case(case_store)
    rec = decide("confirm_bucket", {"bucket": "glioma", "overrode_model": False})
    assert rec.state.stage == "preop_screen" and rec.state.waiting_on == "doctor"
    assert rec.preop is not None and rec.preop.label.startswith("Suspected glioma")
    rec = decide("preop_review", PREOP_OK)
    assert rec.state.stage == "awaiting_pathology" and rec.state.waiting_on == "pathology"


def test_preop_review_gates_even_with_no_pre_surgery_trials(case_store):
    new_case(case_store)
    decide("confirm_bucket", {"bucket": "glioma", "overrode_model": False})
    with pytest.raises(WrongGate, match="expects preop_review"):
        decide("enter_results", glioma_results())
    assert workflow.run_agent(CID).state.stage == "preop_screen"  # agent can't skip it


def test_preop_review_must_ack_every_pre_surgery_trial(case_store, audit_log):
    workflow.set_hooks(AgentHooks(preop_screen=preop_with_trial_hook))
    new_case(case_store)
    decide("confirm_bucket", {"bucket": "glioma", "overrode_model": False})
    with pytest.raises(InvalidDecision, match=r"missing \['NCT99000003'\]"):
        decide("preop_review", PREOP_OK)
    assert "decision_rejected:preop_review" in actions(audit_log)
    assert case_store.get(CID).state.stage == "preop_screen"

    stray = {"trial_id": "NCT99000009", "action": "not_pursuing", "reason": "not relevant"}
    with pytest.raises(InvalidDecision, match="not a pre-surgery trial"):
        decide("preop_review", PREOP_OK | {"pre_surgery_trials": [stray]})

    ack = {"trial_id": "NCT99000003", "action": "pursue_before_surgery"}
    rec = decide("preop_review", PREOP_OK | {"pre_surgery_trials": [ack]})
    assert rec.state.stage == "awaiting_pathology"
    assert "decision:preop_review" in actions(audit_log)
    t = rec.preop.pre_surgery_trials[0]  # the choice is on the record, not only in the log
    assert t.doctor_action == "pursue_before_surgery" and t.doctor_reason is None


def test_preop_review_rejected_before_screen_runs(case_store):
    def boom(rec, policy):
        raise RuntimeError("api down")

    workflow.set_hooks(AgentHooks(preop_screen=boom))
    new_case(case_store)
    decide("confirm_bucket", {"bucket": "glioma", "overrode_model": False})
    with pytest.raises(WrongGate, match="agent has not finished"):
        decide("preop_review", PREOP_OK)


def test_preop_review_of_an_older_screen_is_rejected(case_store, audit_log):
    workflow.set_hooks(AgentHooks(preop_screen=preop_with_trial_hook))
    new_case(case_store)
    rec = decide("confirm_bucket", {"bucket": "glioma", "overrode_model": False})
    reviewed = rec.preop.screen_id

    def status_changed(rec, policy):
        screen = preop_with_trial_hook(rec, policy)
        t = screen.pre_surgery_trials[0].model_copy(update={"recruitment_status": "RECRUITING"})
        data = screen.model_dump(exclude={"screen_id"}) | {"pre_surgery_trials": [t.model_dump()]}
        return PreOpScreen.model_validate(data, context=policy.status_context())

    workflow.set_hooks(AgentHooks(preop_screen=status_changed))
    rec = workflow.rescreen_preop(CID, "dr-synthetic-01", "status check")
    assert rec.preop.screen_id != reviewed  # same trial IDs, different content
    ack = {"trial_id": "NCT99000003", "action": "pursue_before_surgery"}
    with pytest.raises(InvalidDecision, match="screen changed"):
        decide("preop_review", PREOP_OK | {"screen_id": reviewed, "pre_surgery_trials": [ack]})
    rec = decide("preop_review", PREOP_OK | {"pre_surgery_trials": [ack]})  # current screen
    assert rec.state.stage == "awaiting_pathology"
    entry = next(e for e in audit_log.read(CID) if e.action == "preop_review_recorded")
    assert entry.detail["screen_id"] == rec.preop.screen_id
    assert entry.detail["tests_needed_listed"] == len(rec.preop.tests_needed)


def test_pituitary_needs_labs_gate_before_surgery_or_meds(case_store):
    new_case(case_store, suspected="pituitary")
    rec = decide("confirm_bucket", {"bucket": "pituitary", "overrode_model": False})
    assert rec.state.stage == "hormone_labs" and rec.state.waiting_on == "doctor"
    with pytest.raises(WrongGate):
        decide("surgery_or_meds", {"choice": "surgery"})
    rec = decide("hormone_labs", {"labs_reviewed": True})
    assert rec.state.stage == "surgery_or_meds"


def test_pituitary_meds_only_skips_pathology(case_store):
    new_case(case_store, suspected="pituitary")
    decide("confirm_bucket", {"bucket": "pituitary", "overrode_model": False})
    decide("hormone_labs", {"labs_reviewed": True})
    rec = decide("surgery_or_meds", {"choice": "medication_only"})
    assert rec.state.stage == "enter_results" and rec.state.waiting_on == "doctor"
    rec = decide("enter_results", pituitary_meds_results())
    assert rec.state.stage == "no_match"  # stub matching finds nothing
    assert rec.mutation is not None and rec.mutation.ran is False


def test_pituitary_surgery_waits_on_pathology(case_store):
    new_case(case_store, suspected="pituitary")
    decide("confirm_bucket", {"bucket": "pituitary", "overrode_model": False})
    decide("hormone_labs", {"labs_reviewed": True})
    rec = decide("surgery_or_meds", {"choice": "surgery"})
    assert rec.state.stage == "awaiting_pathology"


def test_recurrent_glioma_with_exome_runs_mutation_check(case_store, audit_log):
    workflow.set_hooks(AgentHooks(preop_screen=stub_preop_screen, mutation_check=workflow.stub_mutation_check))
    new_case(case_store)
    decide("confirm_bucket", {"bucket": "glioma", "overrode_model": False})
    decide("preop_review", PREOP_OK)
    rec = decide("enter_results", glioma_results())
    assert rec.mutation is not None and rec.mutation.ran is True
    assert rec.mutation.sbs11_call == "inconclusive"
    assert "stage_done:mutation_check" in actions(audit_log)
    assert rec.state.stage == "no_match"


def test_recurrent_glioma_without_broad_sequencing_skips(case_store, audit_log):
    new_case(case_store)
    decide("confirm_bucket", {"bucket": "glioma", "overrode_model": False})
    decide("preop_review", PREOP_OK)
    rec = decide(
        "enter_results",
        glioma_results(broad_sequencing_available=False, sequencing="panel"),
    )
    m = rec.mutation
    assert isinstance(m, MutationCheckSkipped) and m.skip_reason == "no_broad_sequencing"
    assert "stage_done:mutation_check" not in actions(audit_log)


def test_new_glioma_skips_mutation_check(case_store):
    new_case(case_store)
    decide("confirm_bucket", {"bucket": "glioma", "overrode_model": False})
    decide("preop_review", PREOP_OK)
    rec = decide("enter_results", glioma_results(recurrent=False, recurrence_number=0))
    m = rec.mutation
    assert isinstance(m, MutationCheckSkipped) and m.skip_reason == "not_recurrent_glioma"


# -- shortlist --------------------------------------------------------------


def to_shortlist(case_store):
    workflow.set_hooks(AgentHooks(preop_screen=stub_preop_screen, matching=shortlist_hook))
    new_case(case_store)
    decide("confirm_bucket", {"bucket": "glioma", "overrode_model": False})
    decide("preop_review", PREOP_OK)
    return decide("enter_results", glioma_results(recurrent=False, recurrence_number=0))


def test_match_goes_to_review_then_stopped(case_store):
    rec = to_shortlist(case_store)
    assert rec.state.stage == "review_shortlist" and rec.state.waiting_on == "doctor"
    rec = decide("review_shortlist", {"keep": ["NCT99000001"], "reject": []})
    assert rec.state.stage == "stopped"
    assert rec.shortlist is not None
    assert rec.shortlist.trials[0].doctor_decision == "keep"


def test_review_must_decide_every_trial(case_store):
    to_shortlist(case_store)
    with pytest.raises(InvalidDecision, match="missing"):
        decide("review_shortlist", {"keep": [], "reject": []})


def test_review_rejects_unknown_trial(case_store):
    to_shortlist(case_store)
    with pytest.raises(InvalidDecision, match="not on the shortlist"):
        decide("review_shortlist", {"keep": ["NCT99000001", "NCT99000099"], "reject": []})


# -- gates ------------------------------------------------------------------


def test_wrong_gate_rejected_and_audited(case_store, audit_log):
    new_case(case_store)
    with pytest.raises(WrongGate):
        decide("enter_results", glioma_results())
    rec = case_store.get(CID)
    assert rec.state.stage == "confirm_bucket" and rec.decisions == []
    assert "decision_rejected:enter_results" in actions(audit_log)


def test_no_gate_accepted_at_final_stage(case_store):
    new_case(case_store, suspected="no_tumor")
    decide("confirm_bucket", {"bucket": "no_tumor", "overrode_model": False})
    with pytest.raises(WrongGate):
        decide("confirm_bucket", {"bucket": "glioma", "overrode_model": True})


def test_overrode_model_computed_by_server(case_store):
    new_case(case_store, suspected="glioma")
    rec = decide("confirm_bucket", {"bucket": "meningioma", "overrode_model": False})
    d = rec.decisions[0]
    assert isinstance(d, ConfirmBucketRecord) and d.decision.overrode_model is True
    assert rec.state.stage == "watch_or_treat"  # follows the doctor's bucket


def test_results_bucket_must_match_confirmed(case_store):
    new_case(case_store, suspected="meningioma")
    decide("confirm_bucket", {"bucket": "meningioma", "overrode_model": False})
    decide("watch_or_treat", {"choice": "treat"})
    with pytest.raises(InvalidDecision, match="confirmed meningioma"):
        decide("enter_results", glioma_results())


def test_every_decision_and_stage_change_audited(case_store, audit_log):
    new_case(case_store)
    decide("confirm_bucket", {"bucket": "glioma", "overrode_model": False})
    decide("preop_review", PREOP_OK)
    acts = actions(audit_log)
    assert "decision:confirm_bucket" in acts and "decision:preop_review" in acts
    assert "waiting_on_doctor" in acts
    assert acts.count("stage_changed") == 3  # intake->confirm, ->preop, ->pathology


# -- agent hook failure -----------------------------------------------------


def test_failed_hook_leaves_case_at_agent_stage_and_retries(case_store, audit_log):
    def boom(rec, policy):
        raise RuntimeError("api down")

    workflow.set_hooks(AgentHooks(preop_screen=boom))
    new_case(case_store)
    # The decision is saved, so a hook failure returns the saved state, not an error.
    rec = decide("confirm_bucket", {"bucket": "glioma", "overrode_model": False})
    assert rec.state.stage == "preop_screen" and rec.state.waiting_on == "agent"
    assert case_store.get(CID).state.confirmed_bucket == "glioma"
    assert "stage_failed:preop_screen" in actions(audit_log)
    with pytest.raises(WrongGate):
        decide("confirm_bucket", {"bucket": "glioma", "overrode_model": False})  # already recorded
    with pytest.raises(RuntimeError):
        workflow.run_agent(CID)  # a direct retry still reports the failure

    workflow.set_hooks(AgentHooks(preop_screen=stub_preop_screen))
    rec = workflow.run_agent(CID)
    assert rec.state.stage == "preop_screen" and rec.state.waiting_on == "doctor"


def test_empty_shortlist_is_a_hook_error(case_store, audit_log):
    def empty(rec, policy):
        sl = shortlist_hook(rec, policy)
        return sl.model_copy(update={"trials": []})

    workflow.set_hooks(AgentHooks(preop_screen=stub_preop_screen, matching=empty))
    new_case(case_store)
    decide("confirm_bucket", {"bucket": "glioma", "overrode_model": False})
    decide("preop_review", PREOP_OK)
    rec = decide("enter_results", glioma_results(recurrent=False, recurrence_number=0))
    assert rec.state.stage == "matching" and rec.no_match is None
    assert "stage_failed:matching" in actions(audit_log)
    with pytest.raises(workflow.WorkflowError, match="empty Shortlist"):
        workflow.run_agent(CID)

