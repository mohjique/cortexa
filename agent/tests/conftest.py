import pytest

from agent import audit, store, workspace

# The workspace every test runs in. Send WS_HEADERS on API calls; code run
# outside a request (fixtures, direct workflow calls) uses the same store and
# log, because `case_store` and `audit_log` are this workspace's, also set as
# the process defaults.
TEST_WS = "test-ws-0001"
WS_HEADERS = {workspace.WORKSPACE_HEADER: TEST_WS}


@pytest.fixture(autouse=True)
def workspaces(tmp_path):
    """Every test gets its own workspace folder, so nothing touches agent/data/."""
    reg = workspace.Registry(tmp_path / "workspaces")
    old = workspace.get_registry()
    workspace.set_registry(reg)
    yield reg
    workspace.set_registry(old)


@pytest.fixture
def test_ws(workspaces) -> workspace.Workspace:
    return workspaces.get(TEST_WS)


@pytest.fixture(autouse=True)
def audit_log(test_ws):
    """Every test gets its own audit log: TEST_WS's log, also the process default."""
    old = audit.get_audit_log()
    audit.set_audit_log(test_ws.log)
    yield test_ws.log
    audit.set_audit_log(old)


@pytest.fixture
def case_store(test_ws):
    old = store.get_store()
    store.set_store(test_ws.store)
    yield test_ws.store
    store.set_store(old)
