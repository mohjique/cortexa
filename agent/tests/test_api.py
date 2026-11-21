import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent import workflow
from agent.api import app
from agent.schemas import ClassifierOutput

FIXTURES = Path(__file__).parent / "fixtures"
CID = "c1"


@pytest.fixture
def client(case_store):
    case_store.create(CID)
    out = json.loads((FIXTURES / "classifier_output.json").read_text()) | {"case_id": CID}
    workflow.attach_classification(CID, ClassifierOutput.model_validate(out))
    with TestClient(app) as c:
        yield c


def body(gate, decision, case_id=CID):
    return {
        "case_id": case_id,
        "gate": gate,
        "decision": decision,
        "doctor_id": "dr-synthetic-01",
        "ts": "2026-11-21T14:05:40Z",
    }


def test_get_case(client):
    r = client.get(f"/cases/{CID}")
    assert r.status_code == 200 and r.json()["stage"] == "confirm_bucket"


def test_missing_case_404(client):
    assert client.get("/cases/nope").status_code == 404


def test_post_decision_advances(client):
    r = client.post(
        f"/cases/{CID}/decisions",
        json=body("confirm_bucket", {"bucket": "glioma", "overrode_model": False}),
    )
    assert r.status_code == 200 and r.json()["stage"] == "preop_screen"
    assert r.json()["waiting_on"] == "doctor"
    assert client.get(f"/cases/{CID}/record").json()["preop"] is not None
    r = client.post(
        f"/cases/{CID}/decisions",
        json=body("preop_review", {"tests_reviewed": True, "pre_surgery_trials": []}),
    )
    assert r.status_code == 200 and r.json()["stage"] == "awaiting_pathology"


def test_wrong_gate_409(client):
    r = client.post(f"/cases/{CID}/decisions", json=body("hormone_labs", {"labs_reviewed": True}))
    assert r.status_code == 409


def test_malformed_decision_422(client):
    r = client.post(f"/cases/{CID}/decisions", json=body("confirm_bucket", {"bucket": "tumor"}))
    assert r.status_code == 422


def test_case_id_mismatch_422(client):
    r = client.post(
        f"/cases/{CID}/decisions",
        json=body("confirm_bucket", {"bucket": "glioma", "overrode_model": False}, case_id="c2"),
    )
    assert r.status_code == 422


def test_audit_endpoint(client):
    acts = [e["action"] for e in client.get(f"/cases/{CID}/audit").json()]
    assert "classification_attached" in acts
