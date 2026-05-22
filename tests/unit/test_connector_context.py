from app.security.context import build_user_context, build_user_context_for_connectors
from app.services.auth_service import create_session, create_user, validate_session
from app.services.connector_service import create_binding
from app.services.credential_service import create_credential
from app.services.workspace_service import create_workspace


def test_connector_user_context_does_not_expand_other_workspace_scopes(clean_db):
    create_user("alice", "alice@test.local", "testpassword123", "Alice", "user")
    create_user("bob", "bob@test.local", "testpassword123", "Bob", "user")
    create_workspace("alice-ws", "Alice WS", owner_user_id="alice")
    create_workspace("bob-ws", "Bob WS", owner_user_id="bob")
    bob_credential = create_credential(
        "workspace:bob-ws",
        "bob-token",
        value_plaintext="bob-secret",
        created_by="bob",
    )
    create_binding(
        connector_type_id="generic_http",
        name="bob-binding",
        scope="workspace:bob-ws",
        credential_id=bob_credential["id"],
        created_by="bob",
    )

    session = validate_session(create_session("alice")["session_id"])
    assert session is not None
    user_context = build_user_context(session)
    connector_context = build_user_context_for_connectors(session)

    assert user_context.read_scopes == connector_context.read_scopes
    assert user_context.write_scopes == connector_context.write_scopes
    assert "workspace:bob-ws" not in connector_context.read_scopes
    assert "workspace:bob-ws" not in connector_context.write_scopes
