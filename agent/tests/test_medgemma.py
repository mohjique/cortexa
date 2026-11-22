"""MedGemma wiring: rule-1 summary check, answer parsing, cached demo results at upload."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent import api, policy
from agent.findings import check_summary
from agent.reasoning import PATHWAY, check_reasoning, reasoning_prompt
from agent.schemas import ClassifierOutput
from agent.tests.test_upload import png
from classifier import medgemma

OK = "Axial T1 post-contrast slice with a well-circumscribed extra-axial mass along the left convexity, suspected meningioma."


@pytest.mark.parametrize("text", [
    OK,
    "Axial T2 slice. No mass or abnormal signal is visible.",
    "Enhancing intra-axial lesion in the right frontal lobe, consistent with a suspected high-grade glioma.",
])
def test_safe_summaries_pass(text):
    assert check_summary(text) is None


@pytest.mark.parametrize("text, why", [
    ("Findings are diagnostic of glioblastoma.", "forbidden"),
    ("This confirms a meningioma.", "forbidden"),
    ("Recommend surgical resection.", "forbidden"),
    ("Refer to a neurosurgeon.", "forbidden"),
    ("There is a meningioma in the left convexity.", "unhedged"),
    ("", "empty"),
])
def test_unsafe_summaries_rejected(text, why):
    assert why in (check_summary(text) or "")


@pytest.mark.parametrize("answer, bucket", [
    ("A", "glioma"), ("D) no tumor", "no_tumor"), ("(b)", "meningioma"),
    ("C", "pituitary"), ("Meningioma.", "meningioma"), ("glioma or meningioma", None), ("", None),
])
def test_parse_choice(answer, bucket):
    assert medgemma.parse_choice(answer) == bucket


def out(**kw):
    base = dict(case_id="x", suspected_bucket="meningioma",
                probabilities={"glioma": 0.001, "meningioma": 0.997, "pituitary": 0.001, "no_tumor": 0.001},
                confidence=0.997, flagged_uncertain=False)
    return ClassifierOutput(**(base | kw))


def test_schema_rejects_unsafe_summary():
    assert out(findings_summary=OK).findings_summary == OK
    with pytest.raises(ValidationError, match="rule 1"):
        out(findings_summary="Diagnosis: meningioma.")


# --- cached results at upload ---------------------------------------------------


@pytest.fixture
def cache(tmp_path, monkeypatch):
    d = tmp_path / "medgemma"
    d.mkdir()
    (d / "demo.json").write_text(json.dumps([
        {"case_id": "demo-008", "findings_summary": OK, "medgemma_bucket": "glioma",
         "reasoning": REASON, "reasoning_bucket": "meningioma"},
        {"case_id": "demo-009", "findings_summary": "Diagnosis: glioma.", "medgemma_bucket": "glioma"},
        {"case_id": "demo-010", "reasoning": REASON, "reasoning_bucket": "glioma"},
        {"case_id": "demo-011", "reasoning": "We recommend resection.", "reasoning_bucket": "meningioma"},
    ]), encoding="utf-8")
    monkeypatch.setattr(medgemma, "OUT_DIR", d)
    return d


@pytest.fixture
def client(case_store, cache, tmp_path, monkeypatch):
    from classifier.stub import StubClassifier

    monkeypatch.setattr(api, "HEATMAP_DIR", tmp_path / "heatmaps")
    api.set_classifier(StubClassifier())
    with TestClient(api.app) as c:
        yield c
    api.set_classifier(None)


def record(c, cid):
    return c.get(f"/cases/{cid}/record").json()["classifier"]


REASON = ("The slice shows a well-circumscribed extra-axial mass along the convexity, consistent "
          "with the suspected meningioma. The next step is the doctor's watch-or-treat decision.")


def test_reasoning_attached_for_the_resnet_bucket(client, audit_log):
    client.post("/cases?case_id=demo-008&filename=demo_meningioma.png", content=png())
    c = record(client, "demo-008")
    assert c["suspected_bucket"] == "meningioma"
    assert c["reasoning"] == REASON and c["findings_summary"] == OK
    assert c["medgemma_bucket"] is None  # MedGemma never votes


def test_reasoning_for_another_bucket_is_not_attached(client, audit_log):
    client.post("/cases?case_id=demo-010&filename=demo_meningioma.png", content=png())
    assert record(client, "demo-010")["reasoning"] is None
    assert "reasoning_skipped" in [e.action for e in audit_log.read("demo-010")]


def test_unsafe_cached_reasoning_dropped(client, audit_log):
    client.post("/cases?case_id=demo-011&filename=demo_meningioma.png", content=png())
    assert record(client, "demo-011")["reasoning"] is None
    assert "reasoning_rejected" in [e.action for e in audit_log.read("demo-011")]


@pytest.mark.parametrize("text", [
    REASON,
    "No mass or abnormal signal is visible, consistent with the suspected no tumor call. "
    "If the doctor confirms it, the case stops with no trial search.",
    "Enhancing lesion in the right frontal lobe, consistent with a suspected glioma. The pre-op "
    "screen lists what the biopsy must answer: IDH status, 1p/19q codeletion, MGMT and grade, "
    "before surgery and pathology.",
])
def test_safe_reasoning_passes(text):
    assert check_reasoning(text) is None


@pytest.mark.parametrize("text", [
    "The patient should have surgery.",
    "We recommend resection.",
    "This is diagnostic of glioblastoma.",
    "Refer to endocrinology.",
    "There is a meningioma along the convexity.",
    "",
])
def test_unsafe_reasoning_rejected(text):
    assert check_reasoning(text) is not None


@pytest.mark.parametrize("bucket", sorted(PATHWAY))
def test_prompt_carries_the_resnet_call_and_its_pathway(bucket):
    p = reasoning_prompt(bucket, 0.97, flagged=True)
    assert "suspected" in p and "0.97" in p and "flagged uncertain" in p
    assert all(step in p for step in PATHWAY[bucket])
    assert check_reasoning(" ".join(PATHWAY[bucket])) is None  # the pathway itself passes rule 1


@pytest.mark.parametrize("text", [
    "The image shows a lesion in the brain, which could be a glioma.",
    "The image does not show any features that would strongly suggest a meningioma.",
    "The next steps will determine the specific type of glioma and its grade.",
])
def test_hedged_reasoning_passes(text):
    assert check_reasoning(text) is None


@pytest.mark.parametrize("text", [
    "The doctor will order imaging to confirm the diagnosis.",
    "Next comes confirming the diagnosis of the glioma.",
    "This is a glioma.",
    # Not hedges: these state the tumor type as fact.
    "Imaging determines the lesion is a glioblastoma.",
    "Pathology determining the tumor is a meningioma.",
    "This is a glioma, whether or not surgery is done.",
])
def test_reasoning_that_claims_a_diagnosis_is_rejected(text):
    assert check_reasoning(text) is not None


def test_committed_demo_cache_passes_rule_1():
    path = medgemma.OUT_DIR / "demo.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    demo = {d["case_id"]: d for d in json.loads(medgemma.DEMO_JSON.read_text(encoding="utf-8"))}
    assert {r["case_id"] for r in rows} == set(demo)
    for r in rows:
        assert r["reasoning_bucket"] == demo[r["case_id"]]["model_pred"]  # written for the ResNet call
        if r["reasoning"]:
            assert check_reasoning(r["reasoning"]) is None
