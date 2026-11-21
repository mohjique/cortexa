import json
from pathlib import Path

import pytest

from agent.schemas import ClassifierOutput, DoctorDecisionAdapter, DoctorResults
from agent.store import CaseExists, CaseNotFound, CaseStore

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_create_starts_at_intake(case_store):
    rec = case_store.create("demo-001", suspected_bucket="glioma")
    assert rec.state.stage == "intake" and rec.state.waiting_on == "agent"
    assert (case_store.root / "demo-001.json").exists()


def test_create_twice_fails(case_store):
    case_store.create("demo-001")
    with pytest.raises(CaseExists):
        case_store.create("demo-001")


@pytest.mark.parametrize("bad", ["../etc/passwd", "a/b", "", "x" * 65, "demo 001"])
def test_bad_case_id_rejected(case_store, bad):
    with pytest.raises(ValueError, match="bad case_id"):
        case_store.create(bad)


def test_get_missing_raises(case_store):
    with pytest.raises(CaseNotFound):
        case_store.get("demo-404")


def test_get_returns_copy(case_store):
    case_store.create("demo-001")
    rec = case_store.get("demo-001")
    rec.state.stage = "stopped"  # not saved
    assert case_store.get("demo-001").state.stage == "intake"


def test_save_persists_and_survives_restart(case_store):
    rec = case_store.create("demo-001")
    rec.classifier = ClassifierOutput.model_validate(fixture("classifier_output.json"))
    rec.state.stage = "confirm_bucket"
    rec.state.waiting_on = "doctor"
    saved = case_store.save(rec)
    assert saved.state.updated_at >= rec.state.updated_at

    fresh = CaseStore(case_store.root)  # new process, empty cache
    again = fresh.get("demo-001")
    assert again.state.stage == "confirm_bucket"
    assert again.classifier == rec.classifier


def test_save_revalidates(case_store):
    rec = case_store.create("demo-001")
    rec.state.stage = "made_up_stage"  # type: ignore[assignment]
    with pytest.raises(ValueError):
        case_store.save(rec)
    assert case_store.get("demo-001").state.stage == "intake"


def test_save_unknown_case_fails(case_store, tmp_path):
    other = CaseStore(tmp_path / "other")
    rec = other.create("demo-009")
    with pytest.raises(CaseNotFound):
        case_store.save(rec)


def test_no_tmp_files_left(case_store):
    rec = case_store.create("demo-001")
    case_store.save(rec)
    assert sorted(p.name for p in case_store.root.iterdir()) == ["demo-001.json"]


def test_append_decision_saves_and_audits(case_store, audit_log):
    case_store.create("demo-001", suspected_bucket="glioma")
    decision = DoctorDecisionAdapter.validate_python(fixture("decision_confirm_bucket.json"))
    rec = case_store.append_decision(decision)
    assert len(rec.decisions) == 1 and rec.decisions[0].gate == "confirm_bucket"

    entry = audit_log.read("demo-001")[-1]
    assert entry.actor == "doctor" and entry.action == "decision:confirm_bucket"
    assert entry.detail["doctor_id"] == "dr-synthetic-01"
    assert entry.detail["decision"] == {"bucket": "glioma", "overrode_model": False}


def test_append_decision_unknown_case_fails(case_store):
    decision = DoctorDecisionAdapter.validate_python(fixture("decision_confirm_bucket.json"))
    with pytest.raises(CaseNotFound):
        case_store.append_decision(decision)


def test_enter_results_decision_round_trips(case_store):
    case_store.create("demo-001")
    decision = DoctorDecisionAdapter.validate_python(fixture("decision_enter_results.json"))
    case_store.append_decision(decision)
    again = CaseStore(case_store.root).get("demo-001")
    assert isinstance(again.decisions[0].decision, DoctorResults)


def test_list_and_reset(case_store, audit_log):
    case_store.create("demo-001")
    case_store.create("demo-002")
    assert {s.case_id for s in case_store.list_cases()} == {"demo-001", "demo-002"}
    assert case_store.reset() == 2
    assert case_store.list_cases() == []
    assert audit_log.read()[-1].action == "cases_reset"


def test_create_is_audited(case_store, audit_log):
    case_store.create("demo-001", suspected_bucket="meningioma")
    (entry,) = audit_log.read("demo-001")
    assert entry.action == "case_created"
    assert entry.detail == {"suspected_bucket": "meningioma"}
