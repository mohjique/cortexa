"""MRI upload -> classifier -> confirm_bucket (F2.5), and the F2.4 uncertainty flag."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from agent.tests.conftest import WS_HEADERS
from agent import api, policy, workflow
from agent.schemas import ClassifierOutput


def png() -> bytes:
    img = Image.new("L", (256, 256), 0)
    ImageDraw.Draw(img).ellipse((50, 40, 206, 216), fill=160)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def heatmaps(tmp_path, monkeypatch):
    d = tmp_path / "heatmaps"
    monkeypatch.setattr(api, "HEATMAP_DIR", d)
    return d


@pytest.fixture
def stub_client(case_store, heatmaps):
    from classifier.stub import StubClassifier

    api.set_classifier(StubClassifier())
    with TestClient(api.app, headers=WS_HEADERS) as c:
        yield c
    api.set_classifier(None)


@pytest.fixture
def real_client(case_store, heatmaps, tmp_path):
    """Real classifier code path on a random-weight checkpoint (no trained model needed)."""
    from classifier.model import build_model, save_checkpoint
    from classifier.real import ResNetClassifier

    ck = tmp_path / "model.pt"
    save_checkpoint(ck, build_model(pretrained=False), temperature=1.0, meta={})
    api.set_classifier(ResNetClassifier(ck, heatmap_dir=heatmaps))
    with TestClient(api.app, headers=WS_HEADERS) as c:
        yield c
    api.set_classifier(None)


def out(**kw) -> ClassifierOutput:
    base = dict(case_id="x", suspected_bucket="glioma",
                probabilities={"glioma": 0.995, "meningioma": 0.003, "pituitary": 0.001, "no_tumor": 0.001},
                confidence=0.995, flagged_uncertain=False)
    return ClassifierOutput(**(base | kw))


# --- F2.4: flag from policy -------------------------------------------------------


def test_flag_from_policy_threshold():
    p = policy.get_policy()
    thr = p.classifier.min_confidence_for_auto_suggest
    assert workflow.uncertainty_flag(out(confidence=min(1.0, thr + 0.001)), p) == (False, [])
    flagged, reasons = workflow.uncertainty_flag(out(confidence=thr - 0.01), p)
    assert flagged and reasons[0].startswith("confidence")


def test_medgemma_bucket_is_dropped_and_never_flags():
    # MedGemma does not vote: a second-model bucket can neither survive the
    # schema nor flag the case.
    o = out(medgemma_bucket="meningioma")
    assert o.medgemma_bucket is None
    assert workflow.uncertainty_flag(o, policy.get_policy()) == (False, [])


def test_agent_never_clears_classifier_flag():
    flagged, reasons = workflow.uncertainty_flag(out(flagged_uncertain=True), policy.get_policy())
    assert flagged and reasons == ["classifier_flag"]


def test_threshold_edit_in_policy_changes_flag(tmp_path):
    old = policy._default
    path = tmp_path / "policy.yaml"
    text = policy.POLICY_PATH.read_text(encoding="utf-8")
    import re

    path.write_text(re.sub(r"min_confidence_for_auto_suggest: [0-9.]+", "min_confidence_for_auto_suggest: 0.5",
                           text), encoding="utf-8")
    policy.set_policy_store(policy.PolicyStore(path))
    try:
        assert workflow.uncertainty_flag(out(confidence=0.6), policy.get_policy())[0] is False
    finally:
        policy.set_policy_store(old)


def test_attach_recomputes_flag(case_store, audit_log):
    case_store.create("c9")
    rec = workflow.attach_classification("c9", out(case_id="c9", confidence=0.5,
        probabilities={"glioma": 0.5, "meningioma": 0.3, "pituitary": 0.1, "no_tumor": 0.1}))
    assert rec.classifier and rec.classifier.flagged_uncertain is True
    e = [e for e in audit_log.read("c9") if e.action == "classification_attached"][-1]
    assert e.detail["flagged_uncertain"] is True and e.detail["flag_reasons"]


# --- upload endpoint --------------------------------------------------------------


def test_upload_with_stub_moves_to_confirm_bucket(stub_client):
    r = stub_client.post("/cases?case_id=demo-016&filename=demo_glioma.png", content=png())
    assert r.status_code == 201
    st = r.json()
    assert st["case_id"] == "demo-016" and st["stage"] == "confirm_bucket"
    assert st["suspected_bucket"] == "glioma"


def test_upload_generates_case_id(stub_client):
    r = stub_client.post("/cases", content=png())
    assert r.status_code == 201 and r.json()["case_id"].startswith("case-")


def test_upload_errors(stub_client):
    assert stub_client.post("/cases", content=b"").status_code == 422
    assert stub_client.post("/cases?case_id=bad id!", content=png()).status_code == 422
    assert stub_client.post("/cases?case_id=dup", content=png()).status_code == 201
    assert stub_client.post("/cases?case_id=dup", content=png()).status_code == 409


def test_upload_with_real_classifier_and_heatmap(real_client, heatmaps, test_ws):
    r = real_client.post("/cases?case_id=r1", content=png())
    assert r.status_code == 201 and r.json()["stage"] == "confirm_bucket"
    rec = real_client.get("/cases/r1/record").json()
    c = rec["classifier"]
    assert c["medgemma_bucket"] is None
    assert abs(sum(c["probabilities"].values()) - 1) < 0.01
    assert c["confidence"] == max(c["probabilities"].values())
    h = real_client.get("/cases/r1/heatmap")
    assert h.status_code == 200 and h.headers["content-type"] == "image/png"
    assert (test_ws.heatmap_dir / "r1.png").is_file()  # moved into the workspace
    assert not (heatmaps / "r1.png").exists()


def test_bad_image_leaves_no_case(real_client):
    r = real_client.post("/cases?case_id=r2", content=b"not an image")
    assert r.status_code == 422
    assert real_client.get("/cases/r2").status_code == 404


def test_heatmap_404_without_one(stub_client):
    stub_client.post("/cases?case_id=s1&filename=demo_glioma.png", content=png())
    assert stub_client.get("/cases/s1/heatmap").status_code == 404
