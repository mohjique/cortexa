"""Scripted doctor decisions in the synthetic patient files must pass the real gate schemas."""

import json
from pathlib import Path

import pytest

from agent.schemas import DoctorDecisionAdapter

PATIENTS = sorted((Path(__file__).resolve().parents[2] / "clinical" / "patients").glob("demo-*.json"))


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", PATIENTS, ids=lambda p: p.stem)
def test_doctor_decisions_validate(path):
    d = load(path)
    for gate, decision in d["doctor_decisions"].items():
        if gate == "preop_review":
            # Scripts can't know the screen's content hash; the runner fills it in.
            decision = decision | {"screen_id": "preop-placeholder"}
        DoctorDecisionAdapter.validate_python(
            {
                "case_id": d["case_id"] if "case_id" in d else path.stem,
                "gate": gate,
                "decision": decision,
                "doctor_id": "dr-synthetic-01",
                "ts": "2026-11-21T14:05:40Z",
            }
        )


@pytest.mark.parametrize("path", PATIENTS, ids=lambda p: p.stem)
def test_preop_cases_ack_every_expected_flag(path):
    d = load(path)
    if "preop_screen" not in d["expected_path"]:
        assert "preop_review" not in d["doctor_decisions"]
        return
    review = d["doctor_decisions"].get("preop_review")
    assert review is not None, "glioma cases need a scripted preop_review"
    acked = {a["trial_id"] for a in review["pre_surgery_trials"]}
    assert acked == set(d.get("expected_preop_flags") or [])


# -- replay through the real gates ------------------------------------------

STOP_AT = {"enter_results", "matching"}  # stub matching decides what comes after


def _replay(path: Path, case_store, audit_log):
    from agent import workflow
    from agent.schemas import ClassifierOutput
    from agent.workflow import AgentHooks

    d = load(path)
    cid = d["case_id"]
    decisions = d["doctor_decisions"]
    out = json.loads((Path(__file__).parent / "fixtures" / "classifier_output.json").read_text())
    out |= {"case_id": cid, "suspected_bucket": decisions["confirm_bucket"]["bucket"]}

    old = workflow.get_hooks()
    workflow.set_hooks(AgentHooks())  # real pre-op screen and mutation check, stub matching
    try:
        case_store.create(cid, zip=d["zip"])
        rec = workflow.attach_classification(cid, ClassifierOutput.model_validate(out))
        while rec.state.waiting_on != "agent" and rec.state.stage not in workflow.FINAL_STAGES:
            gate = workflow.GATE_AT_STAGE.get(rec.state.stage)
            if gate not in decisions:
                break
            decision = decisions[gate]
            if gate == "preop_review":
                decision = decision | {"screen_id": rec.preop.screen_id}
            rec = workflow.submit_decision(
                DoctorDecisionAdapter.validate_python(
                    {
                        "case_id": cid,
                        "gate": gate,
                        "decision": decision,
                        "doctor_id": "dr-synthetic-01",
                        "ts": "2026-11-21T14:05:40Z",
                    }
                )
            )
    finally:
        workflow.set_hooks(old)
    visited = ["intake"] + [e.detail["to"] for e in audit_log.read(cid) if e.action == "stage_changed"]
    return d, rec, visited


@pytest.mark.parametrize("path", PATIENTS, ids=lambda p: p.stem)
def test_scripted_decisions_pass_the_real_gates(path, case_store, audit_log):
    d, rec, visited = _replay(path, case_store, audit_log)
    expected = d["expected_path"]
    cut = next((i for i, s in enumerate(expected) if s in STOP_AT), len(expected))
    assert visited[:cut] == expected[:cut]
    assert ("mutation_check" in visited) == ("mutation_check" in expected)
    if "preop_screen" in expected:
        flagged = [t.trial_id for t in rec.preop.pre_surgery_trials]
        assert flagged == d["expected_preop_flags"]
        assert all(t.doctor_action != "pending" for t in rec.preop.pre_surgery_trials)
