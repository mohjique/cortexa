"""The doctor can view the MRI itself: stored scans and the committed demo scans."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from agent.tests.conftest import WS_HEADERS
from agent import api
from agent.tests.test_upload import png

REPO = Path(__file__).resolve().parents[2]
PNG_SIG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def client(case_store, tmp_path, monkeypatch):
    from classifier.stub import StubClassifier

    monkeypatch.setattr(api, "HEATMAP_DIR", tmp_path / "heatmaps")
    api.set_classifier(StubClassifier())
    with TestClient(api.app, headers=WS_HEADERS) as c:
        yield c
    api.set_classifier(None)


def test_upload_keeps_the_scan_as_png(client, audit_log):
    assert client.post("/cases?case_id=demo-001&filename=glioma.png", content=png()).status_code == 201
    r = client.get("/cases/demo-001/image")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(r.content)).size == (256, 256)
    assert any(e.action == "scan_stored" and e.detail == {"stored": True} for e in audit_log.read("demo-001"))


def test_jpeg_upload_is_served_back_as_our_png(client):
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (90, 90, 90)).save(buf, format="JPEG")
    client.post("/cases?case_id=c-jpg&filename=glioma.jpg", content=buf.getvalue())
    body = client.get("/cases/c-jpg/image").content
    assert body.startswith(PNG_SIG)  # re-encoded: the uploaded bytes are never served


def test_unreadable_upload_still_makes_a_case_without_a_scan(client, audit_log):
    # The stub classifies any bytes; there is just nothing to show.
    assert client.post("/cases?case_id=c-raw&filename=glioma.png", content=b"not-an-image").status_code == 201
    assert client.get("/cases/c-raw/image").status_code == 404
    assert any(e.action == "scan_stored" and e.detail == {"stored": False} for e in audit_log.read("c-raw"))


def test_image_of_unknown_case_is_404(client):
    assert client.get("/cases/nope/image").status_code == 404


def test_demo_scans_match_the_demo_cases_and_hide_labels(client):
    demo = json.loads((REPO / "classifier" / "splits" / "demo_images.json").read_text(encoding="utf-8"))
    listed = client.get("/demo-scans").json()
    assert [s["case_id"] for s in listed] == sorted(d["case_id"] for d in demo)
    assert all(set(s) == {"case_id"} for s in listed)  # no label or model call to anchor the doctor
    for p in (REPO / "classifier" / "demo_scans").iterdir():
        name = p.name.lower()
        assert not any(w in name for w in ("glioma", "mening", "pituitar", "tumor", "no_"))


def test_demo_scan_is_served_unchanged(client):
    r = client.get("/demo-scans/demo-023")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content == (REPO / "classifier" / "demo_scans" / "demo-023.jpg").read_bytes()


@pytest.mark.parametrize("bad", ["demo-999", "..%2Fpolicies%2Fpolicy.yaml", "demo-001.jpg", "DEMO-001"])
def test_demo_scan_lookup_is_exact(client, bad):
    assert client.get(f"/demo-scans/{bad}").status_code == 404


def test_demo_scan_uploads_like_any_other(client):
    img = client.get("/demo-scans/demo-005").content
    assert client.post("/cases?case_id=demo-005&filename=demo-005.jpg", content=img).status_code == 201
    assert client.get("/cases/demo-005/image").status_code == 200
