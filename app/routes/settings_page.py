"""Settings pages (general, password, OTP) and settings/prune/vector APIs.
Split from dashboard.py — see private/dashboard-split-plan.md."""

import httpx
from urllib.parse import urlparse

from fastapi import APIRouter, Request, Depends
from pydantic import BaseModel

from app.branding import CREDENTIAL_PREFIX, ENV_PREFIX
from app.security.dependencies import require_admin
from app.security.response_helpers import success_response, error_response
from app.services.auth_service import get_user_by_id
from app.routes.dashboard_shared import (
    render_page,
    require_auth,
    escape_html,
    _parse_manual_prune_cutoff,
)

router = APIRouter()


class ManualPruneRequest(BaseModel):
    resource_type: str
    before_date: str


@router.post("/api/dashboard/user-settings")
async def update_dashboard_user_settings(
    request: Request, session: dict = Depends(require_auth)
):
    from app.services import auth_service

    body = await request.json()
    timezone = body.get("timezone")
    if timezone is not None:
        timezone = str(timezone).strip() or None
    try:
        auth_service.update_user_timezone(session["user_id"], timezone)
    except ValueError as e:
        return error_response("INVALID_TIMEZONE", str(e), 400)
    return success_response({"timezone": timezone})


@router.post("/api/dashboard/system-settings")
async def update_dashboard_system_settings(
    request: Request, session: dict = Depends(require_admin)
):
    from app.database import get_db
    from app.services import audit_service
    from app.routes.auth import get_client_ip

    body = await request.json()
    retention_raw = str(body.get("scratchpad_retention_days", "")).strip()
    solo_raw = str(body.get("solo_mode_enabled", "")).strip().lower()

    try:
        retention_days = int(retention_raw)
    except ValueError:
        return error_response(
            "INVALID_RETENTION",
            "Scratchpad retention must be a whole number of days",
            400,
        )
    if retention_days < 1 or retention_days > 365:
        return error_response(
            "INVALID_RETENTION",
            "Scratchpad retention must be between 1 and 365 days",
            400,
        )
    if solo_raw not in ("true", "false"):
        return error_response(
            "INVALID_SOLO_MODE", "Solo mode must be true or false", 400
        )

    settings_to_save = {
        "scratchpad_retention_days": str(retention_days),
        "solo_mode_enabled": solo_raw,
    }
    with get_db() as conn:
        for key, value in settings_to_save.items():
            conn.execute(
                """
                INSERT INTO system_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )
        conn.commit()

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="system_setting_updated",
        resource_type="system_settings",
        result="success",
        details=settings_to_save,
        ip_address=get_client_ip(request),
    )
    return success_response({"settings": settings_to_save})


@router.post("/api/dashboard/prune")
async def prune_dashboard_data(
    body: ManualPruneRequest,
    request: Request,
    session: dict = Depends(require_admin),
):
    from app.database import get_db
    from app.services import audit_service
    from app.routes.auth import get_client_ip

    resource_type = body.resource_type.strip().lower()
    if resource_type not in {"audit", "activity"}:
        return error_response(
            "INVALID_RESOURCE",
            "resource_type must be either 'audit' or 'activity'",
            400,
        )

    try:
        cutoff_iso, cutoff_sql = _parse_manual_prune_cutoff(body.before_date)
    except ValueError:
        return error_response(
            "INVALID_DATE",
            "before_date must be a valid ISO date or datetime",
            400,
        )

    deleted = 0
    with get_db() as conn:
        if resource_type == "audit":
            cursor = conn.execute(
                "DELETE FROM audit_log WHERE timestamp < ?",
                (cutoff_sql,),
            )
            deleted = cursor.rowcount
        else:
            cursor = conn.execute(
                """
                DELETE FROM agent_activity
                WHERE status IN ('completed', 'cancelled', 'blocked')
                  AND COALESCE(ended_at, updated_at, started_at) < ?
                """,
                (cutoff_iso,),
            )
            deleted = cursor.rowcount
        conn.commit()

    action = "audit_pruned" if resource_type == "audit" else "activity_pruned"
    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action=action,
        resource_type=resource_type,
        result="success",
        details={
            "before_date": body.before_date,
            "cutoff": cutoff_iso,
            "deleted_count": deleted,
        },
        ip_address=get_client_ip(request),
    )
    return success_response(
        {
            "resource_type": resource_type,
            "before_date": body.before_date,
            "deleted_count": deleted,
        }
    )


@router.post("/api/dashboard/vector-settings")
async def update_dashboard_vector_settings(
    request: Request, session: dict = Depends(require_admin)
):
    from app.services.vector_settings_service import save_vector_settings
    from app.services import audit_service
    from app.routes.auth import get_client_ip

    body = await request.json()
    enabled_raw = body.get("vector_search_enabled")
    provider = body.get("vector_provider", "ollama").strip()
    auth_type = body.get("vector_auth_type", "none").strip()
    api_key = body.get("vector_api_key")
    model = body.get("vector_model", "").strip()
    url = body.get("vector_url", "").strip()
    dimension_raw = body.get("vector_dimension")

    if enabled_raw is not None and enabled_raw not in ("true", "false"):
        return error_response(
            "INVALID_ENABLED", "vector_search_enabled must be true or false", 400
        )

    if provider not in ("ollama", "generic"):
        return error_response(
            "INVALID_PROVIDER", "vector_provider must be ollama or generic", 400
        )

    if auth_type not in ("none", "bearer", "api_key"):
        return error_response(
            "INVALID_AUTH_TYPE",
            "vector_auth_type must be none, bearer, or api_key",
            400,
        )

    if not model:
        return error_response("INVALID_MODEL", "vector_model is required", 400)

    if not url:
        return error_response("INVALID_URL", "vector_url is required", 400)

    try:
        dimension = int(dimension_raw) if dimension_raw else None
        if dimension is not None and (dimension < 64 or dimension > 4096):
            return error_response(
                "INVALID_DIMENSION", "vector_dimension must be between 64 and 4096", 400
            )
    except ValueError:
        return error_response(
            "INVALID_DIMENSION", "vector_dimension must be a number", 400
        )

    updated = save_vector_settings(
        enabled=None if enabled_raw is None else (enabled_raw == "true"),
        provider=provider,
        url=url,
        model=model,
        dimension=dimension,
        auth_type=auth_type,
        api_key=api_key,
    )

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="system_setting_updated",
        resource_type="vector_settings",
        result="success",
        details={
            "vector_search_enabled": updated["vector_search_enabled"],
            "vector_auth_type": updated["vector_auth_type"],
            "vector_model": updated["vector_model"],
        },
        ip_address=get_client_ip(request),
    )

    return success_response({"settings": updated})


@router.post("/api/dashboard/vector-settings/test")
async def test_dashboard_vector_settings(
    request: Request, session: dict = Depends(require_admin)
):
    from app.services.vector_settings_service import test_vector_connection
    from app.services import audit_service
    from app.routes.auth import get_client_ip

    result = test_vector_connection()
    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="setup_verification",
        resource_type="vector_settings",
        result="success" if result.get("success") else "failure",
        details={k: v for k, v in result.items() if k != "error"},
        ip_address=get_client_ip(request),
    )
    if result.get("success"):
        return success_response(result)
    return error_response(
        "VECTOR_TEST_FAILED", result.get("error", "Vector test failed"), 400
    )


@router.get("/api/dashboard/vector-settings/models")
async def list_dashboard_vector_models(
    url: str, session: dict = Depends(require_admin)
):
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return error_response("INVALID_URL", "vector_url must be an http(s) URL", 400)

    tags_url = url.strip().rstrip("/") + "/api/tags"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(tags_url)
        if response.status_code != 200:
            return error_response(
                "MODEL_LIST_FAILED",
                f"Ollama returned {response.status_code} while listing models",
                502,
            )
        data = response.json()
    except Exception:
        return error_response(
            "MODEL_LIST_FAILED", "Could not fetch models from Ollama", 502
        )

    models = [
        model.get("name")
        for model in data.get("models", [])
        if isinstance(model, dict) and model.get("name")
    ]
    return success_response({"models": models})


@router.get("/settings")
async def settings_page(request: Request, session: dict = Depends(require_auth)):
    from app.database import get_db

    user = get_user_by_id(session["user_id"])
    is_admin = session.get("role") == "admin"

    def get_system_setting(key, default):
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM system_settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def count_rows(table: str, where: str = "1=1", params: tuple = ()) -> int:
        with get_db() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM {table} WHERE {where}", params
            ).fetchone()
            return int(row["count"] if row else 0)

    scratchpad_retention_days = get_system_setting("scratchpad_retention_days", "7")
    solo_mode_enabled = (
        get_system_setting("solo_mode_enabled", "true").lower() == "true"
    )
    credential_count = count_rows("credentials")

    account_html = f"""
    <div class="two-col">
      <div class="card">
        <h3>Account</h3>
        <p><strong>Display Name:</strong> {user.get("display_name", "")}</p>
        <p><strong>Email:</strong> {user.get("email", "")}</p>
        <p><strong>Role:</strong> {session.get("role", "user")}</p>
      </div>
      <div class="card">
        <h3>Security</h3>
        <div class="form-group">
          <a href="/settings/password" class="btn btn-secondary">Change Password</a>
        </div>
        <div class="form-group">
          <a href="/settings/otp" class="btn btn-secondary">Manage OTP</a>
        </div>
      </div>
    </div>"""

    from zoneinfo import available_timezones

    current_tz = user.get("timezone") or ""
    tz_option_list = '<option value="">Auto-detect from browser</option>'
    for _zone in sorted(available_timezones()):
        _sel = " selected" if _zone == current_tz else ""
        tz_option_list += (
            f'<option value="{escape_html(_zone)}"{_sel}>{escape_html(_zone)}</option>'
        )
    preferences_html = f"""
    <div class="card">
      <div class="section-header">
        <h3>Preferences</h3>
        <div class="section-note">Personal display options for your account.</div>
      </div>
      <form id="user-settings-form" onsubmit="saveUserSettings(event)">
        <div class="form-group">
          <label for="user-timezone">Timezone</label>
          <select id="user-timezone" style="max-width:360px">{tz_option_list}</select>
          <p class="form-hint">Dates and times across the dashboard are shown in this timezone. Stored data stays in UTC.</p>
        </div>
        <button type="submit" class="btn">Save Preferences</button>
        <div id="user-settings-result" style="margin-top:12px"></div>
      </form>
    </div>"""

    secrets_snapshot_html = ""
    if is_admin:
        from app.services.broker_service import get_broker_credential_hash
        from app.services.credential_rotation_service import get_key_status

        key_status = get_key_status()
        broker_active = bool(get_broker_credential_hash())
        broker_label = "present" if broker_active else "missing"
        broker_badge = "active" if broker_active else "stale"
        snapshot_rows = "".join(
            [
                f"<tr><td>Credential Entries</td><td>{credential_count}</td><td class='text-muted'>Encrypted secrets stored in the vault</td></tr>",
                f"<tr><td>Encryption Key</td><td><span class='badge badge-{ 'active' if key_status.get('mode') == 'keyring' else 'stale' }'>{escape_html(key_status.get('mode', 'unknown'))}</span></td><td class='text-muted'>Keyring size: {escape_html(key_status.get('keyring_size', 0))} · Primary key ID: {escape_html(key_status.get('primary_key_id', 'none'))}</td></tr>",
                f"<tr><td>Broker Credential</td><td><span class='badge badge-{broker_badge}'>{broker_label}</span></td><td class='text-muted'>Resolves {CREDENTIAL_PREFIX}* references at runtime</td></tr>",
            ]
        )
        secrets_snapshot_html = f"""
    <div class="card">
      <div class="section-header">
        <h3>Secrets Snapshot</h3>
        <div class="section-note">Current status for key and secret operations.</div>
      </div>
      <table>
        <thead><tr><th>Item</th><th>Status</th><th>Notes</th></tr></thead>
        <tbody>{snapshot_rows}</tbody>
      </table>
    </div>"""

    backup_html = ""
    if is_admin:
        backup_html = f"""
    <div class="card">
      <div class="section-header">
        <h3>Backup & Restore</h3>
        <div class="section-actions"><a href="/audit" class="btn btn-sm btn-secondary">Audit Log</a></div>
      </div>
      <div class="form-group">
        <button class="btn" onclick="exportBackup()">Export Encrypted Backup</button>
        <button class="btn btn-secondary" onclick="openModal('restore-modal')">Restore from Backup</button>
        <button class="btn btn-secondary" onclick="runMaintenance()">Run Maintenance</button>
      </div>
      <p class="form-hint">Maintenance marks stale activities using <code>{ENV_PREFIX}STALE_THRESHOLD_MINUTES</code> and deletes transient scratchpad memories older than the retention setting below. This does not touch credentials or connector bindings.</p>
      <div id="backup-result" style="margin-top:12px"></div>
      <hr class=divider>
      <h4 class="section-title">Startup Checks</h4>
      <div id="startup-checks"><button class="btn btn-secondary btn-sm" onclick="runStartupChecks()">Run Checks</button></div>
    </div>"""

    encryption_key_html = ""
    if is_admin:
        encryption_key_html = """
    <div class="card">
      <h3>Encryption Key</h3>
      <p class="text-muted" style="margin-bottom:12px">This key protects stored credential values. Rotate it if you want to re-encrypt all saved secrets with a new primary key, or restore a known-good key if you need to recover from a bad rotation.</p>
      <div class="form-group">
        <button class="btn btn-secondary" onclick="loadCredentialKeyStatus()">Check Status</button>
        <button class="btn btn-danger" onclick="openModal('credential-key-rotate-modal')">Rotate Key</button>
        <button class="btn btn-secondary" onclick="openModal('credential-key-restore-modal')">Restore Key</button>
      </div>
      <div id="credential-key-result" style="margin-top:12px"></div>
    </div>"""

    broker_html = ""
    if is_admin:
        broker_html = """
    <div class="card">
      <h3>Broker</h3>
      <p class="text-muted" style="margin-bottom:12px">The broker credential resolves <code>{CREDENTIAL_PREFIX}*</code> references at runtime. Rotate it if a local consumer should stop using the current broker secret.</p>
        <button class="btn btn-danger" onclick="rotateBroker()">Rotate Broker Credential</button>
      <div id="broker-result" style="margin-top:12px"></div>
    </div>"""

    system_settings_html = ""
    if is_admin:
        solo_checked = "checked" if solo_mode_enabled else ""
        system_settings_html = f"""
    <div class="card">
      <h3>System Behavior</h3>
      <form id="system-settings-form" onsubmit="saveSystemSettings(event)">
        <div class="form-group">
          <label>Scratchpad Retention</label>
          <input type="number" id="scratchpad-retention-days" min="1" max="365" value="{escape_html(scratchpad_retention_days)}" style="width:120px">
          <p class="form-hint">Used by Run Maintenance. Transient scratchpad memories older than this many days are permanently deleted.</p>
        </div>
        <label class="checkbox-label">
          <input type="checkbox" id="solo-mode-enabled" {solo_checked}>
          New API-created agents automatically read their owner's user scope
        </label>
        <p class="form-hint">Used when an agent is created without explicit scopes. Existing agents are not changed; edit their access on the Agents page.</p>
        <button type="submit" class="btn">Save Behavior Settings</button>
      </form>
      <div id="system-settings-result" style="margin-top:12px"></div>
    </div>"""

    vector_settings_html = ""
    if is_admin:
        from app.services.vector_settings_service import get_vector_settings

        vector_settings = get_vector_settings()
        vector_enabled = vector_settings["vector_search_enabled"].lower() == "true"
        vector_provider = vector_settings.get("vector_provider", "ollama")
        vector_model = vector_settings["vector_model"]
        vector_url = vector_settings["vector_url"]
        vector_dimension = vector_settings["vector_dimension"]
        vector_auth_type = vector_settings["vector_auth_type"]
        vector_has_api_key = vector_settings["vector_has_api_key"]
        vector_checked = "checked" if vector_enabled else ""
        vector_api_key_hint = (
            "Leave blank to keep the stored key."
            if vector_has_api_key
            else "Optional. Stored encrypted when provided."
        )
        vector_settings_html = f"""
    <div class="card">
      <h3>Vector Search</h3>
      <p class="text-muted" style="margin-bottom:12px">Enable semantic search using an Ollama-compatible embedding endpoint. Save the settings to turn it on immediately when the backend is healthy. Disable to use FTS5 text search only.</p>
      <form id="vector-settings-form" onsubmit="saveVectorSettings(event)">
        <label class="checkbox-label" style="margin-bottom:16px">
          <input type="checkbox" id="vector-search-enabled" {vector_checked}>
          Enable vector semantic search
        </label>
        <div class="form-group">
          <label>Provider</label>
          <select id="vector-provider" style="width:200px" onchange="onProviderChange(this.value)">
            <option value="ollama" {"selected" if vector_provider == "ollama" else ""}>Ollama</option>
            <option value="generic" {"selected" if vector_provider == "generic" else ""}>Generic</option>
          </select>
        </div>
        <div class="form-group" id="vector-model-text-group">
          <label>Model</label>
          <input type="text" id="vector-model" value="{escape_html(vector_model)}" style="width:240px">
          <p class="form-hint" id="vector-model-hint-text">For Ollama, use <code>nomic-embed-text</code> or another installed embedding model.</p>
        </div>
        <div class="form-group" id="vector-model-select-group" style="display:none">
          <label>Model</label>
          <select id="vector-model-select" style="width:240px">
            <option value="">Loading models...</option>
          </select>
          <div style="margin-top:8px">
            <button type="button" class="btn btn-secondary btn-sm" onclick="loadOllamaModels()">Load Models</button>
          </div>
          <p class="form-hint" id="vector-model-hint-select"></p>
        </div>
        <div class="form-group">
          <label>URL</label>
          <input type="text" id="vector-url" value="{escape_html(vector_url)}" style="width:300px">
          <p class="form-hint">Base URL for an Ollama-compatible API, for example <code>http://localhost:11434</code>.</p>
        </div>
        <div class="form-group">
          <label>Embedding Dimension</label>
          <input type="number" id="vector-dimension" min="64" max="4096" value="{escape_html(vector_dimension)}" style="width:120px">
          <p class="form-hint">Must match the configured embedding model. <code>nomic-embed-text</code> uses 768.</p>
        </div>
        <div class="form-group">
          <label>Auth Type</label>
          <select id="vector-auth-type" style="width:200px">
            <option value="none" {"selected" if vector_auth_type == "none" else ""}>None</option>
            <option value="bearer" {"selected" if vector_auth_type == "bearer" else ""}>Bearer Token</option>
            <option value="api_key" {"selected" if vector_auth_type == "api_key" else ""}>API Key</option>
          </select>
        </div>
        <div class="form-group">
          <label>API Key</label>
          <input type="password" id="vector-api-key" value="" style="width:300px" autocomplete="off">
          <p class="form-hint">{escape_html(vector_api_key_hint)}</p>
        </div>
        <div class="form-actions">
          <button type="submit" class="btn">Save Vector Settings</button>
          <button type="button" class="btn btn-secondary" onclick="testVectorSettings()">Test Connection</button>
        </div>
      </form>
      <div id="vector-settings-result" style="margin-top:12px"></div>
    </div>"""

    admin_modals = ""
    if is_admin:
        admin_modals = """
    <!-- Restore Modal -->
    <div class="modal-overlay" id="restore-modal" style="display:none">
      <div class="modal">
        <h3>Restore from Backup</h3>
        <div class="alert alert-danger">This can replace your current database and encryption key. Choose merge when you want to preserve current records.</div>
        <form id="restore-form" onsubmit="doRestore(event)">
          <div class="form-group">
            <label>Backup file *</label>
            <input type="file" id="restore-file" accept=".zip,.enc,.zip.enc" required>
          </div>
          <div class="form-group">
            <label>Backup Key</label>
            <input type="text" id="restore-backup-key" placeholder="Required for encrypted exports">
            <p class="form-hint">Encrypted backups need the one-time backup key shown at export time. Legacy unencrypted backups can still be restored without one.</p>
          </div>
          <div class="form-group">
            <label>Restore Mode *</label>
            <select id="restore-mode" required>
              <option value="replace_all">Replace all current data</option>
              <option value="merge">Merge non-conflicting records</option>
            </select>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('restore-modal')">Cancel</button>
            <button type="submit" class="btn btn-danger">Restore</button>
          </div>
        </form>
      </div>
    </div>

    <div class="modal-overlay" id="credential-key-rotate-modal" style="display:none">
      <div class="modal">
        <h3>Rotate Encryption Key</h3>
        <div class="alert alert-danger">This re-encrypts all credential entries with a new primary key. Keep backups of your database and keyring.</div>
        <form id="credential-key-rotate-form" onsubmit="rotateCredentialKey(event)">
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('credential-key-rotate-modal')">Cancel</button>
            <button type="submit" class="btn btn-danger">Rotate Key</button>
          </div>
        </form>
      </div>
    </div>

    <div class="modal-overlay" id="credential-key-restore-modal" style="display:none">
      <div class="modal">
        <h3>Restore Encryption Key</h3>
        <form id="credential-key-restore-form" onsubmit="restoreCredentialKey(event)">
          <div class="form-group">
            <label>Fernet Key *</label>
            <input type="text" id="credential-key-restore-key" required>
            <p class="form-hint">The key must decrypt all current credential entries before it is accepted.</p>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('credential-key-restore-modal')">Cancel</button>
            <button type="submit" class="btn btn-danger">Restore Key</button>
          </div>
        </form>
      </div>
    </div>"""

    js = """
    <script>
    async function saveUserSettings(e) {{
      e.preventDefault();
      const tz = document.getElementById('user-timezone').value;
      const j = await apiFetch('/api/dashboard/user-settings', {{ method: 'POST', body: JSON.stringify({{ timezone: tz }}) }});
      const res = document.getElementById('user-settings-result');
      if (j.ok) {{
        window.AC_USER_TZ = tz || (Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
        if (window.applyLocalTimes) window.applyLocalTimes(document);
        showToast('Preferences saved', 'success');
        res.innerHTML = '<div class="alert alert-success">Saved. Times now shown in ' + escapeHtml(window.AC_USER_TZ) + '.</div>';
      }} else {{
        res.innerHTML = '<div class="alert alert-danger">' + escapeHtml((j.error && j.error.message) || 'Save failed') + '</div>';
      }}
    }}
    window.saveUserSettings = saveUserSettings;

    async function exportBackup() {{
      const r = await fetch('/api/backup/export', {{
        method: 'POST',
      }});
      if (r.ok) {{
        const blob = await r.blob();
        const backupKey = r.headers.get('{BACKUP_KEY_HEADER}');
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = '{APP_SLUG}-backup.zip.enc'; a.click();
        URL.revokeObjectURL(url);
        const keyHtml = backupKey
          ? '<div class="alert alert-warning"><strong>Save this backup key now.</strong> It will not be shown again.<div class="api-key-display" style="margin-top:8px"><code>' + backupKey + '</code></div><button class="copy-btn" onclick="copyToClipboard(' + JSON.stringify(backupKey) + ', this)">Copy</button></div>'
          : '<div class="alert alert-warning"><strong>Backup key missing from response.</strong></div>';
        document.getElementById('backup-result').innerHTML = keyHtml;
        showToast('Encrypted backup downloaded');
      }} else {{
        const j = await r.json();
        showToast(j.error?.message || 'Export failed', 'danger');
      }}
    }}
    async function runMaintenance() {{
      const r = await fetch('/api/backup/maintenance', {{ method: 'POST' }});
      const j = await r.json();
      if (j.ok) {{
        document.getElementById('backup-result').innerHTML =
          '<div class="alert alert-success">Maintenance complete. Stale activities marked: <code>' + j.data.stale_activities_marked + '</code>. Scratchpad memories pruned: <code>' + j.data.scratchpad_pruned + '</code>.</div>';
        showToast('Maintenance complete');
      }} else {{ showToast(j.error?.message || 'Failed', 'danger'); }}
    }}
    async function runStartupChecks() {{
      const r = await fetch('/api/backup/startup-checks');
      const j = await r.json();
      if (j.ok) {{
        const checks = j.data.checks.map(c => '<div style="margin:4px 0"><span class="badge badge-' + (c.status==='OK'?'active':'stale') + '">' + c.status + '</span> ' + c.check + ': ' + c.message + '</div>').join('');
        document.getElementById('startup-checks').innerHTML = checks;
      }} else {{ showToast('Failed', 'danger'); }}
    }}
    async function rotateBroker() {{
      if (!confirm('Rotate broker credential? The new credential will be shown once.')) return;
      const j = await apiFetch('/api/dashboard/broker/rotate', {{ method: 'POST' }});
      if (j.ok) {{
        document.getElementById('broker-result').innerHTML =
          '<div class="alert alert-danger"><strong>Save this credential now - it will not be shown again!</strong></div>' +
          '<div class="api-key-display"><code>' + j.data.credential + '</code></div>' +
          '<button class="copy-btn" onclick="copyToClipboard(' + JSON.stringify(j.data.credential) + ', this)">Copy</button>';
      }} else {{ showToast(j.error.message || 'Failed', 'danger'); }}
    }}
    async function loadCredentialKeyStatus() {{
      const j = await apiFetch('/api/credentials/rotate/status');
      if (j.ok) {{
        const s = j.data.key_status;
        document.getElementById('credential-key-result').innerHTML =
          '<div class="alert alert-success">Mode: <code>' + s.mode + '</code> · Keyring size: <code>' + s.keyring_size + '</code> · Primary key ID: <code>' + s.primary_key_id + '</code></div>';
      }} else {{ showToast(j.error?.message || 'Status failed', 'danger'); }}
    }}
    async function rotateCredentialKey(e) {{
      e.preventDefault();
      const j = await apiFetch('/api/credentials/rotate', {{
        method: 'POST'
      }});
      if (j.ok) {{
        closeModal('credential-key-rotate-modal');
        document.getElementById('credential-key-rotate-form').reset();
        document.getElementById('credential-key-result').innerHTML =
          '<div class="alert alert-success">' + j.data.message + ' Re-encrypted: <code>' + j.data.re_encrypted_count + '</code>. Keyring size: <code>' + j.data.keyring_size + '</code>.</div>';
      }} else {{ showToast(j.error?.message || 'Rotation failed', 'danger'); }}
    }}
    async function restoreCredentialKey(e) {{
      e.preventDefault();
      const key = document.getElementById('credential-key-restore-key').value.trim();
      if (!key) {{ showToast('Enter key first', 'warning'); return; }}
      const j = await apiFetch('/api/credentials/restore-key', {{
        method: 'POST',
        body: JSON.stringify({{ key_base64: key }})
      }});
      if (j.ok) {{
        closeModal('credential-key-restore-modal');
        document.getElementById('credential-key-restore-form').reset();
        document.getElementById('credential-key-result').innerHTML = '<div class="alert alert-success">' + j.data.message + '</div>';
      }} else {{ showToast(j.error?.message || 'Restore failed', 'danger'); }}
    }}
    async function doRestore(e) {{
      e.preventDefault();
      const formData = new FormData();
      const file = document.getElementById('restore-file').files[0];
      const backupKey = document.getElementById('restore-backup-key').value.trim();
      const mode = document.getElementById('restore-mode').value;
      if (!file) {{ showToast('Select a backup file', 'warning'); return; }}
      formData.append('backup', file);
      if (backupKey) formData.append('backup_key', backupKey);
      formData.append('mode', mode);
      const r = await fetch('/api/backup/restore', {{ method: 'POST', body: formData }});
      const j = await r.json();
      if (j.ok) {{ showToast('Restore complete (' + j.data.mode + ')'); closeModal('restore-modal'); }}
      else {{ showToast(j.error?.message || 'Restore failed', 'danger'); }}
    }}
    async function saveSystemSettings(e) {{
      e.preventDefault();
      const body = {{
        scratchpad_retention_days: document.getElementById('scratchpad-retention-days').value,
        solo_mode_enabled: document.getElementById('solo-mode-enabled').checked ? 'true' : 'false',
      }};
      const j = await apiFetch('/api/dashboard/system-settings', {{
        method: 'POST',
        body: JSON.stringify(body)
      }});
      if (j.ok) {{
        document.getElementById('system-settings-result').innerHTML = '<div class="alert alert-success">Behavior settings saved.</div>';
      }} else {{
        showToast(j.error?.message || 'Failed to save settings', 'danger');
      }}
    }}
    async function saveVectorSettings(e) {{
      e.preventDefault();
      const provider = document.getElementById('vector-provider').value;
      const selectedModel = document.getElementById('vector-model-select').value;
      const textModel = document.getElementById('vector-model').value;
      const model = provider === 'ollama' ? (selectedModel || textModel) : textModel;
      const body = {{
        vector_search_enabled: document.getElementById('vector-search-enabled').checked ? 'true' : 'false',
        vector_provider: provider,
        vector_model: model,
        vector_url: document.getElementById('vector-url').value,
        vector_dimension: document.getElementById('vector-dimension').value,
        vector_auth_type: document.getElementById('vector-auth-type').value,
      }};
      const apiKey = document.getElementById('vector-api-key').value;
      if (apiKey) body.vector_api_key = apiKey;
      const j = await apiFetch('/api/dashboard/vector-settings', {{
        method: 'POST',
        body: JSON.stringify(body)
      }});
      if (j.ok) {{
        document.getElementById('vector-api-key').value = '';
        document.getElementById('vector-model').value = model;
        document.getElementById('vector-settings-result').innerHTML = '<div class="alert alert-success">Vector settings saved.</div>';
      }} else {{
        showToast(j.error?.message || 'Failed to save vector settings', 'danger');
      }}
    }}
    async function testVectorSettings() {{
      const result = document.getElementById('vector-settings-result');
      result.innerHTML = '<div class="alert">Testing vector connection...</div>';
      const j = await apiFetch('/api/dashboard/vector-settings/test', {{ method: 'POST' }});
      if (j.ok) {{
        result.innerHTML = '<div class="alert alert-success">Vector connection succeeded for ' + escapeHtml(j.data.model || 'configured model') + '.</div>';
      }} else {{
        result.innerHTML = '<div class="alert alert-danger">' + escapeHtml(j.error?.message || 'Vector connection failed') + '</div>';
      }}
    }}
    async function onProviderChange(provider) {{
      const textGroup = document.getElementById('vector-model-text-group');
      const selectGroup = document.getElementById('vector-model-select-group');
      if (provider === 'ollama') {{
        textGroup.style.display = 'none';
        selectGroup.style.display = 'block';
        document.getElementById('vector-model-hint-select').textContent = 'Click Load Models after confirming the URL.';
      }} else {{
        textGroup.style.display = 'block';
        selectGroup.style.display = 'none';
      }}
    }}
    async function loadOllamaModels() {{
      const url = document.getElementById('vector-url').value.trim();
      if (!url) {{
        document.getElementById('vector-model-select').innerHTML = '<option value="">Enter URL first</option>';
        return;
      }}
      try {{
        const fullUrl = '/api/dashboard/vector-settings/models?url=' + encodeURIComponent(url);
        const resp = await fetch(fullUrl, {{ method: 'GET', credentials: 'same-origin' }});
        if (!resp.ok) throw new Error('Failed to fetch');
        const data = await resp.json();
        if (!data.ok) throw new Error(data.error?.message || 'Failed to fetch');
        const models = data.data?.models || [];
        if (models.length === 0) throw new Error('No models');
        const select = document.getElementById('vector-model-select');
        const currentModel = select.value || document.getElementById('vector-model').value || '';
        select.innerHTML = models.map(name => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join('');
        if (currentModel) select.value = currentModel;
        document.getElementById('vector-model-hint-select').textContent = 'Select an embedding model from Ollama';
      }} catch (e) {{
        document.getElementById('vector-model-select').innerHTML = '<option value="">Failed to load models</option>';
        document.getElementById('vector-model-hint-select').textContent = 'Could not fetch models from Ollama. Ensure Ollama is running.';
      }}
    }}
    function initVectorProvider() {{
      const provider = document.getElementById('vector-provider').value;
      if (provider === 'ollama') {{
        document.getElementById('vector-model-text-group').style.display = 'none';
        document.getElementById('vector-model-select-group').style.display = 'block';
        document.getElementById('vector-model-hint-select').textContent = 'Click Load Models after confirming the URL.';
      }}
    }}
    document.addEventListener('DOMContentLoaded', initVectorProvider);
    window.saveVectorSettings = saveVectorSettings;
    window.testVectorSettings = testVectorSettings;
    window.onProviderChange = onProviderChange;
    window.loadOllamaModels = loadOllamaModels;
    </script>"""
    js = js.replace("{{", "{").replace("}}", "}")

    return render_page(
        "Settings",
        f"""
    <div class="page-header"><h1>Settings</h1></div>
    {account_html}
    {preferences_html}
    {secrets_snapshot_html}
    {system_settings_html}
    {vector_settings_html}
    {encryption_key_html}
    {broker_html}
    {backup_html}
    {admin_modals}
    """,
        "/settings",
        js,
        session=session,
    )


# ─── SETTINGS PASSWORD ──────────────────────────────────────────────────────────


@router.get("/settings/password")
async def settings_password_page(
    request: Request, session: dict = Depends(require_auth)
):
    js = """
    <script>
    function showPasswordStatus(message, type) {
      const box = document.getElementById('password-status');
      if (!box) return;
      box.className = 'alert alert-' + (type || 'success');
      box.textContent = message;
      box.style.display = 'block';
    }
    async function submitPassword(e) {
      e.preventDefault();
      const status = document.getElementById('password-status');
      if (status) {
        status.style.display = 'none';
        status.textContent = '';
      }
      const current = document.getElementById('current-password').value;
      const new_pw = document.getElementById('new-password').value;
      const confirm = document.getElementById('confirm-password').value;
      if (new_pw !== confirm) { showPasswordStatus('New passwords do not match', 'warning'); return; }
      if (new_pw.length < 8) { showPasswordStatus('Password must be at least 8 characters', 'warning'); return; }
      const j = await apiFetch('/api/auth/password', {
        method: 'POST',
        body: JSON.stringify({ current_password: current, new_password: new_pw })
      });
      if (j.ok) {
        e.target.reset();
        showPasswordStatus('Password updated. You can continue this session or use the logout button below to sign back in with the new password.', 'success');
      } else {
        showPasswordStatus(j.error?.message || 'Failed', 'danger');
      }
    }
    </script>"""
    return render_page(
        "Change Password",
        """
    <div class="page-header"><h1>Change Password</h1></div>
    <div class="card" style="max-width:500px">
      <h3>Update Your Password</h3>
      <div id="password-status" class="alert alert-success" style="display:none"></div>
      <form id="password-form" onsubmit="submitPassword(event)">
        <div class="form-group">
          <label>Current Password</label>
          <input type="password" id="current-password" autocomplete="current-password" required>
        </div>
        <div class="form-group">
          <label>New Password</label>
          <input type="password" id="new-password" minlength="8" autocomplete="new-password" required>
        </div>
        <div class="form-group">
          <label>Confirm New Password</label>
          <input type="password" id="confirm-password" minlength="8" autocomplete="new-password" required>
        </div>
        <div class="modal-footer" style="justify-content:flex-start;gap:8px;padding-left:0;padding-right:0">
          <button type="submit" class="btn">Update Password</button>
          <a class="btn btn-secondary" href="/logout">Log Out</a>
        </div>
      </form>
    </div>
    """,
        "/settings",
        js,
        session=session,
    )


# ─── SETTINGS OTP ───────────────────────────────────────────────────────────────


@router.get("/settings/otp")
async def settings_otp_page(request: Request, session: dict = Depends(require_auth)):
    from app.services.auth_service import is_otp_enrolled

    enrolled = is_otp_enrolled(session["user_id"])

    if enrolled:
        body_html = """
        <div class="alert alert-success">OTP is currently enrolled on your account.</div>
        <p class="text-muted">Resetting OTP requires your current password and an existing authenticator code. Disable OTP to turn it off completely and re-enroll later if you want.</p>
        <form id="otp-reset-form" onsubmit="submitOtpReset(event)">
          <div class="form-group">
            <label for="otp-current-password">Current Password</label>
            <input type="password" id="otp-current-password" autocomplete="current-password" required>
          </div>
          <div class="form-group">
            <label for="otp-current-code">Current OTP Code</label>
            <input type="text" id="otp-current-code" maxlength="6" pattern="[0-9]*" inputmode="numeric" required style="width:160px">
          </div>
          <button type="submit" class="btn btn-danger">Reset OTP</button>
        </form>
        <form id="otp-disable-form" onsubmit="submitOtpDisable(event)" style="margin-top:16px">
          <div class="form-group">
            <label for="otp-disable-password">Current Password</label>
            <input type="password" id="otp-disable-password" autocomplete="current-password" required>
          </div>
          <div class="form-group">
            <label for="otp-disable-code">Current OTP Code</label>
            <input type="text" id="otp-disable-code" maxlength="6" pattern="[0-9]*" inputmode="numeric" required style="width:160px">
          </div>
          <button type="submit" class="btn btn-secondary">Disable OTP</button>
        </form>
        <div id="otp-reset-result" style="margin-top:16px"></div>"""
        js = """
        <script>
        async function submitOtpReset(e) {
          e.preventDefault();
          if (!confirm('Reset OTP? Your existing authenticator setup will be replaced.')) return;
          await startOtpEnrollment('otp-reset-result');
        }
        async function submitOtpDisable(e) {
          e.preventDefault();
          if (!confirm('Disable OTP on this account?')) return;
          const password = document.getElementById('otp-disable-password')?.value || '';
          const otp = document.getElementById('otp-disable-code')?.value || '';
          if (!password || !otp) { showToast('Enter your current password and OTP code', 'warning'); return; }
          const j = await apiFetch('/api/auth/otp/disable', {
            method: 'POST',
            body: JSON.stringify({ current_password: password, otp_code: otp })
          });
          if (j.ok) {
            document.getElementById('otp-disable-form').reset();
            document.getElementById('otp-reset-result').innerHTML = '<div class="alert alert-success"><strong>OTP is disabled.</strong></div>';
          } else {
            showToast(j.error?.message || 'Failed', 'danger');
          }
        }
        </script>"""
    else:
        body_html = """
        <div class="alert alert-warning">OTP is not currently enrolled.</div>
        <p class="text-muted">Enrolling OTP adds a second layer of authentication. Enter your current password, then scan the QR code with an authenticator app.</p>
        <div class="form-group">
          <label for="otp-current-password">Current Password</label>
          <input type="password" id="otp-current-password" autocomplete="current-password" required>
        </div>
        <button class="btn" onclick="enrollOtp()">Start OTP Setup</button>
        <div id="otp-enroll-result" style="margin-top:16px"></div>"""
        js = """
        <script>
        async function enrollOtp() {
          await startOtpEnrollment('otp-enroll-result');
        }
        </script>"""

    setup_js = """
        <script>
        async function startOtpEnrollment(targetId) {
          const target = document.getElementById(targetId);
          const password = document.getElementById('otp-current-password')?.value || '';
          const currentOtp = document.getElementById('otp-current-code')?.value || '';
          if (!password) { showToast('Enter your current password', 'warning'); return; }
        target.innerHTML = '<div class="alert alert-warning">Preparing OTP setup...</div>';
          const body = { current_password: password };
          if (currentOtp) body.otp_code = currentOtp;
          const j = await apiFetch('/api/auth/otp/enroll', {
            method: 'POST',
            body: JSON.stringify(body)
          });
          if (j.ok) {
            const qrHtml = j.data.qr_svg
              ? '<img class="otp-qr" src="' + j.data.qr_svg + '" alt="OTP enrollment QR code">'
              : '<div class="alert alert-warning">QR code could not be generated. Use the manual secret instead.</div>';
            target.innerHTML =
              '<div class="alert alert-success"><strong>Scan this QR code in your authenticator app.</strong></div>' +
              '<div class="otp-enroll-layout">' +
                qrHtml +
                '<div>' +
                  '<p class="text-muted">Or enter this secret manually:</p>' +
                  '<div class="mono" style="word-break:break-all;margin:12px 0">' + escapeHtml(j.data.secret) + '</div>' +
                  '<label for="otp-confirm-code">Enter the 6-digit code from your app</label>' +
                  '<input type="text" id="otp-confirm-code" maxlength="6" pattern="[0-9]*" inputmode="numeric" style="width:160px">' +
                  '<div style="margin-top:10px"><button class="btn" onclick="confirmOtpEnrollment(\\'' + targetId + '\\')">Verify and Enable OTP</button></div>' +
                '</div>' +
              '</div>';
          } else {
            target.innerHTML = '<div class="alert alert-danger">' + escapeHtml(j.error?.message || 'Failed to prepare OTP setup') + '</div>';
            showToast(j.error?.message || 'Failed', 'danger');
          }
        }
        async function confirmOtpEnrollment(targetId) {
          const code = document.getElementById('otp-confirm-code')?.value || '';
          if (!code) { showToast('Enter the authenticator code', 'warning'); return; }
          const j = await apiFetch('/api/auth/otp/confirm', {
            method: 'POST',
            body: JSON.stringify({ otp_code: code })
          });
          if (j.ok) {
            document.getElementById(targetId).innerHTML =
              '<div class="alert alert-success"><strong>OTP is enabled.</strong></div>';
          } else {
            showToast(j.error?.message || 'Invalid code', 'danger');
          }
        }
        </script>
    """
    js = setup_js + js

    return render_page(
        "Manage OTP",
        f"""
    <div class="page-header"><h1>Two-Factor Authentication</h1></div>
    <div class="card" style="max-width:600px">
      {body_html}
    </div>
    """,
        "/settings",
        js,
        session=session,
    )


