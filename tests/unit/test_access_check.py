"""The guarded schema revision: kinds are decided by the rule, access is compared before and after, and a revision
that changes access without declaring it is refused and leaves the database as it was."""

import json
import sqlite3

import pytest

from app.access_check import compare, snapshot
from app.access_check.guard import MigrationRefused, RevisionCheck, run_guarded
from app.schema import create_schema

SERVICE = json.dumps(["credentials_resolve"])
DEFAULT = json.dumps(["memory", "coordination", "credentials", "connectors_read", "connectors_execute"])


def _agent(conn, agent_id, caps, read=None, write=None, person="alex"):
    own = json.dumps([f"agent:{agent_id}"])
    conn.execute(
        "INSERT INTO agents (id, display_name, owner_user_id, default_user_id, read_scopes_json, write_scopes_json, "
        "capabilities_json, api_key_hash, inherit_user_bindings) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (agent_id, agent_id, person, person, read or own, write or own, caps, f"hash-{agent_id}"),
    )
    conn.execute(
        "INSERT INTO agent_keys (id, agent_id, label, key_hash, created_at) VALUES (?, ?, 'Original key', ?, '2026-01-01')",
        (f"key-{agent_id}", agent_id, f"hash-{agent_id}"),
    )


def _before_kind_db() -> sqlite3.Connection:
    """A database as it was before revision 30: current schema, the kind column removed, revision 30 not recorded."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_schema(conn)
    conn.execute("DROP INDEX idx_agents_kind")
    conn.execute("ALTER TABLE agents DROP COLUMN kind")
    conn.execute("DELETE FROM schema_migrations WHERE revision = 30")
    conn.execute("INSERT INTO users (id, email, password_hash, display_name) VALUES ('alex', 'a@example.com', 'x', 'Alex')")
    _agent(conn, "builder", DEFAULT, read=json.dumps(["agent:builder", "user:alex", "workspace:site"]))
    _agent(conn, "runtime-web", SERVICE)
    _agent(conn, "runtime-spaced", '[ "credentials_resolve" ]')
    _agent(conn, "has-workspace", SERVICE, read=json.dumps(["agent:has-workspace", "workspace:site"]))
    _agent(conn, "has-work", SERVICE)
    conn.execute(
        "INSERT INTO agent_activity (id, agent_id, user_id, task_description, status, memory_scope, started_at, heartbeat_at) "
        "VALUES ('a1', 'has-work', 'alex', 'a task', 'completed', 'workspace:site', '2026-01-01', '2026-01-01')"
    )
    conn.execute("INSERT INTO credentials (id, scope, name, value_encrypted, reference_name) VALUES ('c1', 'workspace:site', 'k', 'ciphertext', 'AC_SECRET_K')")
    conn.execute(
        "INSERT INTO credential_consumer_grants (id, consumer_agent_id, entry_id, created_by, created_at, allow_write) "
        "VALUES ('g1', 'runtime-web', 'c1', 'alex', '2026-01-01', 1)"
    )
    conn.commit()
    return conn


def test_kind_rule_marks_only_clean_fetch_only_entries_as_services():
    from app.schema import _apply_schema_migrations

    conn = _before_kind_db()
    _apply_schema_migrations(conn)
    kinds = {r["id"]: r["kind"] for r in conn.execute("SELECT id, kind FROM agents")}
    assert kinds == {
        "builder": "agent",
        "runtime-web": "service",
        "runtime-spaced": "service",
        "has-workspace": "agent",  # exactly the service permission, but in a workspace
        "has-work": "agent",  # exactly the service permission, but has recorded work
    }
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE revision = 30").fetchone()[0] == 1


def test_the_step_changes_no_access_and_keeps_keys_credentials_and_grants():
    from app.schema import _apply_schema_migrations

    conn = _before_kind_db()
    before = snapshot.capture(conn)
    _apply_schema_migrations(conn)
    after = snapshot.capture(conn)
    assert compare.differences(before, after) == []
    assert snapshot.counts(after) == {
        "identities": 5, "agents": 3, "services": 2, "bindings": 0, "credentials": 1, "grants": 1,
    }
    assert after["fetch"] == {"runtime-web": {"c1": {"write": True, "expires_at": None}}}
    assert after["identities"]["runtime-web"]["live_keys"] == 1


def test_a_step_that_gains_access_is_refused_and_rolled_back():
    conn = _before_kind_db()
    before_rows = conn.execute("SELECT COUNT(*) FROM credential_consumer_grants").fetchone()[0]

    def sneaky(c):
        c.execute("ALTER TABLE agents ADD COLUMN scratch TEXT")
        c.execute(
            "INSERT INTO credential_consumer_grants (id, consumer_agent_id, entry_id, created_by, created_at, allow_write) "
            "VALUES ('g2', 'runtime-spaced', 'c1', 'alex', '2026-01-01', 0)"
        )

    with pytest.raises(MigrationRefused) as refused:
        run_guarded(conn, 99, "sneaky", sneaky, RevisionCheck())
    assert "fetch.runtime-spaced.c1" in str(refused.value) or "difference" in str(refused.value)
    assert conn.execute("SELECT COUNT(*) FROM credential_consumer_grants").fetchone()[0] == before_rows
    assert "scratch" not in {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE revision = 99").fetchone()[0] == 0
    assert not conn.in_transaction


def test_a_step_that_loses_access_is_refused_too():
    conn = _before_kind_db()

    def drops(c):
        c.execute("UPDATE credential_consumer_grants SET revoked_at = '2026-02-01' WHERE id = 'g1'")

    with pytest.raises(MigrationRefused):
        run_guarded(conn, 99, "drops", drops, RevisionCheck())
    assert conn.execute("SELECT revoked_at FROM credential_consumer_grants WHERE id = 'g1'").fetchone()[0] is None


def test_a_declared_change_is_allowed_and_an_undeclared_one_beside_it_is_not():
    conn = _before_kind_db()

    def two_changes(c):
        c.execute("UPDATE credential_consumer_grants SET allow_write = 0 WHERE id = 'g1'")
        c.execute("UPDATE agents SET capabilities_json = ? WHERE id = 'builder'", (json.dumps(["memory"]),))

    with pytest.raises(MigrationRefused):
        run_guarded(conn, 99, "mixed", two_changes, RevisionCheck(allowed=("fetch.*.*.write",)))
    # With both declared it goes through.
    run_guarded(
        conn, 99, "mixed", two_changes,
        RevisionCheck(allowed=("fetch.*.*.write", "identities.builder.permissions", "mcp_reference.builder", "helper_fetch.builder", "see.builder", "run.builder")),
    )
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE revision = 99").fetchone()[0] == 1


def test_a_failing_step_leaves_the_database_untouched():
    conn = _before_kind_db()

    def boom(c):
        c.execute("ALTER TABLE agents ADD COLUMN scratch TEXT")
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        run_guarded(conn, 99, "boom", boom, RevisionCheck())
    assert "scratch" not in {r[1] for r in conn.execute("PRAGMA table_info(agents)")}


def test_a_wrong_stored_kind_is_caught_by_the_rule_check():
    from app.access_check.revisions import CHECKS

    conn = _before_kind_db()

    def wrong(c):
        c.execute("ALTER TABLE agents ADD COLUMN kind TEXT NOT NULL DEFAULT 'agent'")  # every service left as agent

    with pytest.raises(MigrationRefused):
        run_guarded(conn, 30, "agent-or-service-kind", wrong, CHECKS[30])
    assert "kind" not in {r[1] for r in conn.execute("PRAGMA table_info(agents)")}


def test_the_comparison_reports_both_directions_and_honours_patterns():
    before = {"run": {"a": ["x"], "b": []}, "identities": {"a": {"kind": "agent"}}}
    after = {"run": {"a": [], "b": ["x"]}, "identities": {"a": {"kind": "service"}}}
    paths = {d["path"] for d in compare.differences(before, after)}
    assert paths == {"run.a", "run.b", "identities.a.kind"}
    assert {d["path"] for d in compare.differences(before, after, ["identities.*.kind"])} == {"run.a", "run.b"}


def test_a_database_file_gets_a_copy_before_the_step(tmp_path):
    from app.access_check import guard

    path = tmp_path / "core.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    create_schema(conn)
    conn.execute("INSERT INTO users (id, email, password_hash, display_name) VALUES ('alex', 'a@example.com', 'x', 'Alex')")
    _agent(conn, "builder", DEFAULT)
    conn.commit()
    run_guarded(conn, 99, "noop", lambda c: None, RevisionCheck())
    copies = list(tmp_path.glob("pre-migration-99-*.db"))
    assert len(copies) == 1 and oct(copies[0].stat().st_mode)[-3:] == "600"
    assert guard._database_file(conn) == path


def test_a_step_that_adds_a_workspace_to_an_inactive_entry_is_refused():
    """Dormant access counts: the matrices skip inactive entries, so their stored scopes are compared directly."""
    conn = _before_kind_db()
    conn.execute("UPDATE agents SET is_active = 0 WHERE id = 'builder'")
    conn.commit()

    def widens(c):
        c.execute(
            "UPDATE agents SET read_scopes_json = ? WHERE id = 'builder'",
            (json.dumps(["agent:builder", "user:alex", "workspace:site", "workspace:vault"]),),
        )

    with pytest.raises(MigrationRefused) as refused:
        run_guarded(conn, 99, "widens", widens, RevisionCheck())
    assert "identities.builder.read_scopes" in str(refused.value) or "difference" in str(refused.value)
    stored = json.loads(conn.execute("SELECT read_scopes_json FROM agents WHERE id = 'builder'").fetchone()[0])
    assert "workspace:vault" not in stored


def test_a_step_that_changes_write_or_recall_scopes_of_any_entry_is_refused():
    conn = _before_kind_db()
    with pytest.raises(MigrationRefused):
        run_guarded(
            conn, 99, "w", lambda c: c.execute("UPDATE agents SET write_scopes_json = '[\"agent:builder\", \"shared\"]' WHERE id = 'builder'"),
            RevisionCheck(),
        )
    with pytest.raises(MigrationRefused):
        run_guarded(
            conn, 99, "r", lambda c: c.execute("UPDATE agents SET default_recall_scopes_json = '[\"agent:builder\"]' WHERE id = 'builder'"),
            RevisionCheck(),
        )


@pytest.mark.parametrize(
    "stored",
    ['[["credentials_resolve"]]', '[{"a": 1}]', '"credentials_resolve"', "null", "5", "{", '["credentials_resolve", "credentials_resolve"]', '[1]'],
)
def test_a_malformed_permissions_row_is_an_agent_and_does_not_stop_the_upgrade(stored):
    from app.schema import _apply_schema_migrations
    from app.services import agent_kind

    assert agent_kind.has_service_permissions(stored) is False
    conn = _before_kind_db()
    conn.execute("UPDATE agents SET capabilities_json = ? WHERE id = 'runtime-web'", (stored,))
    conn.commit()
    _apply_schema_migrations(conn)
    assert conn.execute("SELECT kind FROM agents WHERE id = 'runtime-web'").fetchone()[0] == "agent"
    assert conn.execute("SELECT kind FROM agents WHERE id = 'runtime-spaced'").fetchone()[0] == "service"


def test_the_copy_and_the_report_are_owner_only_from_the_moment_they_exist(tmp_path, monkeypatch):
    """With a wide umask and the after-the-fact chmod disabled, the files still end up 0600: they were created that way."""
    import os

    from app.access_check import guard

    path = tmp_path / "core.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    create_schema(conn)
    conn.execute("INSERT INTO users (id, email, password_hash, display_name) VALUES ('alex', 'a@example.com', 'x', 'Alex')")
    _agent(conn, "builder", DEFAULT)
    conn.commit()
    monkeypatch.setattr(guard.os, "chmod", lambda *a, **k: None)
    old = os.umask(0)
    try:
        run_guarded(conn, 98, "noop", lambda c: None, RevisionCheck())
        with pytest.raises(MigrationRefused):
            run_guarded(conn, 99, "bad", lambda c: c.execute("UPDATE agents SET read_scopes_json = '[]'"), RevisionCheck())
    finally:
        os.umask(old)
    copy = next(tmp_path.glob("pre-migration-98-*.db"))
    report = next((tmp_path / "migration-reports").glob("revision-99-*.json"))
    assert oct(copy.stat().st_mode)[-3:] == "600"
    assert oct(report.stat().st_mode)[-3:] == "600"
    assert oct((tmp_path / "migration-reports").stat().st_mode)[-3:] in ("700", "755", "775")  # folder made with 0700 (umask 0 here)
