import json
from pathlib import Path
from unittest.mock import MagicMock

from app.connectors.manifest import load_and_validate
from app.services import connector_oauth_service, connector_service, credential_service


def _install_gmail_connector():
    manifest, err = load_and_validate(
        Path("/srv/docker-data/projects/Apps/agent-core/data/adapters/google_gmail/adapter.json")
    )
    assert err is None
    row = manifest.to_connector_type_row()
    connector_service.create_connector_type(
        connector_type_id=row["id"],
        display_name=row["display_name"],
        description=row["description"],
        provider_type=row["provider_type"],
        auth_type=row["auth_type"],
        supported_actions=manifest.actions,
        required_credential_fields=json.loads(row["required_credential_fields_json"]),
        backend_type=row["backend_type"],
        backend_json=row["backend_json"],
    )


def _create_gmail_binding():
    credential = credential_service.create_credential(
        scope="user:admin",
        name="gmail-oauth",
        value_plaintext=json.dumps(
            {"client_id": "client-id", "client_secret": "client-secret"}
        ),
        created_by="admin",
    )
    return connector_service.create_binding(
        connector_type_id="google_gmail",
        name="Gmail OAuth",
        scope="user:admin",
        credential_id=credential["id"],
        created_by="admin",
    )


def test_oauth_authorization_url_and_callback_store_tokens(clean_db, monkeypatch):
    _install_gmail_connector()
    binding = _create_gmail_binding()
    callback_url = "https://core.example.com/api/connector-bindings/oauth/callback"

    authorization_url = connector_oauth_service.build_authorization_url(
        binding["id"], "admin", callback_url
    )
    assert authorization_url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "access_type=offline" in authorization_url
    assert "prompt=consent" in authorization_url
    assert "gmail.modify" in authorization_url

    state = authorization_url.split("state=", 1)[1].split("&", 1)[0]
    response = MagicMock()
    response.read.return_value = json.dumps(
        {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
        }
    ).encode()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    monkeypatch.setattr(connector_oauth_service, "safe_urlopen", lambda *a, **k: response)

    result = connector_oauth_service.exchange_callback(state, "auth-code", "admin")
    assert result["binding_id"] == binding["id"]

    linked = connector_service.get_binding_with_credential(binding["id"])
    assert linked["credential"].fields["access_token"] == "access-token"
    assert linked["credential"].fields["refresh_token"] == "refresh-token"
    assert linked["credential"].fields["expires_at"]


def test_oauth_start_endpoint_returns_callback_url(test_client, admin_token):
    _install_gmail_connector()
    binding = _create_gmail_binding()

    response = test_client.post(
        f"/api/connector-bindings/{binding['id']}/oauth/start",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["callback_url"].endswith("/api/connector-bindings/oauth/callback")
    assert data["authorization_url"].startswith(
        "https://accounts.google.com/o/oauth2/v2/auth?"
    )


def test_oauth_start_endpoint_uses_forwarded_public_url(test_client, admin_token):
    _install_gmail_connector()
    binding = _create_gmail_binding()

    response = test_client.post(
        f"/api/connector-bindings/{binding['id']}/oauth/start",
        headers={
            "Authorization": f"Bearer {admin_token}",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "core.veditz.com",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["callback_url"] == (
        "https://core.veditz.com/api/connector-bindings/oauth/callback"
    )
