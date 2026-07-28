"""A merge restore has to bring back the data, and say what it did.

The previous version keyed every table on an `id` column, so tables with a
composite or named key were skipped and the skip was recorded as "0 rows
inserted" — the same value a table with nothing new reports. Any table that
raised was swallowed the same way, and the counts never reached the operator.
"""

import io
import sqlite3

from app.database import get_db
from app.services import backup_service


def _seed():
    """Two installations' worth of state, written straight to the tables."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (id, email, password_hash, display_name, role)"
            " VALUES ('carol','carol@test.local','x','Carol','user')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO workspaces (id, name, description, owner_user_id)"
            " VALUES ('shared','Shared','x','carol')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO workspace_collaborators"
            " (workspace_id, user_id, role, can_read, can_write)"
            " VALUES ('shared','carol','editor',1,1)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO system_settings (key, value)"
            " VALUES ('review_model_name','qwen3:8b')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO connector_types (id, display_name, auth_type,"
            " supported_actions_json, required_credential_fields_json, backend_type,"
            " is_active) VALUES ('searxng','SearXNG','none','[]','[]','http',1)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO adapter_installations"
            " (adapter_id, source_kind, source_path, installed_connector_type_id,"
            " installed_version) VALUES ('searxng','system','builtin/searxng','searxng','1.0')"
        )
        conn.commit()


def _backup_bytes(db_path):
    from cryptography.fernet import Fernet

    if not _key_path().exists():
        _key_path().write_bytes(Fernet.generate_key())
    return backup_service.build_backup_zip(str(db_path), str(_key_path()), "admin")


def _key_path():
    from app.config import settings

    return settings.credential_key_path


def _wipe(table, where="1=1"):
    with get_db() as conn:
        conn.execute(f"DELETE FROM {table} WHERE {where}")
        conn.commit()


def _count(table, where="1=1"):
    with get_db() as conn:
        return conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}").fetchone()["n"]


def test_the_declared_table_set_is_what_gets_merged(clean_db):
    """The set is declared in one place so it can be reviewed as a decision."""
    assert "workspace_collaborators" in backup_service.MERGED_TABLES
    assert "system_settings" in backup_service.MERGED_TABLES
    assert "memory_proposals" in backup_service.MERGED_TABLES
    # Logs, caches and this machine's own identity stay out.
    for excluded in ("audit_log", "sessions", "otp_secrets", "broker_credentials"):
        assert excluded not in backup_service.MERGED_TABLES


def test_tables_without_an_id_column_are_restored(clean_db):
    """Collaborator grants and settings key on something other than `id`."""
    _seed()
    archive = _backup_bytes(clean_db)

    _wipe("workspace_collaborators")
    _wipe("system_settings", "key = 'review_model_name'")
    _wipe("adapter_installations")
    assert _count("workspace_collaborators") == 0

    ok, msg, manifest = backup_service.merge_restore_from_zip(
        io.BytesIO(archive.getvalue()), str(clean_db), str(_key_path())
    )

    assert ok, msg
    assert _count("workspace_collaborators") == 1, "the grant came back"
    assert _count("system_settings", "key = 'review_model_name'") == 1
    assert _count("adapter_installations") == 1


def test_the_counts_reach_the_caller(clean_db):
    _seed()
    archive = _backup_bytes(clean_db)
    _wipe("workspace_collaborators")

    ok, _, manifest = backup_service.merge_restore_from_zip(
        io.BytesIO(archive.getvalue()), str(clean_db), str(_key_path())
    )

    assert ok
    merge = manifest["merge"]
    assert merge["inserted_counts"]["workspace_collaborators"] == 1
    assert merge["failed_tables"] == {}
    assert set(merge["tables_merged"]) == set(backup_service.MERGED_TABLES)


def test_a_backup_that_predates_a_table_brings_nothing_rather_than_failing(clean_db):
    """An older archive simply has less in it; that is not a failure.

    Being strict about tables that cannot be merged has to stop short of
    rejecting every backup taken before the newest table was added — which is
    every backup, the moment a migration lands.
    """
    with get_db() as current:
        backup = sqlite3.connect(":memory:")
        backup.row_factory = sqlite3.Row
        # An archive from a version where memory_proposals did not exist yet.
        backup.execute("CREATE TABLE unrelated (id TEXT PRIMARY KEY)")

        inserted, skipped = backup_service._insert_missing_rows(
            current, backup, "memory_proposals"
        )

    assert (inserted, skipped) == (0, 0)


def test_a_table_that_fails_is_reported_not_swallowed(clean_db, monkeypatch):
    """A partial restore must not answer "complete"."""
    _seed()
    archive = _backup_bytes(clean_db)

    real = backup_service._insert_missing_rows

    def explode(current_con, backup_con, table, transform=None, conflicts=None):
        if table == "memory_records":
            raise sqlite3.OperationalError("disk went away")
        return real(current_con, backup_con, table, transform=transform, conflicts=conflicts)

    monkeypatch.setattr(backup_service, "_insert_missing_rows", explode)

    ok, msg, manifest = backup_service.merge_restore_from_zip(
        io.BytesIO(archive.getvalue()), str(clean_db), str(_key_path())
    )

    assert ok is False, "a table that did not merge is not a success"
    assert "memory_records" in msg
    assert list(manifest["merge"]["failed_tables"]) == ["memory_records"], (
        "only the table that actually failed should be reported"
    )
    assert manifest["merge"]["inserted_counts"], "the tables that did merge are still reported"


# --- related records must not be re-pointed --------------------------------


def _credential(credential_id, value):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO credentials (id, scope, name, reference_name,"
            " value_encrypted) VALUES (?,?,?,?,?)",
            (credential_id, "workspace:proj", "tok", f"AC_SECRET_TOK_{credential_id}", value),
        )
        conn.commit()


def test_a_binding_is_not_attached_to_a_stranger_s_credential(clean_db):
    """The same id can mean two different secrets on two installations.

    Tables merge independently and current wins, so a credential whose id
    already exists here is skipped. A binding that referenced it would keep its
    own id, find the id taken, and quietly authenticate with this
    installation's unrelated secret.
    """
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO connector_types (id, display_name, auth_type,"
            " supported_actions_json, required_credential_fields_json, backend_type,"
            " is_active) VALUES ('t','T','none','[]','[]','generic_http',1)"
        )
        conn.commit()
    _credential("same-id", "THEIR-SECRET")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO connector_bindings (id, connector_type_id, name, scope,"
            " credential_id, enabled) VALUES ('their-binding','t','Theirs',"
            " 'workspace:proj','same-id',1)"
        )
        conn.commit()
    archive = _backup_bytes(clean_db)

    # This installation has its own secret under the same id, and never saw
    # their binding.
    _wipe("connector_bindings")
    _credential("same-id", "OUR-SECRET")

    ok, msg, manifest = backup_service.merge_restore_from_zip(
        io.BytesIO(archive.getvalue()), str(clean_db), str(_key_path())
    )

    assert ok, msg
    with get_db() as conn:
        binding = conn.execute(
            "SELECT credential_id FROM connector_bindings WHERE id='their-binding'"
        ).fetchone()
        secret = conn.execute(
            "SELECT value_encrypted FROM credentials WHERE id='same-id'"
        ).fetchone()["value_encrypted"]

    assert secret == "OUR-SECRET", "current wins for the credential itself"
    assert binding is None, (
        "the binding was imported and silently bound to this installation's secret"
    )
    assert manifest["merge"]["skipped_conflicts"].get("connector_bindings") == 1
    assert "conflict_note" in manifest["merge"]


def test_a_matching_record_is_not_treated_as_a_conflict(clean_db):
    """Same id and same content is the ordinary case, not a collision.

    Two installations that share history hold identical rows; refusing those
    would make merging useless.
    """
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO connector_types (id, display_name, auth_type,"
            " supported_actions_json, required_credential_fields_json, backend_type,"
            " is_active) VALUES ('t','T','none','[]','[]','generic_http',1)"
        )
        conn.commit()
    _credential("shared-id", "SAME-SECRET")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO connector_bindings (id, connector_type_id, name, scope,"
            " credential_id, enabled) VALUES ('their-binding','t','Theirs',"
            " 'workspace:proj','shared-id',1)"
        )
        conn.commit()
    archive = _backup_bytes(clean_db)

    _wipe("connector_bindings")  # the credential stays, unchanged

    ok, msg, manifest = backup_service.merge_restore_from_zip(
        io.BytesIO(archive.getvalue()), str(clean_db), str(_key_path())
    )

    assert ok, msg
    with get_db() as conn:
        binding = conn.execute(
            "SELECT credential_id FROM connector_bindings WHERE id='their-binding'"
        ).fetchone()
    assert binding is not None, "an unambiguous binding should still come across"
    assert binding["credential_id"] == "shared-id"
    assert manifest["merge"]["skipped_conflicts"] == {}


def _workspace(workspace_id, description, owner="carol"):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (id, email, password_hash, display_name, role)"
            " VALUES (?,?,?,?,'user')",
            (owner, f"{owner}@test.local", "x", owner.title()),
        )
        conn.execute(
            "INSERT OR REPLACE INTO workspaces (id, name, description, owner_user_id)"
            " VALUES (?,?,?,?)",
            (workspace_id, workspace_id.title(), description, owner),
        )
        conn.commit()


def _memory(record_id, scope, content):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO memory_records (id, scope, memory_class, content,"
            " created_at, status_changed_at) VALUES (?,?,'fact',?,?,?)",
            (record_id, scope, content, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()


def test_a_memory_is_not_filed_into_a_workspace_of_the_same_name(clean_db):
    """`workspace:proj` is a relationship even though it is only a string.

    Two installations can both have a workspace called proj that have nothing
    to do with each other. Importing their memory into ours files a stranger's
    notes under our project.
    """
    _workspace("proj", "their project")
    _memory("mem-1", "workspace:proj", "Their internal deployment note.")
    archive = _backup_bytes(clean_db)

    _wipe("memory_records")
    _workspace("proj", "our completely different project")

    ok, msg, manifest = backup_service.merge_restore_from_zip(
        io.BytesIO(archive.getvalue()), str(clean_db), str(_key_path())
    )

    assert ok, msg
    assert _count("memory_records", "id = 'mem-1'") == 0, (
        "their memory was filed into our unrelated workspace"
    )
    assert manifest["merge"]["skipped_conflicts"].get("memory_records") == 1


def test_a_proposal_does_not_end_up_pointing_at_our_memory(clean_db):
    """The target ids live inside JSON, which is still a relationship."""
    _workspace("proj", "same on both sides")
    _memory("mem-1", "workspace:proj", "Their record.")
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO memory_proposals (id, rule, action, scope,"
            " target_ids_json, rationale, status, created_at) VALUES"
            " ('prop-1','duplicate_cluster','retract','workspace:proj',"
            " '[\"mem-1\"]','theirs','pending','2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
    archive = _backup_bytes(clean_db)

    _wipe("memory_proposals")
    _memory("mem-1", "workspace:proj", "Ours, entirely different content.")

    ok, msg, manifest = backup_service.merge_restore_from_zip(
        io.BytesIO(archive.getvalue()), str(clean_db), str(_key_path())
    )

    assert ok, msg
    assert _count("memory_proposals", "id = 'prop-1'") == 0, (
        "a proposal to retract our record was imported from their database"
    )
    assert manifest["merge"]["skipped_conflicts"].get("memory_proposals") == 1


def _agent(agent_id, read_scopes, owner="carol"):
    import json as _json

    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (id, email, password_hash, display_name, role)"
            " VALUES (?,?,?,?,'user')",
            (owner, f"{owner}@test.local", "x", owner.title()),
        )
        conn.execute(
            "INSERT OR REPLACE INTO agents (id, display_name, api_key_hash,"
            " owner_user_id, read_scopes_json, write_scopes_json,"
            " default_recall_scopes_json) VALUES (?,?,?,?,?,'[]','[]')",
            (agent_id, agent_id.title(), "hash", owner, _json.dumps(read_scopes)),
        )
        conn.commit()


def test_an_imported_agent_is_not_granted_our_workspace(clean_db):
    """Scope arrays on an agent are its permissions, not just metadata.

    An agent restored from someone else's backup carrying
    ["workspace:proj"] would be granted whatever `proj` means here. That is a
    stranger's agent holding a key to your workspace.
    """
    _workspace("proj", "their project")
    _agent("theiragent", ["workspace:proj"])
    archive = _backup_bytes(clean_db)

    _wipe("agents", "id = 'theiragent'")
    _workspace("proj", "our unrelated project")

    ok, msg, manifest = backup_service.merge_restore_from_zip(
        io.BytesIO(archive.getvalue()), str(clean_db), str(_key_path())
    )

    assert ok, msg
    assert _count("agents", "id = 'theiragent'") == 0, (
        "their agent was imported holding read access to our workspace"
    )
    assert manifest["merge"]["skipped_conflicts"].get("agents") == 1


def test_an_agent_whose_scopes_are_unambiguous_still_merges(clean_db):
    """Only conflicting scopes block the import, not every scope."""
    _workspace("proj", "identical on both sides")
    _agent("theiragent", ["workspace:proj"])
    archive = _backup_bytes(clean_db)

    _wipe("agents", "id = 'theiragent'")  # the workspace stays, unchanged

    ok, msg, manifest = backup_service.merge_restore_from_zip(
        io.BytesIO(archive.getvalue()), str(clean_db), str(_key_path())
    )

    assert ok, msg
    assert _count("agents", "id = 'theiragent'") == 1
    assert manifest["merge"]["skipped_conflicts"] == {}


def test_assigned_work_does_not_follow_a_conflicting_agent(clean_db):
    """assigned_agent_id decides who may claim the work, so it is authorization.

    The two agents are deliberately different: the activity was *created* by an
    agent that merges cleanly, and only the agent it is *assigned to* conflicts.
    Using one agent for both would let agent_id block the row on its own, and
    the test would pass with the assignment mapping removed.
    """
    _workspace("proj", "same on both sides")
    _agent("author", ["workspace:proj"])  # identical here and there
    _agent("claimant", ["workspace:proj"])
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO agent_activity (id, agent_id, user_id,"
            " task_description, status, assigned_agent_id, started_at, updated_at)"
            " VALUES ('act-1','author','carol','Their task','active','claimant',"
            " '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
    archive = _backup_bytes(clean_db)

    _wipe("agent_activity")
    # Only the claimant differs here — same id, a different agent entirely.
    _agent("claimant", ["workspace:somewhere-else"])

    ok, msg, manifest = backup_service.merge_restore_from_zip(
        io.BytesIO(archive.getvalue()), str(clean_db), str(_key_path())
    )

    assert ok, msg
    assert _count("agents", "id = 'author'") == 1, (
        "the creating agent is unambiguous and must still merge"
    )
    assert _count("agent_activity", "id = 'act-1'") == 0, (
        "work assigned to their agent was handed to ours"
    )


def test_skipping_propagates_to_what_depended_on_the_skipped_row(clean_db):
    """A dependant of a skipped row cannot be imported either.

    Without the closure the execution is inserted, its binding is absent, and
    the foreign key fails the whole merge instead of declining one row.
    """
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO connector_types (id, display_name, auth_type,"
            " supported_actions_json, required_credential_fields_json, backend_type,"
            " is_active) VALUES ('t','T','none','[]','[]','generic_http',1)"
        )
        conn.commit()
    _credential("same-id", "THEIR-SECRET")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO connector_bindings (id, connector_type_id, name, scope,"
            " credential_id, enabled) VALUES ('their-binding','t','Theirs',"
            " 'workspace:proj','same-id',1)"
        )
        conn.execute(
            "INSERT INTO connector_executions (id, binding_id, action, result_status)"
            " VALUES ('exec-1','their-binding','GET /x','success')"
        )
        conn.commit()
    archive = _backup_bytes(clean_db)

    _wipe("connector_executions")
    _wipe("connector_bindings")
    _credential("same-id", "OUR-SECRET")

    ok, msg, manifest = backup_service.merge_restore_from_zip(
        io.BytesIO(archive.getvalue()), str(clean_db), str(_key_path())
    )

    assert ok, msg
    assert _count("connector_bindings", "id = 'their-binding'") == 0
    assert _count("connector_executions", "id = 'exec-1'") == 0, (
        "an execution survived its binding"
    )
    assert manifest["merge"]["failed_tables"] == {}, "this must decline rows, not fail"


def test_the_endpoint_surfaces_the_merge_detail(test_client, admin_token, clean_db):
    from app.config import settings

    _seed()
    _backup_bytes(clean_db)  # ensures the key file exists
    encrypted, backup_key = backup_service.build_encrypted_backup_package(
        str(clean_db), str(settings.credential_key_path), "admin"
    )
    _wipe("workspace_collaborators")

    r = test_client.post(
        "/api/backup/restore",
        headers={"Authorization": f"Bearer {admin_token}"},
        files={"backup": ("backup.zip.enc", io.BytesIO(encrypted.getvalue()))},
        data={"backup_key": backup_key.decode(), "mode": "merge"},
    )
    assert r.status_code == 200, r.text[:300]
    merge = r.json()["data"]["merge"]
    assert "inserted_counts" in merge, "the operator could not see what came across"
    assert merge["failed_tables"] == {}
