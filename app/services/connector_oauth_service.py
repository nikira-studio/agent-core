"""OAuth authorization-code flow for declarative OAuth2 connector bindings."""

import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from app.database import get_db
from app.security.safe_http import safe_urlopen
from app.services import connector_service, credential_service
from app.time_utils import parse_utc_datetime, utc_now


class ConnectorOAuthError(Exception):
    pass


def _oauth_specs(binding_id: str) -> tuple[dict, dict, dict]:
    binding = connector_service.get_binding_with_credential(binding_id)
    if not binding:
        raise ConnectorOAuthError("Binding not found")
    if not binding.get("credential"):
        raise ConnectorOAuthError("Binding has no linked credential")
    connector_type = connector_service.get_connector_type(binding["connector_type_id"])
    backend = json.loads(connector_type.get("backend_json") or "{}")
    auth = backend.get("auth") or {}
    authorization = auth.get("authorization") or {}
    refresh = backend.get("refresh") or {}
    if auth.get("type") != "oauth2" or not authorization.get("url"):
        raise ConnectorOAuthError("Connector does not declare an OAuth authorization flow")
    if not refresh.get("token_url"):
        raise ConnectorOAuthError("Connector does not declare an OAuth token URL")
    return binding, authorization, refresh


def build_authorization_url(binding_id: str, user_id: str, redirect_uri: str) -> str:
    binding, authorization, _ = _oauth_specs(binding_id)
    client_id = binding["credential"].fields.get("client_id")
    if not client_id:
        raise ConnectorOAuthError("Credential is missing client_id")

    state = secrets.token_urlsafe(32)
    now = utc_now()
    with get_db() as conn:
        conn.execute(
            "DELETE FROM connector_oauth_states WHERE expires_at < ?", (now.isoformat(),)
        )
        conn.execute(
            """
            INSERT INTO connector_oauth_states (state, binding_id, user_id, redirect_uri, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                state,
                binding_id,
                user_id,
                redirect_uri,
                (now + timedelta(minutes=10)).isoformat(),
            ),
        )

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
        "scope": " ".join(authorization.get("scopes") or []),
        **(authorization.get("params") or {}),
    }
    return authorization["url"] + "?" + urllib.parse.urlencode(params)


def exchange_callback(state: str, code: str, user_id: str) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM connector_oauth_states WHERE state = ?", (state,)
        ).fetchone()
        conn.execute("DELETE FROM connector_oauth_states WHERE state = ?", (state,))
    if not row:
        raise ConnectorOAuthError("OAuth state is missing or already used")
    state_row = dict(row)
    if state_row["user_id"] != user_id:
        raise ConnectorOAuthError("OAuth state belongs to a different user")
    if utc_now() > parse_utc_datetime(state_row["expires_at"]):
        raise ConnectorOAuthError("OAuth state expired; start authorization again")

    binding, _, refresh = _oauth_specs(state_row["binding_id"])
    cred = binding["credential"]
    fields = cred.fields
    post_data = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": fields.get("client_id", ""),
            "client_secret": fields.get("client_secret", ""),
            "redirect_uri": state_row["redirect_uri"],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        refresh["token_url"],
        data=post_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with safe_urlopen(request, timeout=30) as response:
            token_data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            error_data = json.loads(exc.read().decode("utf-8"))
        except Exception:
            error_data = {}
        message = error_data.get("error_description") or error_data.get("error") or str(exc)
        raise ConnectorOAuthError(f"OAuth token exchange failed: {message}") from exc
    except Exception as exc:
        raise ConnectorOAuthError(f"OAuth token exchange failed: {exc}") from exc

    if not token_data.get("access_token"):
        raise ConnectorOAuthError("OAuth token exchange did not return an access token")
    if not token_data.get("refresh_token") and not fields.get("refresh_token"):
        raise ConnectorOAuthError(
            "OAuth token exchange did not return a refresh token; revoke the existing grant and authorize again"
        )

    updated = {**fields}
    for key in ("access_token", "refresh_token"):
        if token_data.get(key):
            updated[key] = token_data[key]
    if token_data.get("expires_in"):
        updated["expires_at"] = str(utc_now().timestamp() + float(token_data["expires_in"]))
    credential_service.update_credential_value(cred.reference_name, json.dumps(updated))
    return {"binding_id": state_row["binding_id"]}
