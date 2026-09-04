"""Slow work must not run on the event loop.

Several services call out over the network with a synchronous client: the
embedding backend, the review model, connector actions, webhook deliveries.
Awaiting none of that, a single request to a hung endpoint stalls every other
request in the process — and since the supported deployment is one worker,
"the process" is the whole installation.

The fix is not to make the service layer async; it is to hand the synchronous
call to a thread at the route boundary. These tests assert that the boundary
is where it should be, because a reverted `await asyncio.to_thread(...)` is
invisible until something hangs in production.
"""

import ast
import pathlib

import pytest

# Everything in app/routes that reaches the network, a subprocess, or a sleep.
# Names rather than call sites, because the check below walks every call.
BLOCKING = frozenset(
    {
        # Outbound HTTP, a subprocess, or a retry that sleeps.
        "search_memory",
        "write_memory",
        "assess_memory_write",
        "safe_backend_status",
        "verify_scope",
        "verify_record",
        "test_binding",
        "execute_binding_action_with_logging",
        "import_spec",
        "discover_mcp_server",
        "refresh_mcp_server",
        "install_adapter",
        "update_adapter",
        "uninstall_adapter",
        "list_available_adapters",
        "generate_connector_type_tools",
        "build_authorization_url",
        "exchange_callback",
        "_fetch_directory",
        "test_delivery",
        "review_scope",
        "test_vector_connection",
        # Long-running local work: zipping and re-encrypting the whole
        # database, rewriting every credential under a new key, walking the
        # corpus. No network involved, and just as capable of stalling the
        # single supported worker.
        "build_encrypted_backup_package",
        "decrypt_backup_package",
        "build_backup_zip",
        "restore_from_zip",
        "merge_restore_from_zip",
        "rotate_key",
        "restore_key",
        "rotate_broker_credential",
        "run_scheduled_maintenance",
        "run_startup_checks",
        "export_memory_csv",
        "export_memory_jsonl",
        "export_credentials_metadata",
        "export_audit_csv",
        "generate_handoff_briefing",
        "generate_prd_handoff_briefing",
        "generate_proposals",
    }
)

# This list is curated, and that is a deliberate limitation worth stating.
# Deriving it from the services was tried and does not work: the analysis is
# name-based, so `conn.execute` and a connector's `execute` are the same
# symbol, and one round of call-graph propagation marks 221 functions
# including `write_event`. Offloading every SQLite read would be a far larger
# change than the problem justifies. So the rule is narrower and honest: any
# service call that reaches the network, spawns a process, sleeps, or rewrites
# the database wholesale belongs here, and the test below proves none of them
# runs on the loop.


def _async_functions(tree):
    return [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]


def _offloaded_calls(tree):
    """Call nodes that ARE the first argument of an asyncio.to_thread call."""
    offloaded = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "to_thread" and node.args:
            offloaded.add(id(node.args[0]))
    return offloaded


def _call_name(node):
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", "")


ROUTE_FILES = sorted(pathlib.Path("app/routes").glob("*.py"))


@pytest.mark.parametrize("path", ROUTE_FILES, ids=lambda p: p.name)
def test_no_async_handler_calls_blocking_work_directly(path):
    """Walk every call, not every name.

    The earlier version of this test asked whether a name appeared somewhere in
    a to_thread call in the same module. That passes while a second call site
    of the same function still runs on the loop, and says nothing at all about
    modules it was not told to look at. This looks at each call node and asks
    whether that node is the one being offloaded.
    """
    tree = ast.parse(path.read_text())
    offloaded = _offloaded_calls(tree)

    on_the_loop = []
    for fn in _async_functions(tree):
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name in BLOCKING and id(node) not in offloaded:
                on_the_loop.append(f"{name} at line {node.lineno}")

    assert not on_the_loop, (
        f"{path} runs blocking work on the event loop: {on_the_loop}"
    )


def test_the_audit_covers_every_route_module():
    """No hand-maintained list to fall out of date."""
    assert len(ROUTE_FILES) > 10
    assert any(p.name == "overview_page.py" for p in ROUTE_FILES), (
        "dashboard pages were missed entirely by the previous list"
    )


def test_the_retry_sleep_stays_off_the_loop():
    """The connector retry sleeps between attempts.

    That is fine inside a worker thread and unacceptable on the event loop, so
    it is allowed to remain synchronous only because every caller reaches it
    through `asyncio.to_thread`.
    """
    source = pathlib.Path("app/services/connector_service.py").read_text()
    assert "time.sleep(delay)" in source, "the retry backoff moved; recheck its callers"

    for path in ("app/routes/connectors.py", "app/routes/mcp.py"):
        tree = ast.parse(pathlib.Path(path).read_text())
        offloaded = _offloaded_calls(tree)
        for fn in _async_functions(tree):
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and _call_name(node) == "execute_binding_action_with_logging"
                ):
                    assert id(node) in offloaded, (
                        f"{path}:{node.lineno} would run the retry backoff on the loop"
                    )


def test_a_slow_connector_does_not_block_other_requests(test_client, admin_token):
    """The end-to-end version of the same claim.

    A connector action that takes a second is served while an unrelated request
    is answered from another thread. On the event loop the second request could
    not start until the first finished.
    """
    import threading
    import time

    from app.services import connector_service

    started = threading.Event()
    release = threading.Event()

    def slow(binding_id, action, params=None):
        started.set()
        release.wait(timeout=5)
        return {"success": True, "data": {}}

    original = connector_service.execute_binding_action_with_logging
    connector_service.execute_binding_action_with_logging = slow
    try:
        from app.database import get_db

        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO connector_types (id, display_name, auth_type,"
                " supported_actions_json, required_credential_fields_json,"
                " backend_type, is_active)"
                " VALUES ('t','T','none','[\"GET /x\"]','[]','generic_http',1)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO connector_bindings (id, connector_type_id,"
                " name, scope, credential_id, enabled)"
                " VALUES ('slow-b','t','B','user:admin',NULL,1)"
            )
            conn.commit()

        result = {}

        def run_action():
            result["response"] = test_client.post(
                "/api/connector-bindings/slow-b/run",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"action": "GET /x", "params": {}},
            )

        worker = threading.Thread(target=run_action)
        worker.start()
        assert started.wait(timeout=5), "the connector action never started"

        # While it is parked in its thread, an unrelated request must still be
        # answered promptly.
        began = time.monotonic()
        health = test_client.get("/health")
        elapsed = time.monotonic() - began

        release.set()
        worker.join(timeout=10)

        assert health.status_code == 200
        assert elapsed < 2, (
            f"an unrelated request waited {elapsed:.1f}s behind a busy connector"
        )
    finally:
        release.set()
        connector_service.execute_binding_action_with_logging = original


def test_catalog_endpoints_do_not_scan_adapter_manifests(
    test_client, admin_token, agent_token, monkeypatch
):
    """A service-catalog read must not hide filesystem validation in async routes."""
    from app.services import adapter_loader

    def fail_if_scanned():
        raise AssertionError("catalog endpoint scanned adapter manifests")

    monkeypatch.setattr(adapter_loader, "list_available_adapters", fail_if_scanned)

    dashboard = test_client.post(
        "/api/dashboard/search",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"query": "generic", "limit": 5},
    )
    mcp = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "connectors_list", "params": {"limit": 20}},
    )

    assert dashboard.status_code == 200, dashboard.text
    assert mcp.status_code == 200, mcp.text
