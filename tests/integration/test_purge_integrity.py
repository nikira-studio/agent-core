"""Deleting an owner must not leave the database half-cleaned.

Foreign keys are enforced, and several tables point at users, credentials and
workspaces. A purge that deletes rows in the wrong order raises IntegrityError
partway through, which surfaced as a 500 and left whatever had already been
deleted deleted.
"""

from app.database import get_db
from app.services import credential_service
from app.services.auth_service import create_user, delete_user


def _credential(scope):
    entry = credential_service.create_credential(
        scope=scope, name="tok", value_plaintext="secret-value", label="Tok"
    )
    return entry["id"] if isinstance(entry, dict) else entry[0]["id"]


def _binding(binding_id, scope, credential_id):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO connector_types (id, display_name, auth_type,"
            " supported_actions_json, required_credential_fields_json, backend_type,"
            " is_active) VALUES ('t','T','none','[]','[]','generic_http',1)"
        )
        conn.execute(
            "INSERT INTO connector_bindings (id, connector_type_id, name, scope,"
            " credential_id, enabled) VALUES (?,'t','B',?,?,1)",
            (binding_id, scope, credential_id),
        )
        conn.commit()


def _rows(table, where, params):
    with get_db() as conn:
        return conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params
        ).fetchone()["n"]


def test_deleting_a_user_whose_credential_backs_a_binding(test_client, admin_token):
    create_user("owner", "owner@test.local", "testpassword123", "Owner", "user")
    credential_id = _credential("user:owner")
    _binding("b-user", "user:owner", credential_id)

    ok, err = delete_user("owner")

    assert ok, f"purge failed: {err}"
    assert _rows("credentials", "id = ?", (credential_id,)) == 0
    assert _rows("connector_bindings", "id = ?", ("b-user",)) == 0, (
        "a binding pointing at a deleted credential is a connector that cannot work"
    )


def test_a_binding_in_the_scope_goes_even_with_another_credential(test_client, admin_token):
    """The binding lives in the purged scope; its credential's home is separate."""
    create_user("owner", "owner@test.local", "testpassword123", "Owner", "user")
    create_user("other", "other@test.local", "testpassword123", "Other", "user")
    outside_credential = _credential("user:other")
    _binding("b-scope", "user:owner", outside_credential)

    ok, err = delete_user("owner")

    assert ok, f"purge failed: {err}"
    assert _rows("connector_bindings", "id = ?", ("b-scope",)) == 0
    assert _rows("credentials", "id = ?", (outside_credential,)) == 1, (
        "another user's credential must survive"
    )


def test_deleting_a_user_who_collaborates_on_someone_elses_workspace(
    test_client, admin_token
):
    create_user("owner", "owner@test.local", "testpassword123", "Owner", "user")
    create_user("collab", "collab@test.local", "testpassword123", "Collab", "user")
    from app.services import workspace_service

    workspace_service.create_workspace("shared", "Shared", "owner", "x")
    workspace_service.upsert_workspace_collaborator(
        "shared", "collab", can_read=True, can_write=False, created_by="owner"
    )

    ok, err = delete_user("collab")

    assert ok, f"purge failed: {err}"
    assert _rows("workspace_collaborators", "user_id = ?", ("collab",)) == 0
    assert _rows("workspaces", "id = ?", ("shared",)) == 1, "the workspace is not theirs"


def test_deleting_a_workspace_owner_takes_the_grants_with_it(test_client, admin_token):
    create_user("owner", "owner@test.local", "testpassword123", "Owner", "user")
    create_user("collab", "collab@test.local", "testpassword123", "Collab", "user")
    from app.services import workspace_service

    workspace_service.create_workspace("owned", "Owned", "owner", "x")
    workspace_service.upsert_workspace_collaborator(
        "owned", "collab", can_read=True, can_write=False, created_by="owner"
    )

    ok, err = delete_user("owner")

    assert ok, f"purge failed: {err}"
    assert _rows("workspaces", "id = ?", ("owned",)) == 0
    assert _rows("workspace_collaborators", "workspace_id = ?", ("owned",)) == 0


def test_purging_a_workspace_with_a_bound_credential(test_client, admin_token):
    from app.services import workspace_service

    create_user("owner", "owner@test.local", "testpassword123", "Owner", "user")
    workspace_service.create_workspace("proj", "Proj", "owner", "x")
    credential_id = _credential("workspace:proj")
    _binding("b-ws", "workspace:proj", credential_id)

    result = workspace_service.delete_workspace_hard("proj")

    assert result is not False, "purge reported failure"
    assert _rows("connector_bindings", "id = ?", ("b-ws",)) == 0
    assert _rows("credentials", "id = ?", (credential_id,)) == 0


def test_purging_an_agent_with_a_bound_credential(test_client, admin_token):
    from app.services import agent_service

    agent_service.create_agent(
        agent_id="doomed", display_name="Doomed", owner_user_id="admin"
    )
    credential_id = _credential("agent:doomed")
    _binding("b-agent", "agent:doomed", credential_id)

    agent_service.delete_agent_hard("doomed")

    assert _rows("connector_bindings", "id = ?", ("b-agent",)) == 0
    assert _rows("credentials", "id = ?", (credential_id,)) == 0
