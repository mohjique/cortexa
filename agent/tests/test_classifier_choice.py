"""CORTEXA_CLASSIFIER: an explicit choice loads at startup and never falls back to the stub silently."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.tests.conftest import WS_HEADERS
from agent import api
from classifier import real


@pytest.fixture(autouse=True)
def fresh(case_store, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "HEATMAP_DIR", tmp_path / "heatmaps")
    api.set_classifier(None)
    yield
    api.set_classifier(None)


def test_real_without_model_fails_the_boot(monkeypatch, tmp_path):
    monkeypatch.setenv("CORTEXA_CLASSIFIER", "real")
    monkeypatch.setattr(real, "DEFAULT_MODEL", tmp_path / "missing.pt")
    with pytest.raises(RuntimeError, match="no model at"):
        with TestClient(api.app):
            pass


def test_unknown_choice_fails_the_boot(monkeypatch):
    monkeypatch.setenv("CORTEXA_CLASSIFIER", "rea1")
    with pytest.raises(RuntimeError, match="must be 'real' or 'stub'"):
        with TestClient(api.app):
            pass


def test_explicit_stub_loads_at_startup_and_shows_in_health(monkeypatch, audit_log):
    monkeypatch.setenv("CORTEXA_CLASSIFIER", "stub")
    monkeypatch.delenv("CORTEXA_TRIAL_SOURCE", raising=False)
    with TestClient(api.app, headers=WS_HEADERS) as c:
        assert c.get("/health").json() == {"status": "ok", "classifier": "StubClassifier", "trial_source": "demo"}
    assert any(e.action == "classifier_loaded" for e in audit_log.read())


def test_no_choice_loads_lazily(monkeypatch):
    monkeypatch.delenv("CORTEXA_CLASSIFIER", raising=False)
    with TestClient(api.app, headers=WS_HEADERS) as c:
        assert c.get("/health").json()["classifier"] == "not_loaded"


@pytest.mark.skipif(not real.DEFAULT_MODEL.exists(), reason="needs artifacts/classifier/model.pt")
def test_explicit_real_loads_resnet(monkeypatch):
    pytest.importorskip("torchvision")
    monkeypatch.setenv("CORTEXA_CLASSIFIER", "real")
    with TestClient(api.app, headers=WS_HEADERS) as c:
        assert c.get("/health").json()["classifier"] == "ResNetClassifier"


def test_health_shows_live_trial_source(monkeypatch):
    """The banner reads this to say whether trials are real registry records or the synthetic demo set."""
    monkeypatch.setenv("CORTEXA_CLASSIFIER", "stub")
    monkeypatch.setenv("CORTEXA_TRIAL_SOURCE", "live")
    with TestClient(api.app, headers=WS_HEADERS) as c:
        assert c.get("/health").json()["trial_source"] == "live"
