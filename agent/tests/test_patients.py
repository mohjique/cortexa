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
