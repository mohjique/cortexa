"""Per-browser workspaces (issue 48): one demo visitor can't see or change another's cases.

Isolation, not authentication: the workspace ID is a random token the browser
keeps; the data is synthetic (rule 9).
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent import api, store
from agent.schemas import ClassifierOutput
from agent.tests.test_upload import png
from agent.workspace import WORKSPACE_HEADER, InvalidWorkspace, Registry, validate_id

WS_A = "aaaaaaaa-0000-4000-8000-000000000001"
WS_B = "bbbbbbbb-0000-4000-8000-000000000002"
CID = "iso-01"
GLIOMA = {"bucket": "glioma", "overrode_model": False}


class HeatmapClassifier:
    """Stands in for the ResNet: always glioma, writes a heatmap where the real one does."""

    def __init__(self, heatmap_dir: Path) -> None:
        self.heatmap_dir = heatmap_dir

    def classify(self, case_id: str, image: bytes, filename: str | None = None) -> ClassifierOutput:
        self.heatmap_dir.mkdir(parents=True, exist_ok=True)
        p = self.heatmap_dir / f"{case_id}.png"
        p.write_bytes(png())
        return ClassifierOutput(
            case_id=case_id, suspected_bucket="glioma",
            probabilities={"glioma": 0.995, "meningioma": 0.003, "pituitary": 0.001, "no_tumor": 0.001},
            confidence=0.995, flagged_uncertain=False, heatmap_path=p.as_posix(),
        )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "HEATMAP_DIR", tmp_path / "staging-heatmaps")
    api.set_classifier(HeatmapClassifier(tmp_path / "staging-heatmaps"))
    with TestClient(api.app) as c:
        yield c
    api.set_classifier(None)


def h(ws: str) -> dict[str, str]:
    return {WORKSPACE_HEADER: ws}


def upload(client, ws: str, case_id: str = CID):
    r = client.post(f"/cases?case_id={case_id}&filename=scan.png", content=png(), headers=h(ws))
    assert r.status_code == 201, r.text
    return r.json()


def decide(client, ws: str, gate: str, decision: dict, case_id: str = CID):
    body = {"case_id": case_id, "gate": gate, "decision": decision,
            "doctor_id": "dr-synthetic-01", "ts": "2026-11-21T14:05:40Z"}
    return client.post(f"/cases/{case_id}/decisions", json=body, headers=h(ws))


def b_snapshot(client):
    """B's state, as B sees it."""
    return (client.get(f"/cases/{CID}/record", headers=h(WS_B)).json(),
            client.get(f"/cases/{CID}/audit", headers=h(WS_B)).json())


# -- isolation -----------------------------------------------------------------


def test_other_workspace_sees_nothing_and_changes_nothing(client):
    upload(client, WS_B)
    before = b_snapshot(client)
    assert client.get(f"/cases/{CID}/heatmap", headers=h(WS_B)).status_code == 200
    assert client.get(f"/cases/{CID}/image", headers=h(WS_B)).status_code == 200

    assert client.get("/cases", headers=h(WS_A)).json() == []
    for path in ("", "/record", "/audit", "/image", "/heatmap"):
        r = client.get(f"/cases/{CID}{path}", headers=h(WS_A))
        assert r.status_code == 404, path  # 404, not 403: existence is not leaked
    assert decide(client, WS_A, "confirm_bucket", GLIOMA).status_code == 404
    rescreen = {"doctor_id": "dr-synthetic-01"}
    assert client.post(f"/cases/{CID}/preop/rescreen", json=rescreen, headers=h(WS_A)).status_code == 404
    assert client.post(f"/cases/{CID}/run", headers=h(WS_A)).status_code == 404
    # The image routes' query param can't reach B either.
    assert client.get(f"/cases/{CID}/image?ws={WS_A}").status_code == 404
    assert client.get(f"/cases/{CID}/heatmap?ws={WS_A}").status_code == 404

    assert b_snapshot(client) == before
    assert [c["case_id"] for c in client.get("/cases", headers=h(WS_B)).json()] == [CID]


def test_same_case_id_in_two_workspaces_is_independent(client):
    upload(client, WS_A)
    upload(client, WS_B)  # no 409: each workspace has its own case IDs
    r = decide(client, WS_A, "confirm_bucket", GLIOMA)
    assert r.status_code == 200 and r.json()["stage"] == "preop_screen"
    b = client.get(f"/cases/{CID}", headers=h(WS_B)).json()
    assert b["stage"] == "confirm_bucket" and b["confirmed_bucket"] is None
    assert client.get(f"/cases/{CID}/record", headers=h(WS_B)).json()["decisions"] == []


def test_each_workspace_serves_its_own_heatmap_and_scan(client, workspaces):
    upload(client, WS_A)
    upload(client, WS_B)
    a, b = workspaces.get(WS_A), workspaces.get(WS_B)
    assert (a.heatmap_dir / f"{CID}.png").is_file() and (b.heatmap_dir / f"{CID}.png").is_file()
    assert (a.scan_dir / f"{CID}.png").is_file() and (b.scan_dir / f"{CID}.png").is_file()
    rec_a = client.get(f"/cases/{CID}/record", headers=h(WS_A)).json()
    assert Path(rec_a["classifier"]["heatmap_path"]).parent == a.heatmap_dir
    # A heatmap path pointing into another workspace is never served.
    with b.active():
        rec = store.get_store().get(CID)
        rec.classifier = rec.classifier.model_copy(
            update={"heatmap_path": (a.heatmap_dir / f"{CID}.png").as_posix()})
        store.get_store().save(rec)
    assert client.get(f"/cases/{CID}/heatmap", headers=h(WS_B)).status_code == 404


def test_image_routes_accept_the_query_param(client):
    upload(client, WS_A)
    assert client.get(f"/cases/{CID}/image?ws={WS_A}").status_code == 200
    assert client.get(f"/cases/{CID}/heatmap?ws={WS_A}").status_code == 200
    # Only the image routes: other case routes need the header.
    assert client.get(f"/cases/{CID}?ws={WS_A}").status_code == 400
    assert client.get(f"/cases?ws={WS_A}").status_code == 400


# -- bad workspace IDs ---------------------------------------------------------


@pytest.mark.parametrize("bad", [None, "", "short", "../../etc", "..%2F..%2Fx", "a/b/c/d/e/f",
                                 "abc.defgh", "x" * 65, "aaaaaaaa bbbb", "aaaaaaaa\\..\\x"])
def test_missing_or_invalid_workspace_is_400(client, bad, workspaces):
    headers = {} if bad is None else h(bad)
    for method, path in [("get", "/cases"), ("get", f"/cases/{CID}"), ("get", f"/cases/{CID}/record"),
                         ("get", f"/cases/{CID}/audit"), ("post", f"/cases/{CID}/run"),
                         ("post", "/workspace/reset"), ("get", f"/cases/{CID}/image")]:
        assert getattr(client, method)(path, headers=headers).status_code == 400, (bad, path)
    r = client.post(f"/cases?case_id={CID}", content=png(), headers=headers)
    assert r.status_code == 400
    if bad:
        r = client.get(f"/cases/{CID}/image", params={"ws": bad})
        assert r.status_code == 400
    # Nothing was created outside the workspaces folder.
    assert all(p.name in ("test-ws-0001",) for p in workspaces.root.iterdir())


def test_global_routes_need_no_workspace(client):
    assert client.get("/health").status_code == 200
    assert client.get("/demo-scans").status_code == 200
    assert client.get("/demo-scans/demo-001").status_code == 200


def test_validate_id_lowercases():
    assert validate_id("ABCDEFGH-1234") == "abcdefgh-1234"
    with pytest.raises(InvalidWorkspace):
        validate_id("..")


def test_cors_allows_the_workspace_header(client):
    r = client.options("/cases", headers={
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": WORKSPACE_HEADER.lower(),
    })
    assert r.status_code == 200
    assert WORKSPACE_HEADER.lower() in r.headers["access-control-allow-headers"].lower()


# -- audit log (rule 7) -------------------------------------------------------------


def test_audit_entries_land_only_in_the_callers_log(client, workspaces, audit_log):
    upload(client, WS_A)
    upload(client, WS_B)
    b_before = workspaces.get(WS_B).log.read()
    assert decide(client, WS_A, "confirm_bucket", GLIOMA).status_code == 200  # runs the pre-op agent

    a_log = workspaces.get(WS_A).log.read()
    actions = [e.action for e in a_log]
    assert "decision:confirm_bucket" in actions  # doctor approval
    assert "trial_search" in actions  # @audited agent tool call, deep inside the workflow
    assert "tool_call:pre_surgery_detection" in actions
    assert workspaces.get(WS_B).log.read() == b_before  # nothing from A's request
    assert not any(e.action == "trial_search" for e in audit_log.read())  # nor in the test default log


def test_workspace_creation_is_audited_once(client, workspaces):
    client.get("/cases", headers=h(WS_A))
    client.get("/cases", headers=h(WS_A))
    created = [e for e in workspaces.get(WS_A).log.read() if e.action == "workspace_created"]
    assert len(created) == 1 and created[0].actor == "system"


# -- seeding and reset ------------------------------------------------------------


def test_lazy_seeding_is_idempotent_and_safe_under_concurrency(tmp_path):
    reg = Registry(tmp_path / "ws")
    assert reg.ids() == []
    results = []

    def first_request():
        results.append(reg.open(WS_A))

    threads = [threading.Thread(target=first_request) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(created for _, created in results) == 1  # seeded exactly once
    assert len({id(ws) for ws, _ in results}) == 1  # one shared store and lock
    assert reg.ids() == [WS_A]
    marker = (tmp_path / "ws" / WS_A / "workspace.json").read_text()
    assert reg.open(WS_A)[1] is False
    assert (tmp_path / "ws" / WS_A / "workspace.json").read_text() == marker
    # A new process sees the existing workspace as seeded.
    assert Registry(tmp_path / "ws").open(WS_A)[1] is False


def test_reset_clears_and_reseeds_only_the_callers_workspace(client, workspaces):
    upload(client, WS_A)
    upload(client, WS_A, "iso-02")
    upload(client, WS_B)
    r = client.post("/workspace/reset", headers=h(WS_A))
    assert r.status_code == 200 and r.json() == {"status": "reset", "removed_cases": 2}
    assert client.get("/cases", headers=h(WS_A)).json() == []
    assert client.get(f"/cases/{CID}", headers=h(WS_A)).status_code == 404
    a = workspaces.get(WS_A)
    assert not (a.scan_dir / f"{CID}.png").exists() and not (a.heatmap_dir / f"{CID}.png").exists()
    log = a.log.read()
    assert [e.action for e in log] == ["workspace_reset"]  # first entry of the fresh log
    assert log[0].detail == {"removed_cases": 2} and log[0].actor == "system"
    # B untouched, and A works again with the same case ID.
    assert [c["case_id"] for c in client.get("/cases", headers=h(WS_B)).json()] == [CID]
    upload(client, WS_A)


# -- eviction -----------------------------------------------------------------------


def _age(reg: Registry, ws_id: str, hours: float) -> None:
    t = time.time() - hours * 3600
    os.utime(reg.root / ws_id / "workspace.json", (t, t))
    reg._touched.pop(ws_id, None)


def test_stale_workspaces_are_evicted_when_a_new_one_starts(tmp_path):
    reg = Registry(tmp_path / "ws", max_workspaces=10, ttl_hours=24)
    old, fresh = reg.get("old-workspace-1"), reg.get("fresh-workspace-1")
    with old.active():
        store.get_store().create("c1")
    _age(reg, "old-workspace-1", 25)
    _age(reg, "fresh-workspace-1", 23)
    reg.get("new-workspace-1")
    assert reg.ids() == ["fresh-workspace-1", "new-workspace-1"]
    assert not (tmp_path / "ws" / "old-workspace-1").exists()
    # Coming back after eviction just starts a fresh, empty workspace.
    again, created = reg.open("old-workspace-1")
    assert created and again.store.list_cases() == []


def test_least_recently_used_beyond_the_cap_are_evicted(tmp_path):
    reg = Registry(tmp_path / "ws", max_workspaces=3, ttl_hours=24)
    for i in range(3):
        reg.get(f"workspace-{i:04d}")
        _age(reg, f"workspace-{i:04d}", 3 - i)  # 0 is the oldest
    reg.get("workspace-0003")
    assert reg.ids() == ["workspace-0001", "workspace-0002", "workspace-0003"]
    # Using a workspace keeps it: 1 is touched, so 2 is now the oldest.
    reg._touched.pop("workspace-0001", None)
    reg.get("workspace-0001")
    _age(reg, "workspace-0002", 5)
    reg.get("workspace-0004")
    assert reg.ids() == ["workspace-0001", "workspace-0003", "workspace-0004"]


def test_eviction_never_removes_the_workspace_being_opened(tmp_path):
    reg = Registry(tmp_path / "ws", max_workspaces=1, ttl_hours=0)
    reg.get("only-workspace")
    reg.get("only-workspace")
    assert reg.ids() == ["only-workspace"]
    reg.get("next-workspace")
    assert reg.ids() == ["next-workspace"]
