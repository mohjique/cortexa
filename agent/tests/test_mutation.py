"""The real mutation check wired into the workflow (issue #18)."""

import json
from pathlib import Path

import pytest

from agent import mutation, workflow
from agent.policy import get_policy
from agent.schemas import (
    ClassifierOutput,
    DoctorDecisionAdapter,
    MutationCheckRan,
    MutationCheckSkipped,
    Shortlist,
)
from agent.workflow import AgentHooks, submit_decision

FIXTURES = Path(__file__).parent / "fixtures"
PATIENTS = Path(__file__).resolve().parents[2] / "clinical" / "patients"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def patient(case_id: str) -> dict:
    return json.loads((PATIENTS / f"{case_id}.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def default_hooks():
    old_hooks = workflow.get_hooks()
    old_source = mutation.set_catalog_source(mutation.demo_catalog)
    workflow.set_hooks(AgentHooks())
    yield
    workflow.set_hooks(old_hooks)
    mutation.set_catalog_source(old_source)


def decide(case_id, gate, decision):
    return submit_decision(
        DoctorDecisionAdapter.validate_python(
            {
                "case_id": case_id,
                "gate": gate,
                "decision": decision,
                "doctor_id": "dr-synthetic-01",
                "ts": "2026-11-21T14:05:40Z",
            }
        )
    )


def run_to_results(case_store, case_id, results):
    case_store.create(case_id)
    out = fixture("classifier_output.json") | {"case_id": case_id, "suspected_bucket": "glioma"}
    workflow.attach_classification(case_id, ClassifierOutput.model_validate(out))
    rec = decide(case_id, "confirm_bucket", {"bucket": "glioma", "overrode_model": False})
    acks = [
        {"trial_id": t.trial_id, "action": "not_pursuing", "reason": "test: recurrent glioma"}
        for t in rec.preop.pre_surgery_trials
    ]
    review = {"screen_id": rec.preop.screen_id, "tests_reviewed": True, "pre_surgery_trials": acks}
    decide(case_id, "preop_review", review)
    return decide(case_id, "enter_results", results)


def demo_results(case_id, **over):
    """The doctor-entered results stored in the synthetic patient file."""
    return patient(case_id)["doctor_decisions"]["enter_results"] | over


def test_every_patient_enter_results_validates():
    from agent.schemas import DoctorResults

    files = sorted(PATIENTS.glob("demo-*.json"))
    assert files
    for f in files:
        er = json.loads(f.read_text(encoding="utf-8"))["doctor_decisions"].get("enter_results")
        if er is not None:
            DoctorResults.model_validate(er)


def run_demo(case_store, case_id):
    return run_to_results(case_store, case_id, demo_results(case_id))


def actions(audit_log, case_id):
    return [e.action for e in audit_log.read(case_id)]


# -- demo patients, end to end ----------------------------------------------


@pytest.mark.parametrize(
    "case_id, call",
    [("demo-016", "detected"), ("demo-017", "inconclusive"), ("demo-018", "not_detected")],
)
def test_demo_patients_get_real_calls(case_store, audit_log, case_id, call):
    rec = run_demo(case_store, case_id)
    m = rec.mutation
    assert isinstance(m, MutationCheckRan)
    assert m.sbs11_call == call
    assert m.total_mutations == sum(patient(case_id)["mutation_counts_96"])
    assert m.false_alarm_rate == get_policy().mutation_check.false_alarm_rate
    assert m.summary  # the frontend shows this as written
    acts = actions(audit_log, case_id)
    assert "tool_call:mutation_check" in acts and "stage_done:mutation_check" in acts
    assert rec.state.stage == "no_match"  # stub matching; the check never blocks matching


def test_demo_catalog_never_reads_answer_key():
    cat = mutation.demo_catalog("demo-016")
    assert cat is not None and set(cat) == {"sequencing", "counts_96"}


@pytest.mark.parametrize("case_id", ["c1", "demo-999", "../policies/policy", "demo-001"])
def test_demo_catalog_none_without_stored_counts(case_id):
    assert mutation.demo_catalog(case_id) is None


def test_request_sends_one_source_and_doctor_sequencing_wins(case_store, monkeypatch):
    results = demo_results("demo-016", sequencing="genome")
    seen = {}

    def spy(request, *, false_alarm_rate):
        seen.update(request)
        raise mutation.MutationCheckUnavailable("spy")

    monkeypatch.setattr(mutation, "run_mutation_check", spy)
    run_to_results(case_store, "demo-016", results)
    assert seen["sequencing"] == "genome" and seen["reference"] == "GRCh38"
    assert "counts_96" in seen and "snvs" not in seen


# -- shortlist context --------------------------------------------------------


def shortlist_hook(rec, policy):
    data = fixture("shortlist.json") | {"case_id": rec.case_id}
    return Shortlist.model_validate(data, context=policy.status_context())


def test_result_lands_in_every_trial_mutation_context(case_store):
    workflow.set_hooks(AgentHooks(matching=shortlist_hook))
    rec = run_demo(case_store, "demo-016")
    assert rec.state.stage == "review_shortlist" and rec.shortlist is not None
    for t in rec.shortlist.trials:
        assert t.mutation_context == rec.mutation
        # Rule 10: context never becomes eligibility evidence (schema enforces the rest).
        assert all(not r.evidence_field.startswith("mutation") for r in t.eligibility)


def test_no_mutation_context_when_check_unavailable(case_store):
    workflow.set_hooks(AgentHooks(matching=shortlist_hook))
    mutation.set_catalog_source(lambda cid: None)
    rec = run_demo(case_store, "demo-016")
    assert rec.shortlist is not None
    assert all(t.mutation_context is None for t in rec.shortlist.trials)


# -- unavailable: audit, no guess, matching continues -------------------------


def test_unavailable_is_audited_and_matching_continues(case_store, audit_log, monkeypatch):
    def unavailable(request, *, false_alarm_rate):
        raise mutation.MutationCheckUnavailable("calibrated at false-alarm rate 0.05, policy asks for 0.01")

    monkeypatch.setattr(mutation, "run_mutation_check", unavailable)
    rec = run_demo(case_store, "demo-016")
    assert rec.mutation is None  # never a guessed result
    assert rec.state.stage == "no_match"  # matching ran
    entries = [e for e in audit_log.read("demo-016") if e.action == "mutation_check_unavailable"]
    assert len(entries) == 1 and "0.05" in entries[0].detail["reason"]
    assert "stage_done:mutation_check" in actions(audit_log, "demo-016")


def test_no_catalog_on_file_is_unavailable(case_store, audit_log):
    rec = run_to_results(case_store, "c1", fixture("results_glioma.json") | {"case_id": "c1"})
    assert rec.mutation is None and rec.state.stage == "no_match"
    assert "mutation_check_unavailable" in actions(audit_log, "c1")


def test_bad_catalog_is_unavailable(case_store, audit_log):
    mutation.set_catalog_source(lambda cid: {"sequencing": "exome", "counts_96": [1] * 95})
    rec = run_demo(case_store, "demo-016")
    assert rec.mutation is None and rec.state.stage == "no_match"
    assert "mutation_check_unavailable" in actions(audit_log, "demo-016")


def test_skipped_result_is_stored(case_store, audit_log):
    def skipped(rec, policy):
        return MutationCheckSkipped(case_id=rec.case_id, ran=False, skip_reason="no_broad_sequencing")

    workflow.set_hooks(AgentHooks(mutation_check=skipped))
    rec = run_demo(case_store, "demo-016")
    assert isinstance(rec.mutation, MutationCheckSkipped)
    assert rec.state.stage == "no_match"
