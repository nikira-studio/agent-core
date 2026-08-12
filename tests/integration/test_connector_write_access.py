"""Read access to a binding is permission to look, not to act.

Both run paths checked `can_read` and nothing else, so an agent granted
read-only access to a scope could invoke every action the binding exposed,
including deletes. Read and write are separate grants everywhere else in the
scope model; this brings connector execution in line with that.
"""

import pytest

from app.services import connector_service

TYPE = {
    "id": "t",
    "supported_actions": [
        {"name": "GET /items", "method": "GET"},
        {"name": "POST /items", "method": "POST"},
        {"name": "DELETE /items/{id}", "method": "DELETE"},
        {"name": "search", "side_effect": "none"},
        {"name": "purge_everything"},
    ],
}


@pytest.mark.parametrize(
    "action,needs_write",
    [
        ("GET /items", False),
        ("POST /items", True),
        ("DELETE /items/{id}", True),
        ("search", True),
        ("purge_everything", True),
        ("unlisted action", True),
    ],
)
def test_which_actions_need_write(action, needs_write):
    assert connector_service.action_requires_write(TYPE, action) is needs_write


@pytest.mark.parametrize("declared", ["write", "destructive", "delete", "mutating"])
def test_declared_write_metadata_beats_a_harmless_method(declared):
    """A manifest that says an action is destructive is believed.

    Inferring from the method alone read `{"method": "GET", "side_effect":
    "destructive"}` as safe. No bundled manifest does that today, but imported
    ones are written by other people.
    """
    connector_type = {
        "supported_actions": [
            {"name": "GET /wipe", "method": "GET", "side_effect": declared}
        ]
    }
    assert connector_service.action_requires_write(connector_type, "GET /wipe") is True


def test_an_unidentifiable_action_needs_write():
    """The safe default is the stronger grant.

    An inconclusive verification check leaves a record alone, because guessing
    wrong there deletes something true. Here guessing wrong runs a stranger's
    DELETE, so the unknown case resolves the other way.
    """
    assert connector_service.action_requires_write({}, "do_something") is True
    assert connector_service.action_requires_write({}, "GET /safe") is False


def test_remote_read_metadata_cannot_weaken_authorization_but_operator_override_can():
    connector_type = {
        "supported_actions": [{"name": "search", "side_effect": "read"}],
        "capability_policy_overrides_json": '{"search":{"authorization_class":"read"}}',
    }
    assert connector_service.action_requires_write(
        {"supported_actions": [{"name": "search", "side_effect": "read"}]}, "search"
    ) is True
    assert connector_service.action_requires_write(connector_type, "search") is False


def test_the_method_is_read_from_the_action_name_when_metadata_is_thin():
    """Imported specs name actions "GET /path"; that is enough to tell."""
    assert connector_service.action_requires_write({"supported_actions": []}, "GET /x") is False
    assert connector_service.action_requires_write({"supported_actions": []}, "PATCH /x") is True


# --- over the wire ---------------------------------------------------------


def _binding(scope="workspace:proj"):
    from app.database import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO connector_types (id, display_name, auth_type,"
            " supported_actions_json, required_credential_fields_json, backend_type,"
            " is_active) VALUES ('t','T','none',?,'[]','generic_http',1)",
            (
                '[{"name": "GET /items", "method": "GET"},'
                ' {"name": "DELETE /items", "method": "DELETE"}]',
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO connector_bindings"
            " (id, connector_type_id, name, scope, credential_id, enabled)"
            " VALUES ('b1','t','B',?,NULL,1)",
            (scope,),
        )
        conn.commit()
    return "b1"


def _run(client, token, action):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "tool": "connectors_run",
            "params": {"binding_id": "b1", "action": action, "params": {}},
        },
    )


def _read_only_agent(client, admin_token):
    client.post(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "proj", "name": "Proj", "description": "x"},
    )
    r = client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "id": "readeragent",
            "display_name": "Reader",
            "read_scopes": ["workspace:proj"],
            "write_scopes": [],
        },
    )
    assert r.status_code in (200, 201), r.json()
    key = client.post(
        "/api/integrations/generate-connection",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"user_id": "admin", "agent_id": "readeragent", "output_type": "env"},
    )
    assert key.status_code == 200, key.json()
    return key.json()["data"]["api_key"]


def test_a_read_only_agent_is_refused_a_destructive_action(test_client, admin_token):
    _binding()
    token = _read_only_agent(test_client, admin_token)

    r = _run(test_client, token, "DELETE /items")
    assert r.json()["error"]["code"] == "SCOPE_DENIED", r.json()
    assert "write access" in r.json()["error"]["message"]


def test_a_read_only_agent_may_still_read(test_client, admin_token):
    """The fix must not turn read-only bindings into useless ones."""
    _binding()
    token = _read_only_agent(test_client, admin_token)

    r = _run(test_client, token, "GET /items")
    error = r.json().get("error") or {}
    assert error.get("code") != "SCOPE_DENIED", r.json()


def test_the_rest_route_refuses_it_the_same_way(test_client, admin_token):
    """Two transports, one rule — or the rule is only a suggestion."""
    _binding()
    token = _read_only_agent(test_client, admin_token)

    r = test_client.post(
        "/api/connector-bindings/b1/run",
        headers={"Authorization": f"Bearer {token}"},
        json={"action": "DELETE /items", "params": {}},
    )
    assert r.status_code == 403, r.text[:200]
    assert r.json()["error"]["code"] == "SCOPE_DENIED"
