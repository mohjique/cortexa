import json
from datetime import datetime

import pytest

from agent.audit import AuditLog, audit, audited


def test_append_writes_one_json_line(audit_log):
    audit("agent", "trial_search", {"trials_found": 9}, case_id="demo-001")
    audit("doctor", "decision:confirm_bucket", case_id="demo-002")
    lines = audit_log.path.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["actor"] == "agent" and first["detail"] == {"trials_found": 9}
    assert first["ts"].endswith("Z")


def test_read_filters_by_case(audit_log):
    audit("agent", "a", case_id="demo-001")
    audit("agent", "b", case_id="demo-002")
    audit("system", "policy_reloaded")
    assert [e.action for e in audit_log.read("demo-001")] == ["a"]
    assert len(audit_log.read()) == 3


def test_read_missing_file_is_empty(tmp_path):
    assert AuditLog(tmp_path / "nope.jsonl").read() == []


def test_log_is_append_only_across_instances(audit_log):
    audit("agent", "one")
    AuditLog(audit_log.path).append("agent", "two")
    assert [e.action for e in audit_log.read()] == ["one", "two"]


def test_detail_with_odd_types_is_jsonable(audit_log):
    audit("agent", "x", {"when": datetime(2026, 11, 21), "obj": object()})
    detail = audit_log.read()[0].detail
    assert detail["when"] == "2026-11-21T00:00:00"
    assert detail["obj"].startswith("<object")


def test_bad_actor_rejected():
    with pytest.raises(ValueError):
        audit("llm", "x")  # type: ignore[arg-type]


def test_audited_logs_args_result_and_case(audit_log):
    @audited("geocode_zip")
    def geocode(zip: str, case_id: str | None = None) -> dict:
        return {"lat": 30.6, "lon": -96.3}

    assert geocode("77840", case_id="demo-001") == {"lat": 30.6, "lon": -96.3}
    (entry,) = audit_log.read()
    assert entry.action == "geocode_zip" and entry.case_id == "demo-001"
    assert entry.detail["args"] == {"zip": "77840", "case_id": "demo-001"}
    assert entry.detail["ok"] is True and "ms" in entry.detail


def test_audited_logs_and_reraises_errors(audit_log):
    @audited("trial_search")
    def search():
        raise TimeoutError("ctgov down")

    with pytest.raises(TimeoutError):
        search()
    (entry,) = audit_log.read()
    assert entry.detail["ok"] is False
    assert entry.detail["error"] == "TimeoutError: ctgov down"


def _broken_audit(monkeypatch, audit_log):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(audit_log, "append", boom)


def test_audited_keeps_tool_error_when_audit_also_fails(audit_log, monkeypatch):
    _broken_audit(monkeypatch, audit_log)

    @audited("trial_search")
    def search():
        raise TimeoutError("ctgov down")

    with pytest.raises(TimeoutError, match="ctgov down") as info:
        search()
    assert any("audit write also failed: OSError: disk full" in n for n in info.value.__notes__)


def test_audited_success_with_failed_audit_raises(audit_log, monkeypatch):
    """Rule 7: a tool call that can't be logged must not pass silently."""
    _broken_audit(monkeypatch, audit_log)

    @audited("geocode_zip")
    def geocode():
        return {"lat": 1.0}

    with pytest.raises(OSError, match="disk full"):
        geocode()


def test_audited_shrinks_big_results(audit_log):
    @audited("trial_search")
    def search():
        return [{"trial_id": f"NCT990000{i:02d}", "title": "x" * 50} for i in range(50)]

    search()
    assert audit_log.read()[0].detail["result"] == {"type": "list", "len": 50}
