"""MedGemma wiring: rule-1 summary check, answer parsing, cached demo results at upload."""

from __future__ import annotations

import csv
import json
import re

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent import api, policy
from agent.findings import check_summary
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
        {"case_id": "demo-008", "findings_summary": OK, "medgemma_bucket": "glioma"},
        {"case_id": "demo-009", "findings_summary": "Diagnosis: glioma.", "medgemma_bucket": "glioma"},
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


def test_summary_attached_vote_off_by_default(client):
    client.post("/cases?case_id=demo-008&filename=demo_meningioma.png", content=png())
    c = record(client, "demo-008")
    assert c["findings_summary"] == OK
    assert c["medgemma_bucket"] != "glioma"  # cached call not applied: medgemma_vote is false


def test_unsafe_cached_summary_dropped(client, audit_log):
    client.post("/cases?case_id=demo-009&filename=demo_glioma.png", content=png())
    assert record(client, "demo-009")["findings_summary"] is None
    assert "findings_summary_rejected" in [e.action for e in audit_log.read("demo-009")]


def test_no_cache_entry_is_a_no_op(client):
    client.post("/cases?case_id=other&filename=demo_glioma.png", content=png())
    assert record(client, "other")["findings_summary"] is None


def test_vote_on_flags_disagreement(client, tmp_path):
    old = policy._default
    path = tmp_path / "policy.yaml"
    path.write_text(re.sub(r"medgemma_vote: false", "medgemma_vote: true",
                           policy.POLICY_PATH.read_text(encoding="utf-8")), encoding="utf-8")
    policy.set_policy_store(policy.PolicyStore(path))
    try:
        client.post("/cases?case_id=demo-008&filename=demo_meningioma.png", content=png())
        c = record(client, "demo-008")
        assert c["medgemma_bucket"] == "glioma"  # disagrees with the stub's meningioma
        assert c["flagged_uncertain"] is True
    finally:
        policy.set_policy_store(old)


def test_finetune_rows_come_from_train_split_only():
    rows = medgemma.pick_train()
    assert rows and all(r["split"] == "train" for r in rows)
    with medgemma.SPLIT_CSV.open(encoding="utf-8") as f:
        held_out = {r["path"] for r in csv.DictReader(f) if r["split"] != "train"}
    demo = {d["image_file"] for d in json.loads(medgemma.DEMO_JSON.read_text(encoding="utf-8"))}
    paths = {r["path"] for r in rows}
    assert not paths & held_out and not paths & demo


def test_finetune_rows_weighted_toward_hard_classes():
    from collections import Counter

    rows = medgemma.pick_train()
    n = Counter(r["label"] for r in rows)
    assert n == Counter(medgemma.TRAIN_COUNTS)
    assert n["glioma"] > n["no_tumor"] and n["meningioma"] > n["no_tumor"]
    variants = sum(r["origin"] not in medgemma.CLEAN_ORIGINS for r in rows)
    assert 0 < variants < len(rows)
    assert len({r["path"] for r in rows}) == len(rows)  # no repeats
    assert medgemma.pick_train() == rows  # seeded: same rows on every machine
    assert set(medgemma.LETTER) == set(medgemma.CHOICES.values())
    assert all(medgemma.CHOICES[medgemma.LETTER[b]] == b for b in medgemma.LETTER)
