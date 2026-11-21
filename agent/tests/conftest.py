import pytest

from agent import audit, store


@pytest.fixture(autouse=True)
def audit_log(tmp_path):
    """Every test gets its own audit log, so nothing touches agent/data/."""
    log = audit.AuditLog(tmp_path / "audit.jsonl")
    old = audit.get_audit_log()
    audit.set_audit_log(log)
    yield log
    audit.set_audit_log(old)


@pytest.fixture
def case_store(tmp_path):
    s = store.CaseStore(tmp_path / "cases")
    old = store.get_store()
    store.set_store(s)
    yield s
    store.set_store(old)
