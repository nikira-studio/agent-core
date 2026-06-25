import json

from fastapi import APIRouter, Depends, Request
from app.branding import APP_NAME, JS_WINDOW_EVENT
from app.security.context import build_user_context
from app.security.scope_enforcer import ScopeEnforcer
from app.database import get_db
from app.services import credential_service
from app.services import workspace_service
from app.services import connector_service
from app.services import adapter_loader
from app.services.agent_service import list_agents
from app.routes.dashboard_shared import (
    render_page,
    escape_html,
    require_auth,
    get_icon,
    local_dt,
)

router = APIRouter()


def _binding_guidance_for_connector_type(
    ct: dict, adapter_entry: dict | None = None
) -> dict:
    credential_fields = list(ct.get("required_credential_fields") or [])
    config_fields: list[str] = []
    backend_json = ct.get("backend_json")
    backend = None
    if backend_json:
        try:
            backend = json.loads(backend_json)
        except Exception:
            backend = None

    def visit(value):
        if isinstance(value, dict):
            if value.get("from") == "config" and value.get("field"):
                field = value["field"]
                if field not in config_fields:
                    config_fields.append(field)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    if isinstance(backend, dict):
        visit(backend)

    if (
        ct.get("backend_type") == "generic_http"
        or ct.get("provider_type") == "generic_http"
    ) and "base_url" not in config_fields:
        config_fields.append("base_url")

    display_name = ct.get("display_name") or ct.get("id") or "Binding"
    setup = (adapter_entry or {}).get("setup") or {}
    return {
        "suggested_binding_name": display_name,
        "suggested_credential_name": f"{display_name} credentials",
        "credential_fields": credential_fields,
        "config_fields": config_fields,
        "setup_instructions": setup.get("instructions") or "",
        "documentation_url": setup.get("documentation_url") or "",
    }


def _binding_recipe_line(guidance: dict) -> str:
    bits = [f"Suggested binding: {guidance.get('suggested_binding_name') or ''}".strip()]
    credential_fields = guidance.get("credential_fields") or []
    config_fields = guidance.get("config_fields") or []
    if credential_fields:
        bits.append("Credential JSON fields: " + ", ".join(credential_fields))
    if config_fields:
        bits.append("Config JSON fields: " + ", ".join(config_fields))
    return " · ".join([b for b in bits if b and b != "Suggested binding: "])


def _render_adapter_cards(adapter_entries: list[dict], ctx) -> str:
    adapter_cards = ""
    for adapter in adapter_entries:
        source_label = "System" if adapter["source_kind"] == "system" else "Local"
        installed = bool(adapter.get("installed"))
        installable = bool(adapter.get("installable"))
        update_available = bool(adapter.get("update_available"))
        req = adapter.get("requirements_summary") or {}
        missing_bins = req.get("bins") or []
        missing_env = req.get("env") or []
        required_config = req.get("config") or []
        credential_fields = req.get("credential_fields") or []
        source_version = adapter.get("available_version") or adapter.get("version")
        installed_version = adapter.get("installed_version")
        guidance = {
            "suggested_binding_name": adapter.get("display_name") or adapter.get("id") or "Binding",
            "credential_fields": credential_fields,
            "config_fields": required_config,
        }
        req_bits = []
        if missing_bins:
            req_bits.append("Requires binary: " + ", ".join(missing_bins))
        if missing_env:
            req_bits.append("Requires env: " + ", ".join(missing_env))
        if credential_fields:
            req_bits.append("Credential JSON fields: " + ", ".join(credential_fields))
        if required_config:
            req_bits.append("Binding config JSON: " + ", ".join(required_config))
        recipe_line = _binding_recipe_line(guidance)
        req_text = " · ".join(req_bits)
        req_hover = " | ".join(req_bits)
        if installed:
            state_badge = '<span class="badge badge-success">Installed</span>'
            if update_available:
                state_badge += ' <span class="badge badge-warning">Update available</span>'
            action_btn = ""
            if ctx.is_admin:
                if update_available:
                    action_btn += (
                        f"<button type='button' class='btn btn-sm btn-primary' "
                        f"onclick='updateAdapter(this, \"{adapter['id']}\")'>Update</button>"
                    )
                action_btn += (
                    f"<button type='button' class='btn btn-sm btn-danger' "
                    f"onclick='uninstallAdapter(this, \"{adapter['id']}\")'>Uninstall</button>"
                )
        elif installable:
            state_badge = '<span class="badge badge-warning">Available</span>'
            action_btn = (
                f"<button type='button' class='btn btn-sm btn-primary' "
                f"onclick='installAdapter(this, \"{adapter['id']}\")'>Install</button>"
                if ctx.is_admin
                else ""
            )
        else:
            state_badge = '<span class="badge badge-danger">Unavailable</span>'
            action_btn = ""

        requirement_line = ""
        if req_text:
            requirement_line = (
                "<div style='font-size:0.82em;"
                + ("color:var(--danger);" if not adapter.get("requirements_met", True) else "color:var(--text-muted);")
                + "margin-top:0.35rem'"
                + (f" title=\"{escape_html(req_hover)}\"" if req_hover else "")
                + ">"
                + escape_html(req_text)
                + "</div>"
            )
        binding_recipe_line = ""
        if recipe_line:
            binding_recipe_line = (
                "<div style='font-size:0.8em;color:var(--text-muted);margin-top:0.25rem'>"
                + escape_html(recipe_line)
                + "</div>"
            )
        version_line = (
            f"{escape_html(str(source_version))} &middot; {source_label}"
            if source_version
            else source_label
        )
        installed_version_line = ""
        if installed and installed_version and installed_version != source_version:
            installed_version_line = (
                "<div style='font-size:0.75em;color:var(--text-muted);margin-top:0.1rem'>"
                + f"Installed {escape_html(str(installed_version))}"
                + "</div>"
            )

        adapter_cards += f"""
        <div class='connector-type-card' data-adapter-card data-search-text="{escape_html((adapter.get("display_name", "") or "") + " " + (adapter.get("description", "") or "") + " " + (source_version or "") + " " + (installed_version or "") + " " + adapter.get("source_kind", "") + " " + req_text)}">
          <div style="padding:0 0 0.5rem">
            <div class='connector-type-name' style="margin:0">{escape_html(adapter["display_name"])}</div>
            <div style="font-size:0.8em;color:var(--text-muted);margin-top:0.1rem">
              {version_line}
            </div>
            {installed_version_line}
            <div class='connector-type-desc' style="margin-top:0.35rem">{escape_html(adapter.get("description", "") or "")}</div>
            {requirement_line}
            {binding_recipe_line}
          </div>
          <div class='connector-type-footer' style="margin-top:auto; display:flex; flex-direction:column; gap:8px; align-items:stretch;">
            <div style="display:flex;align-items:center;gap:0.4rem;">{state_badge}</div>
            <div style="display:flex;gap:0.4rem;align-items:center;justify-content:flex-end;">
              {action_btn}
            </div>
          </div>
        </div>"""
    return adapter_cards


@router.get("/connectors")
async def connectors_page(request: Request, session: dict = Depends(require_auth)):
    ctx = build_user_context(session)
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )

    connector_types = connector_service.list_connector_types()
    available_adapters = {
        entry["id"]: entry for entry in adapter_loader.list_available_adapters()
    }
    binding_guidance = {
        ct["id"]: _binding_guidance_for_connector_type(
            ct, available_adapters.get(ct["id"])
        )
        for ct in connector_types
    }

    all_bindings = connector_service.list_bindings()
    visible_bindings = [b for b in all_bindings if enforcer.can_read(b["scope"])]
    connector_type_count = len(connector_types)
    visible_binding_count = len(visible_bindings)
    enabled_binding_count = len([b for b in visible_bindings if b.get("enabled")])
    failed_binding_count = len([b for b in visible_bindings if b.get("last_error")])
    with get_db() as conn:
        execution_rows = conn.execute(
            """
            SELECT ce.id, ce.binding_id, ce.action, ce.result_status, ce.error_message,
                   ce.executed_at, cb.name as binding_name, cb.scope, ct.display_name as connector_display_name
            FROM connector_executions ce
            JOIN connector_bindings cb ON ce.binding_id = cb.id
            JOIN connector_types ct ON cb.connector_type_id = ct.id
            ORDER BY ce.executed_at DESC
            LIMIT 40
            """
        ).fetchall()
    visible_executions = [
        dict(row)
        for row in execution_rows
        if ctx.is_admin or enforcer.can_read(row["scope"])
    ][:8]

    workspaces = (
        workspace_service.list_workspaces()
        if ctx.is_admin
        else workspace_service.list_workspaces(owner_user_id=ctx.user_id)
    )

    agents = list_agents() if ctx.is_admin else list_agents(owner_user_id=ctx.user_id)

    credential_entries = [
        e
        for e in (credential_service.list_credentials(limit=500) or [])
        if enforcer.can_read(e.get("scope", ""))
    ]

    user_scope = f"user:{session['user_id']}"
    workspace_scope_opts = "".join(
        f'<option value="workspace:{p["id"]}">workspace:{p["id"]}</option>'
        for p in workspaces
    )
    agent_scope_opts = "".join(
        f'<option value="agent:{a["id"]}">agent:{a["id"]}</option>'
        for a in agents
        if a.get("is_active")
    )
    scope_options = (
        f'<option value="{user_scope}">{user_scope}</option>\n'
        f"{workspace_scope_opts}\n"
        f"{agent_scope_opts}"
    )

    connector_type_opts = "".join(
        (
            f'<option value="{ct["id"]}" '
            f'data-display-name="{escape_html(ct["display_name"])}" '
            f'data-guidance="{escape_html(json.dumps(binding_guidance.get(ct["id"], {})))}">'
            f'{escape_html(ct["display_name"])}'
            f'</option>'
        )
        for ct in connector_types
    )
    credential_opts = "".join(
        f'<option value="{e["id"]}" data-credential-name="{escape_html(e.get("name", e["id"]))}" data-credential-scope="{escape_html(e.get("scope", ""))}">'
        f'{escape_html(e.get("name", e["id"]))} ({escape_html(e.get("scope", ""))} / {escape_html(e.get("reference_name", ""))})'
        f'</option>'
        for e in credential_entries
    )

    bindings_rows = ""
    binding_counts = {ct["id"]: 0 for ct in connector_types}
    for b in visible_bindings:
        if b["connector_type_id"] in binding_counts:
            binding_counts[b["connector_type_id"]] += 1

    # Per connector-type health, derived from the last stored test result on each
    # visible enabled binding. Surfaced as a badge on the Service Catalog card and
    # refreshed when the operator runs Check Health.
    ct_health = {}
    for ct in connector_types:
        enabled = [
            b
            for b in visible_bindings
            if b["connector_type_id"] == ct["id"] and b.get("enabled")
        ]
        failed = [b for b in enabled if b.get("last_error")]
        tested_ok = [
            b for b in enabled if b.get("last_tested_at") and not b.get("last_error")
        ]
        if not enabled:
            ct_health[ct["id"]] = ("none", 0)
        elif failed:
            ct_health[ct["id"]] = ("issues", len(failed))
        elif len(tested_ok) == len(enabled):
            ct_health[ct["id"]] = ("healthy", len(enabled))
        else:
            ct_health[ct["id"]] = ("untested", len(enabled) - len(tested_ok))
    for b in visible_bindings:
        ct = next(
            (c for c in connector_types if c["id"] == b["connector_type_id"]), None
        )
        text_style = (
            "text-decoration:line-through;opacity:0.62;" if not b.get("enabled") else ""
        )
        if b.get("enabled") and not b.get("last_error"):
            status_cls = "status-ok"
            status_text = "Enabled" if b.get("enabled") else "Disabled"
        else:
            status_cls = "status-error"
            status_text = (
                "Error"
                if b.get("last_error")
                else ("Disabled" if not b.get("enabled") else "OK")
            )
        if b.get("last_error"):
            status_text = f"Error: {str(b['last_error'])[:40]}"
        elif b.get("last_tested_at"):
            status_text = f"OK ({b['last_tested_at'][:10]})"
        oauth_button = ""
        if ct and ct.get("auth_type") == "oauth2":
            oauth_label = (
                "Authorize Google"
                if ct.get("id", "").startswith("google_")
                else "Authorize OAuth"
            )
            oauth_button = (
                f"<button type='button' class='btn btn-sm btn-primary' "
                f"onclick='authorizeBindingOAuth(\"{b['id']}\")'>{oauth_label}</button>"
            )
        bindings_rows += f"""
        <tr data-binding-id="{b["id"]}">
          <td style="{text_style}">{escape_html(b.get("name", ""))}</td>
          <td style="{text_style}">{escape_html(ct.get("display_name", "") if ct else b.get("connector_type_id", ""))}</td>
          <td style="{text_style}"><code>{escape_html(b.get("scope", ""))}</code></td>
          <td class="{status_cls}" style="{text_style}">{escape_html(status_text)}</td>
          <td class='actions-cell'>
            {oauth_button}
            <button type='button' class='btn btn-sm btn-secondary' onclick='editBinding("{b["id"]}")'>Edit</button>
            <button type='button' class='btn btn-sm btn-secondary' onclick='viewExecutions("{b["id"]}")'>History</button>
            <button type='button' class='btn btn-sm btn-secondary' onclick='testBinding("{b["id"]}")'>Test</button>
            <button type='button' class='btn btn-sm btn-danger icon-delete-btn' onclick='deleteBinding("{b["id"]}")' title='Delete binding' aria-label='Delete binding'>{get_icon("delete")}</button>
          </td>
        </tr>"""

    if visible_bindings:
        bindings_html = f"""
        <table><thead><tr><th>Name</th><th>Type</th><th>Scope</th><th>Status</th><th class='actions-cell'>Actions</th></tr></thead>
        <tbody>{bindings_rows}</tbody></table>"""
    else:
        bindings_html = (
            "<div class='empty'>No connector bindings yet. Create one below.</div>"
        )

    execution_rows_html = ""
    for execution in visible_executions:
        status = execution.get("result_status") or "unknown"
        if status == "success":
            badge_style = "background:rgba(80,200,120,0.15);color:var(--success)"
        elif status in ("failure", "error"):
            badge_style = "background:rgba(224,80,80,0.15);color:var(--danger)"
        else:
            badge_style = "background:rgba(107,114,128,0.15);color:var(--muted)"
        execution_rows_html += f"""
        <tr>
          <td><button type="button" class="btn btn-sm btn-secondary" onclick='viewExecutions("{execution.get("binding_id", "")}")'>{escape_html(execution.get("binding_name", ""))}</button></td>
          <td><code>{escape_html(execution.get("action", ""))}</code></td>
          <td><span class="badge" style="{badge_style}">{escape_html(status)}</span></td>
          <td>{local_dt(execution.get("executed_at"))}</td>
          <td>{escape_html(str(execution.get("error_message", ""))[:48])}</td>
        </tr>"""
    if execution_rows_html:
        executions_html = f"""
        <table>
          <thead><tr><th>Binding</th><th>Action</th><th>Status</th><th>When</th><th>Notes</th></tr></thead>
          <tbody>{execution_rows_html}</tbody>
        </table>"""
    else:
        executions_html = "<div class='empty'>No connector executions yet. Test a binding or run an action to populate this view.</div>"

    ct_cards = ""
    for ct in connector_types:
        supported_actions = ct.get("supported_actions", [])
        supported_action_names = connector_service.normalize_action_names(
            supported_actions
        )
        disabled_actions = ct.get("disabled_actions") or []
        provider_type = ct.get("provider_type") or "openapi"
        if provider_type == "mcp":
            type_chip = '<span style="display:inline-block;font-size:0.72em;font-weight:600;line-height:1;padding:3px 8px;border-radius:10px;background:#7c3aed;color:#fff;letter-spacing:0.03em;white-space:nowrap">MCP</span>'
        elif provider_type == "generic_http" or ct.get("backend_type") == "generic_http":
            type_chip = '<span style="display:inline-block;font-size:0.72em;font-weight:600;line-height:1;padding:3px 8px;border-radius:10px;background:rgba(80,200,120,0.15);color:var(--success);letter-spacing:0.03em;white-space:nowrap">HTTP</span>'
        elif provider_type == "builtin":
            type_chip = '<span style="display:inline-block;font-size:0.72em;font-weight:600;line-height:1;padding:3px 8px;border-radius:10px;background:#6b7280;color:#fff;letter-spacing:0.03em;white-space:nowrap">Built-in</span>'
        else:
            type_chip = '<span style="display:inline-block;font-size:0.72em;font-weight:600;line-height:1;padding:3px 8px;border-radius:10px;background:#2563eb;color:#fff;letter-spacing:0.03em;white-space:nowrap">API</span>'
        action_count = len(supported_action_names)
        enabled_action_count = max(
            action_count
            - len([a for a in disabled_actions if a in supported_action_names]),
            0,
        )
        view_actions_btn = (
            f'<button type="button" class="btn btn-sm btn-secondary" onclick=\'viewActions("{ct["id"]}", "{escape_html(ct["display_name"])}", {action_count})\'>View Actions</button>'
            if action_count
            else ""
        )
        binding_action_line = f"{binding_counts.get(ct['id'], 0)} binding(s)"
        if action_count:
            binding_action_line += (
                f" &middot; {enabled_action_count}/{action_count} actions"
            )
        health_state, health_n = ct_health.get(ct["id"], ("none", 0))
        if health_state == "issues":
            health_badge = f'<span class="badge badge-danger" title="Enabled bindings whose last health check failed">{health_n} issue(s)</span>'
        elif health_state == "healthy":
            health_badge = '<span class="badge badge-success" title="All enabled bindings passed their last health check">Healthy</span>'
        elif health_state == "untested":
            health_badge = f'<span class="badge badge-warning" title="Enabled bindings not yet health-checked">{health_n} untested</span>'
        else:
            health_badge = ""
        guidance = binding_guidance.get(ct["id"], {})
        recipe_line = _binding_recipe_line(guidance)
        adapter_entry = available_adapters.get(ct["id"])
        adapter_installed = bool(adapter_entry and adapter_entry.get("installed"))
        adapter_update_available = bool(
            adapter_entry and adapter_entry.get("update_available")
        )
        adapter_badges = ""
        if adapter_installed:
            adapter_badges = '<span class="badge badge-success">Installed</span>'
            if adapter_update_available:
                adapter_badges += ' <span class="badge badge-warning">Update available</span>'
        update_btn = ""
        if adapter_update_available and ctx.is_admin:
            update_btn = (
                f"<button type='button' class='btn btn-sm btn-primary' "
                f"onclick='updateAdapter(this, \"{ct['id']}\")'>Update</button>"
            )
        binding_recipe_line = (
            f'<div style="font-size:0.8em;color:var(--text-muted);margin-top:0.25rem">{escape_html(recipe_line)}</div>'
            if recipe_line
            else ""
        )
        ct_cards += f"""
        <div class='connector-type-card'>
          <div style="padding:0 0 0.5rem">
            <div class='connector-type-name' style="margin:0">{escape_html(ct["display_name"])}</div>
            <div style="font-size:0.8em;color:var(--text-muted);margin-top:0.1rem">{binding_action_line}</div>
            <div class='connector-type-desc' style="margin-top:0.35rem">{escape_html(ct.get("description", "") or "")}</div>
            {binding_recipe_line}
          </div>
          <div class='connector-type-footer' style="margin-top:auto; display:flex; flex-direction:column; gap:8px; align-items:stretch;">
            <div style="display:flex;align-items:center;gap:0.4rem;">{type_chip}{health_badge}{adapter_badges}</div>
            <div style="display:flex;gap:0.4rem;align-items:center;justify-content:flex-end;">
              {view_actions_btn}
              <button type='button' class='btn btn-sm btn-secondary' onclick='openNewBinding("{ct["id"]}", "{escape_html(ct["display_name"])}")'>Bind</button>
              {update_btn}
              <button type='button' class='btn btn-sm btn-danger icon-delete-btn' onclick='deleteConnectorType("{ct["id"]}")' title='{ "Uninstall adapter" if adapter_installed else "Delete connector type" }' aria-label='{ "Uninstall adapter" if adapter_installed else "Delete connector type" }'>{ "Uninstall" if adapter_installed else get_icon("delete") }</button>
            </div>
          </div>
        </div>"""

    body = f"""
    <div class="page-header">
      <div>
        <h1>Connectors</h1>
        <p class="text-muted" style="max-width:760px;margin-top:8px">
          Manage the service catalog agents can call, along with the credentials and bindings that
          make each capability available in the right scope.
        </p>
      </div>
      <div class="page-actions">
        <a class="btn btn-secondary" href="/credentials">+ New Credential</a>
        <a class="btn btn-secondary" href="/connectors/directory">Browse API Directory</a>
        <a class="btn btn-secondary" href="/connectors/adapters">Browse Adapters</a>
        <div class="dropdown" style="display:inline-block;position:relative">
          <button class="btn btn-secondary" onclick="toggleDropdown(this)" type="button">+ Add <span style="font-size:0.8em">&#9662;</span></button>
          <div class="dropdown-menu" style="display:none;position:absolute;right:0;top:100%;background:#fff;border:1px solid #ddd;border-radius:4px;box-shadow:0 2px 8px rgba(0,0,0,0.15);z-index:1000;min-width:200px;text-align:left">
            <button class="dropdown-item" style="display:block;width:100%;padding:10px 16px;border:none;background:none;text-align:left;cursor:pointer;font-size:14px" onclick="resetImportPreview();openModal('import-spec-modal');closeAllDropdowns()">Import API Spec</button>
            <button class="dropdown-item" style="display:block;width:100%;padding:10px 16px;border:none;background:none;text-align:left;cursor:pointer;font-size:14px" onclick="openModal('import-mcp-modal');closeAllDropdowns()">Import MCP Server</button>
            <button class="dropdown-item" style="display:block;width:100%;padding:10px 16px;border:none;background:none;text-align:left;cursor:pointer;font-size:14px" onclick="openModal('add-http-modal');closeAllDropdowns()">Add HTTP Connector</button>
          </div>
        </div>
      </div>
    </div>

    <div class="stat-grid">
      <a class="stat-card stat-link" href="#service-catalog"><div class="value">{connector_type_count}</div><div class="label">Connector Types</div></a>
      <a class="stat-card stat-link" href="#bindings"><div class="value">{visible_binding_count}</div><div class="label">Visible Bindings</div></a>
      <a class="stat-card stat-link" href="#bindings"><div class="value">{enabled_binding_count}</div><div class="label">Enabled Bindings</div></a>
      <a class="stat-card stat-link" href="#executions"><div class="value">{failed_binding_count}</div><div class="label">Bindings with Errors</div></a>
      <a class="stat-card stat-link" href="#service-catalog"><div class="value">{sum(max(len(connector_service.normalize_action_names(ct.get("supported_actions"))) - len(ct.get("disabled_actions") or []), 0) for ct in connector_types)}</div><div class="label">Enabled Actions</div></a>
    </div>

    <div class="card">
      <div class="section-header">
        <h3>Quick Start</h3>
        <div class="section-note">The simplest path for most users.</div>
      </div>
      <ol style="margin:0;padding-left:20px;line-height:1.6">
        <li>Create a credential for the service you want to use.</li>
        <li>Create a binding for that credential in the right scope.</li>
        <li>Test the binding, then ask an agent to use it through MCP.</li>
      </ol>
    </div>

     <div class="card" id="service-catalog">
      <div class="section-header" style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px">
        <div>
          <h3>Service Catalog</h3>
          <div class="section-note">Built-in connector types and imported connector types are shared across the instance.</div>
        </div>
        <button type="button" class="btn btn-sm btn-secondary" onclick="checkHealth(this)">Check Health</button>
      </div>
      <div class="connector-types-grid">{ct_cards or "<div class='empty'>No connector types yet. <a href='/connectors/directory'>Browse the API Directory</a> or import a custom spec.</div>"}</div>
    </div>

    <div class="card" id="bindings">
      <div class="section-header">
        <h3>Bindings</h3>
        <div class="section-note">How a capability becomes available inside a scope.</div>
      </div>
      <div id="bindings-list">{bindings_html}</div>
    </div>

    <div class="card" id="executions">
      <div class="section-header">
        <h3>Recent Executions</h3>
        <div class="section-note">What the service layer has actually run recently.</div>
      </div>
      <div id="executions-list">{executions_html}</div>
    </div>

    <!-- Create Binding Modal -->
    <div class="modal-overlay" id="create-binding-modal" style="display:none">
      <div class="modal">
        <h3>New Binding</h3>
        <form id="create-binding-form" onsubmit="createBinding(event)">
          <div class="form-group">
            <label>Connector Type *</label>
            <select id="binding-connector-type" required onchange="updateBindingFormContext()">
              <option value="">-- Select --</option>
              {connector_type_opts}
            </select>
          </div>
          <div class="form-group">
            <div id="binding-recipe" class="form-hint">Choose a connector type to see the required credential and config fields.</div>
          </div>
          <div class="form-group" id="binding-setup-group" style="display:none">
            <label>Setup Instructions</label>
            <div id="binding-setup" class="form-hint" style="white-space:pre-line"></div>
          </div>
          <div class="form-group">
            <label>Name *</label>
            <input type="text" id="binding-name" placeholder="e.g. Workspace API" autocomplete="off" required>
          </div>
          <div class="form-group">
            <label>Scope *</label>
            <select id="binding-scope" required>
              <option value="">Select scope...</option>
              {scope_options}
            </select>
          </div>
          <div class="form-group">
            <label>Credential</label>
            <select id="binding-credential-mode" onchange="toggleBindingCredentialMode()">
              <option value="existing">Use stored credential</option>
              <option value="new">Create new credential</option>
            </select>
          </div>
          <div id="binding-existing-credential-fields" class="form-group">
            <select id="binding-credential">
              <option value="">-- Select stored credential --</option>
              {credential_opts}
            </select>
          </div>
          <div id="binding-new-credential-fields" style="display:none">
            <div class="form-group">
              <label>Credential Name *</label>
              <input type="text" id="binding-new-credential-name" placeholder="e.g. service-token" autocomplete="off">
            </div>
            <div id="binding-new-credential-editor"></div>
          </div>
          <div class="form-group">
            <label>Config (JSON, optional)</label>
            <textarea id="binding-config" rows="2" placeholder='{{"repo": "owner/name"}}'></textarea>
            <div class="form-hint">
              Optional non-secret settings for this binding, such as <code>base_url</code>,
              <code>default_params</code>, <code>auth_header</code>, or <code>test_url</code>.
              Leave it blank if the credential and connector type are enough.
              For adapters with their own request paths, point <code>base_url</code> at the
              service root, not the final RPC endpoint.
            </div>
          </div>
          <div class="form-group">
            <label class="checkbox-label">
              <input type="checkbox" id="binding-enabled" checked> Enabled
            </label>
          </div>
          <button type="submit" class="btn btn-primary">Create Binding</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('create-binding-modal')">Cancel</button>
        </form>
      </div>
    </div>

    <!-- Edit Binding Modal -->
    <div class="modal-overlay" id="edit-binding-modal" style="display:none">
      <div class="modal">
        <h3>Edit Binding</h3>
        <form id="edit-binding-form" onsubmit="submitEditBinding(event)">
          <input type="hidden" id="edit-binding-id">
          <div class="form-group">
            <label>Name</label>
            <input type="text" id="edit-binding-name" autocomplete="off">
          </div>
          <div class="form-group">
            <label>Scope</label>
            <select id="edit-binding-scope">
              {scope_options}
            </select>
          </div>
          <div class="form-group">
            <label>Credential</label>
            <select id="edit-binding-credential">
              <option value="">-- Select --</option>
              {credential_opts}
            </select>
          </div>
          <div class="form-group">
            <label>Config (JSON)</label>
            <textarea id="edit-binding-config" rows="2"></textarea>
            <div class="form-hint">
              Optional non-secret settings for this binding, such as <code>base_url</code>,
              <code>default_params</code>, <code>auth_header</code>, or <code>test_url</code>.
              Leave it blank if the credential and connector type are enough.
              For adapters with their own request paths, point <code>base_url</code> at the
              service root, not the final RPC endpoint.
            </div>
          </div>
          <div class="form-group">
            <label class="checkbox-label">
              <input type="checkbox" id="edit-binding-enabled"> Enabled
            </label>
          </div>
          <button type="submit" class="btn btn-primary">Save Changes</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('edit-binding-modal')">Cancel</button>
        </form>
      </div>
    </div>

    <!-- Import API Spec Modal -->
    <div class="modal-overlay" id="import-spec-modal" style="display:none">
      <div class="modal">
        <h3>Import API Spec</h3>
        <form id="import-spec-form" onsubmit="importSpec(event)">
          <div class="form-group">
            <label>Spec URL</label>
            <input type="url" id="import-spec-url" placeholder="https://example.com/openapi.json">
          </div>
          <div class="form-group">
            <label>Or paste JSON / upload file</label>
            <textarea id="import-spec-json" rows="6" placeholder='Paste OpenAPI JSON here, or use the file picker below'></textarea>
          </div>
          <div class="form-group">
            <input type="file" id="import-spec-file" accept=".json,.yaml,.yml" onchange="handleSpecFile(event)">
          </div>
          <div class="form-group">
            <label>Display Name (optional)</label>
            <input type="text" id="import-spec-name" placeholder="e.g. Example REST API" autocomplete="off">
          </div>
          <div id="import-spec-preview" class="card" style="display:none;margin:12px 0 0 0"></div>
          <div class="form-hint">
            <strong>Where do I find OpenAPI specs?</strong><br>
            Many APIs publish their spec at <code>/openapi.json</code> or <code>/swagger.json</code>.<br>
            Search <a href="https://apis.guru" target="_blank">apis.guru</a> for 2000+ specs, or <a href="/connectors/help">read the guide</a>.
          </div>
          <button type="button" class="btn btn-secondary" onclick="previewSpec(event)">Preview Spec</button>
          <button type="submit" class="btn btn-primary" id="import-spec-import-btn" disabled>Create</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('import-spec-modal')">Cancel</button>
        </form>
      </div>
    </div>

    <!-- Import MCP Server Modal -->
    <div class="modal-overlay" id="import-mcp-modal" style="display:none">
      <div class="modal">
        <h3>Import MCP Server</h3>
        <div style="background:var(--bg-secondary);border-left:3px solid var(--warning-color);padding:0.6rem 0.85rem;border-radius:4px;margin-bottom:1rem;font-size:0.875em">
          <strong>HTTP transport only.</strong> {APP_NAME} connects to MCP servers over HTTP — it cannot launch local processes.
          Servers configured with <code>command</code>/<code>args</code> (stdio) are not supported here.
          Use <strong>+ Add HTTP Connector</strong> for REST APIs, or run a stdio server behind an HTTP bridge first.
        </div>
        <form id="import-mcp-form" onsubmit="importMcpServer(event)">
          <div class="form-group">
            <label>Server URL</label>
            <input type="url" id="import-mcp-url" placeholder="https://mcp.example.com/mcp" required>
          </div>
          <div class="form-group">
            <label>Display Name (optional)</label>
            <input type="text" id="import-mcp-name" placeholder="e.g. Context7 MCP" autocomplete="off">
          </div>
          <div class="form-group">
            <label>Description (optional)</label>
            <input type="text" id="import-mcp-description" placeholder="What this MCP server does" autocomplete="off">
          </div>
          <div class="form-group">
            <label>Transport</label>
            <select id="import-mcp-transport">
              <option value="streamable_http" selected>streamable_http (recommended)</option>
              <option value="http">http</option>
            </select>
          </div>
          <div class="form-group">
            <label>Timeout (ms)</label>
            <input type="number" id="import-mcp-timeout" min="1000" step="1000" value="60000">
          </div>
          <div class="form-group">
            <label>Discovery Auth (optional)</label>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.5rem">
              <input type="text" id="import-mcp-auth-header" placeholder="Header name (e.g. Authorization)" autocomplete="off">
              <input type="text" id="import-mcp-auth-value" placeholder="Value (e.g. Bearer YOUR_KEY)" autocomplete="off">
            </div>
            <div class="form-hint">
              Used only during import and refresh — not stored in the binding. For standard Bearer auth use header <code>Authorization</code> and value <code>Bearer YOUR_KEY</code>. For custom headers like Context7, use <code>CONTEXT7_API_KEY</code> and the raw key as the value.
            </div>
          </div>
          <button type="submit" class="btn btn-primary">Import MCP Server</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('import-mcp-modal')">Cancel</button>
        </form>
      </div>
    </div>

    <!-- Add HTTP Connector Modal -->
    <div class="modal-overlay" id="add-http-modal" style="display:none">
      <div class="modal">
        <h3>Add HTTP Connector</h3>
        <form id="add-http-form" onsubmit="addHttpConnector(event)">
          <div class="form-group">
            <label>Display Name</label>
            <input type="text" id="http-display-name" placeholder="e.g. OpenRouter" required autocomplete="off">
          </div>
          <div class="form-group">
            <label>Base URL</label>
            <input type="url" id="http-base-url" placeholder="https://openrouter.ai/api/v1" required>
          </div>
          <div class="form-group">
            <label>Auth Type</label>
            <select id="http-auth-type" onchange="updateHttpAuthFields()">
              <option value="bearer" selected>Bearer token (Authorization: Bearer ...)</option>
              <option value="header">Custom header</option>
              <option value="query">Query parameter</option>
              <option value="none">None</option>
            </select>
          </div>
          <div class="form-group" id="http-auth-header-group">
            <label>Auth Header Name</label>
            <input type="text" id="http-auth-header" placeholder="Authorization" autocomplete="off">
            <div class="form-hint">Leave blank to use Authorization</div>
          </div>
          <div class="form-group" id="http-auth-scheme-group">
            <label>Auth Scheme</label>
            <input type="text" id="http-auth-scheme" placeholder="Bearer" autocomplete="off">
            <div class="form-hint">Leave blank to use Bearer. Set empty to send the token value directly.</div>
          </div>
          <div class="form-group">
            <label>Extra Headers (optional, JSON)</label>
            <textarea id="http-extra-headers" rows="3" placeholder='{{"X-Custom-Header":"value"}}'></textarea>
          </div>
          <button type="submit" class="btn btn-primary">Add Connector</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('add-http-modal')">Cancel</button>
        </form>
      </div>
    </div>

    <!-- View Actions Modal -->
    <div class="modal-overlay" id="view-actions-modal" style="display:none">
      <div class="modal" style="max-width:700px">
        <h3 id="view-actions-title">Actions</h3>
        <div style="display:flex;gap:8px;margin-bottom:12px">
          <input type="text" id="view-actions-filter" placeholder="Filter actions..." oninput="filterActions()" style="flex:1" autocomplete="off">
          <button type="button" class="btn btn-sm btn-secondary" onclick="bulkSetActions(true)">Select All</button>
          <button type="button" class="btn btn-sm btn-secondary" onclick="bulkSetActions(false)">Clear All</button>
        </div>
        <div id="view-actions-content" style="max-height:400px;overflow-y:auto"></div>
        <div style="margin-top:12px;display:flex;gap:8px;justify-content:flex-end">
          <button type="button" class="btn btn-primary" onclick="saveActionSettings()">Save Changes</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('view-actions-modal')">Close</button>
        </div>
      </div>
    </div>

    <!-- Test Result Modal -->
    <div class="modal-overlay" id="test-result-modal" style="display:none">
      <div class="modal">
        <h3>Connection Test Result</h3>
        <div id="test-result-content"></div>
        <button type="button" class="btn" onclick="closeModal('test-result-modal')">Close</button>
      </div>
    </div>

    <!-- OAuth Redirect Modal -->
    <div class="modal-overlay" id="oauth-redirect-modal" style="display:none">
      <div class="modal" style="max-width:760px">
        <h3>Google OAuth Redirect URI</h3>
        <div class="form-hint" style="margin-bottom:8px">
          Add this exact URL to the Google OAuth client’s Authorized redirect URIs.
        </div>
        <div class="form-group">
          <textarea id="oauth-redirect-url" rows="3" readonly style="width:100%;font-family:monospace;white-space:pre-wrap"></textarea>
        </div>
        <div style="display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap">
          <button type="button" class="btn btn-secondary" onclick="copyOAuthRedirectUrl()">Copy URL</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('oauth-redirect-modal')">Cancel</button>
          <button type="button" class="btn btn-primary" onclick="continueOAuthAuthorization()">Continue to Google</button>
        </div>
      </div>
    </div>

    <!-- Binding Executions Modal -->
    <div class="modal-overlay" id="executions-modal" style="display:none">
      <div class="modal" style="max-width:700px">
        <h3>Execution History</h3>
        <div id="executions-content"></div>
        <button type="button" class="btn" onclick="closeModal('executions-modal')">Close</button>
      </div>
    </div>
    """

    js = """
<script>
function parseBindingGuidance() {
  const select = document.getElementById('binding-connector-type');
  if (!select || select.selectedIndex < 0) return {};
  const option = select.options[select.selectedIndex];
  const raw = option?.dataset?.guidance || '{}';
  try {
    return JSON.parse(raw) || {};
  } catch (e) {
    return {};
  }
}

function bindingPlaceholderForField(field, kind) {
  const name = String(field || '').trim();
  const lower = name.toLowerCase();

  if (kind === 'config') {
    if (lower === 'base_url' || lower === 'url' || lower.endsWith('_url')) {
      return lower === 'base_url' ? 'http://HOST:PORT' : 'https://example.com';
    }
    if (lower.includes('host')) return 'HOST';
    if (lower.includes('port')) return 'PORT';
    if (lower.includes('path')) return '/path';
    return 'your-' + name.replace(/_/g, '-');
  }

  if (lower === 'username') return 'your-username';
  if (lower.includes('password')) return 'your-password';
  if (lower.includes('token') || lower.includes('secret') || lower.includes('key')) {
    return 'YOUR_' + name.toUpperCase().replace(/[^A-Z0-9]+/g, '_');
  }
  return 'your-' + name.replace(/_/g, '-');
}

function findMatchingCredentialOption(name, scope) {
  const select = document.getElementById('binding-credential');
  if (!select) return null;
  const targetName = String(name || '').trim().toLowerCase();
  const targetScope = String(scope || '').trim();
  const options = Array.from(select.options || []);
  let exact = null;
  let nameOnly = null;
  for (const option of options) {
    if (!option.value) continue;
    const optionName = String(option.dataset?.credentialName || option.textContent || '').trim().toLowerCase();
    const optionScope = String(option.dataset?.credentialScope || '').trim();
    if (!optionName || optionName !== targetName) continue;
    if (targetScope && optionScope === targetScope) return option;
    if (!nameOnly) nameOnly = option;
    if (optionScope === targetScope) exact = option;
  }
  return exact || nameOnly;
}

function buildBindingJsonTemplate(fields, kind) {
  const out = {};
  (Array.isArray(fields) ? fields : []).forEach(function(field) {
    out[field] = bindingPlaceholderForField(field, kind);
  });
  return JSON.stringify(out, null, 2);
}

function clearBindingTemplateFlag(el) {
  if (!el || !el.dataset) return;
  el.dataset.template = 'false';
}

function prefillBindingConfig(guidance) {
  const configEl = document.getElementById('binding-config');
  if (!configEl) return;
  const fields = Array.isArray(guidance?.config_fields) ? guidance.config_fields : [];
  if (!fields.length) {
    configEl.dataset.template = 'false';
    return;
  }
  if (configEl.value.trim() && configEl.dataset.template !== 'true') return;
  configEl.value = buildBindingJsonTemplate(fields, 'config');
  configEl.dataset.template = 'true';
  if (!configEl.dataset.templateListenerAttached) {
    configEl.addEventListener('input', function() {
      clearBindingTemplateFlag(configEl);
    });
    configEl.dataset.templateListenerAttached = 'true';
  }
}

function renderBindingCredentialEditor(guidance) {
  const wrap = document.getElementById('binding-new-credential-editor');
  if (!wrap) return;
  const fields = Array.isArray(guidance?.credential_fields) ? guidance.credential_fields : [];
  if (!fields.length) {
    wrap.dataset.template = 'false';
    wrap.innerHTML = '<div class="form-hint">This connector does not declare a credential payload. Use a stored credential if one exists.</div>';
    return;
  }

  const isMulti = fields.length > 1;
  const hint = isMulti
    ? 'Store the credential as one JSON object with these fields: ' + fields.join(', ')
    : 'Store the single secret value for ' + fields[0];

  if (isMulti) {
    const template = buildBindingJsonTemplate(fields, 'credential');
    wrap.innerHTML =
      '<div class="form-group">' +
        '<label>Credential JSON *</label>' +
        '<textarea id="binding-new-credential-value" rows="5" autocomplete="off" data-template="true" placeholder=' +
          '"Replace the placeholders before saving"' +
        '></textarea>' +
      '</div>' +
      '<div class="form-hint">' + escapeHtml(hint) + ' The template is prefilled below.</div>';
    const valueEl = document.getElementById('binding-new-credential-value');
    if (valueEl) {
      valueEl.value = template;
      valueEl.addEventListener('input', function() {
        clearBindingTemplateFlag(valueEl);
      }, { once: true });
    }
    return;
  }

  const single = fields[0];
  const looksSecret = /password|secret|token|key/i.test(single);
  wrap.innerHTML =
    '<div class="form-group">' +
      '<label>Credential Value *</label>' +
      '<input type="' + (looksSecret ? 'password' : 'text') + '" id="binding-new-credential-value" autocomplete="new-password" placeholder="e.g. ' + escapeHtml(single) + '">' +
    '</div>' +
    '<div class="form-hint">' + escapeHtml(hint) + '</div>';
}

let pendingOAuthAuthorizationUrl = null;

function updateBindingFormContext(defaultName) {
  const guidance = parseBindingGuidance();
  const recipe = document.getElementById('binding-recipe');
  const setupGroup = document.getElementById('binding-setup-group');
  const setupEl = document.getElementById('binding-setup');
  const nameEl = document.getElementById('binding-name');
  const credNameEl = document.getElementById('binding-new-credential-name');

  if (recipe) {
    const bits = [];
    if (guidance?.suggested_binding_name) {
      bits.push('Suggested binding: ' + guidance.suggested_binding_name);
    }
    if (Array.isArray(guidance?.credential_fields) && guidance.credential_fields.length) {
      bits.push('Credential JSON fields: ' + guidance.credential_fields.join(', '));
    }
    if (Array.isArray(guidance?.config_fields) && guidance.config_fields.length) {
      bits.push('Config JSON fields: ' + guidance.config_fields.join(', '));
    }
    recipe.textContent = bits.length ? bits.join(' · ') : 'No extra binding requirements declared.';
  }

  if (setupGroup && setupEl) {
    const instructions = String(guidance?.setup_instructions || '').trim();
    const documentationUrl = String(guidance?.documentation_url || '').trim();
    const hasDocumentationUrl = documentationUrl.startsWith('https://');
    setupGroup.style.display = instructions || hasDocumentationUrl ? '' : 'none';
    setupEl.innerHTML = '';
    if (instructions) {
      setupEl.appendChild(document.createTextNode(instructions));
    }
    if (hasDocumentationUrl) {
      if (instructions) setupEl.appendChild(document.createTextNode('\\n'));
      const link = document.createElement('a');
      link.href = documentationUrl;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = 'Open setup documentation';
      setupEl.appendChild(link);
    }
  }

  if (nameEl && !nameEl.value) {
    nameEl.value = defaultName || guidance?.suggested_binding_name || '';
  }
  if (credNameEl && !credNameEl.value) {
    credNameEl.value = guidance?.suggested_credential_name || '';
  }

  const credentialSelect = document.getElementById('binding-credential');
  const credentialMode = document.getElementById('binding-credential-mode');
  const bindingScope = document.getElementById('binding-scope');
  if (
    credentialSelect &&
    credentialMode &&
    credentialMode.value === 'existing' &&
    bindingScope &&
    bindingScope.value &&
    guidance?.suggested_credential_name
  ) {
    const match = findMatchingCredentialOption(
      guidance.suggested_credential_name,
      bindingScope.value
    );
    if (match) {
      credentialSelect.value = match.value;
    }
  }

  renderBindingCredentialEditor(guidance);
  prefillBindingConfig(guidance);
}

async function createBinding(e) {
  e.preventDefault();
  let credentialId = document.getElementById('binding-credential').value || null;
  const credentialMode = document.getElementById('binding-credential-mode').value;
  const bindingScope = document.getElementById('binding-scope').value;
  const guidance = parseBindingGuidance();

  if (credentialMode === 'new') {
    const credentialName = document.getElementById('binding-new-credential-name').value;
    const credentialValue = document.getElementById('binding-new-credential-value').value;
    if (!bindingScope) {
      showToast('Select a scope before creating a credential', 'danger');
      return;
    }
    if (!credentialName || !credentialValue) {
      showToast('Credential name and secret value are required', 'danger');
      return;
    }
    const existingCredential = findMatchingCredentialOption(credentialName, bindingScope);
    if (existingCredential) {
      credentialId = existingCredential.value;
    } else {
      if ((guidance.credential_fields || []).length > 1) {
        let parsed = null;
        try {
          parsed = JSON.parse(credentialValue);
        } catch (err) {
          showToast('Enter valid JSON for ' + guidance.credential_fields.join(', '), 'danger');
          return;
        }
        const missing = (guidance.credential_fields || []).filter(function(field) {
          return parsed[field] === undefined || parsed[field] === null || parsed[field] === '';
        });
        if (missing.length) {
          showToast('Credential JSON must include: ' + missing.join(', '), 'danger');
          return;
        }
        const credentialEl = document.getElementById('binding-new-credential-value');
        if (credentialEl && credentialEl.dataset.template === 'true') {
          showToast('Replace the credential template values before saving', 'danger');
          return;
        }
      }
      const credentialBody = {
        name: credentialName,
        label: credentialName,
        scope: bindingScope,
        value: credentialValue,
      };
      const credentialResult = await apiFetch('/api/credentials/entries', { method: 'POST', body: JSON.stringify(credentialBody) });
      if (!credentialResult.ok) {
        showToast(credentialResult.error?.message || 'Failed to create credential', 'danger');
        return;
      }
      credentialId = credentialResult.data.entry.id;
    }
  }

  if ((guidance.credential_fields || []).length && !credentialId) {
    showToast('This adapter needs a credential for: ' + guidance.credential_fields.join(', '), 'danger');
    return;
  }

  if ((guidance.config_fields || []).length) {
    const configEl = document.getElementById('binding-config');
    let configValue = (configEl && configEl.value ? configEl.value.trim() : '');
    if (!configValue) {
      showToast('This adapter needs binding config JSON with: ' + guidance.config_fields.join(', '), 'danger');
      return;
    }
    let parsedConfig = null;
    try {
      parsedConfig = JSON.parse(configValue);
    } catch (err) {
      showToast('Binding config must be valid JSON', 'danger');
      return;
    }
    const missingConfig = (guidance.config_fields || []).filter(function(field) {
      return parsedConfig[field] === undefined || parsedConfig[field] === null || parsedConfig[field] === '';
    });
    if (missingConfig.length) {
      showToast('Binding config must include: ' + missingConfig.join(', '), 'danger');
      return;
    }
    if (configEl && configEl.dataset.template === 'true') {
      showToast('Replace the config template values before saving', 'danger');
      return;
    }
  }

  const body = {
    connector_type_id: document.getElementById('binding-connector-type').value,
    name: document.getElementById('binding-name').value,
    scope: bindingScope,
    credential_id: credentialId,
    config_json: document.getElementById('binding-config').value || null,
    enabled: document.getElementById('binding-enabled').checked,
  };
  const j = await apiFetch('/api/connector-bindings', { method: 'POST', body: JSON.stringify(body) });
  if (j.ok) {
    showToast('Binding created', 'success');
    closeModal('create-binding-modal');
    document.getElementById('create-binding-form').reset();
    toggleBindingCredentialMode();
    location.reload();
  } else {
    showToast(j.error?.message || 'Failed to create binding', 'danger');
  }
}

function toggleBindingCredentialMode() {
  const mode = document.getElementById('binding-credential-mode').value;
  document.getElementById('binding-existing-credential-fields').style.display = mode === 'existing' ? '' : 'none';
  document.getElementById('binding-new-credential-fields').style.display = mode === 'new' ? '' : 'none';
  updateBindingFormContext();
}

async function editBinding(id) {
  const j = await apiFetch('/api/connector-bindings/' + id);
  if (!j.ok) { showToast(j.error?.message || 'Error', 'danger'); return; }
  const b = j.data.binding;
  document.getElementById('edit-binding-id').value = id;
  document.getElementById('edit-binding-name').value = b.name || '';
  document.getElementById('edit-binding-scope').value = b.scope || '';
  document.getElementById('edit-binding-credential').value = b.credential_id || '';
  document.getElementById('edit-binding-config').value = b.config_json || '';
  document.getElementById('edit-binding-enabled').checked = !!b.enabled;
  openModal('edit-binding-modal');
}

async function submitEditBinding(e) {
  e.preventDefault();
  const id = document.getElementById('edit-binding-id').value;
  const body = {
    name: document.getElementById('edit-binding-name').value,
    scope: document.getElementById('edit-binding-scope').value,
    credential_id: document.getElementById('edit-binding-credential').value || null,
    config_json: document.getElementById('edit-binding-config').value || null,
    enabled: document.getElementById('edit-binding-enabled').checked,
  };
  const j = await apiFetch('/api/connector-bindings/' + id, { method: 'PUT', body: JSON.stringify(body) });
  if (j.ok) { showToast('Updated', 'success'); closeModal('edit-binding-modal'); location.reload(); }
  else { showToast(j.error?.message || 'Failed', 'danger'); }
}

async function deleteBinding(id) {
  if (!confirm('Delete this binding? This cannot be undone.')) return;
  const j = await apiFetch('/api/connector-bindings/' + id, { method: 'DELETE' });
  if (j.ok) { showToast('Deleted', 'success'); location.reload(); }
  else { showToast(j.error?.message || 'Failed', 'danger'); }
}

async function deleteConnectorType(id) {
  if (!confirm('Delete this connector type and all its bindings? This cannot be undone.')) return;
  const j = await apiFetch('/api/connector-types/' + id, { method: 'DELETE' });
  if (j.ok) { showToast('Deleted', 'success'); location.reload(); }
  else { showToast(j.error?.message || 'Failed', 'danger'); }
}

async function testBinding(id) {
  const j = await apiFetch('/api/connector-bindings/' + id + '/test', { method: 'POST' });
  if (j.ok) {
    const r = j.data.result;
    const content = document.getElementById('test-result-content');
    if (r.success) {
      content.innerHTML = '<div class="alert alert-success">Connection successful!</div>';
    } else {
      content.innerHTML = '<div class="alert alert-danger">Connection failed: ' + escapeHtml(r.error || 'Unknown error') + '</div>';
    }
    openModal('test-result-modal');
  } else {
    showToast(j.error?.message || 'Failed to test binding', 'danger');
  }
}

async function authorizeBindingOAuth(id) {
  const j = await apiFetch('/api/connector-bindings/' + id + '/oauth/start', { method: 'POST' });
  if (!j.ok) {
    showToast(j.error?.message || 'Failed to start OAuth authorization', 'danger');
    return;
  }
  const callbackUrl = j.data.callback_url;
  pendingOAuthAuthorizationUrl = j.data.authorization_url;
  const urlEl = document.getElementById('oauth-redirect-url');
  if (urlEl) urlEl.value = callbackUrl;
  openModal('oauth-redirect-modal');
}

async function copyOAuthRedirectUrl() {
  const urlEl = document.getElementById('oauth-redirect-url');
  const text = urlEl ? urlEl.value : '';
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    showToast('Redirect URI copied', 'success');
  } catch (err) {
    if (urlEl) {
      urlEl.focus();
      urlEl.select();
      document.execCommand('copy');
      showToast('Redirect URI copied', 'success');
    }
  }
}

function continueOAuthAuthorization() {
  if (!pendingOAuthAuthorizationUrl) {
    showToast('OAuth authorization URL is missing', 'danger');
    return;
  }
  closeModal('oauth-redirect-modal');
  window.location.href = pendingOAuthAuthorizationUrl;
}

(function showOAuthResult() {
  const success = getUrlParam('oauth_success');
  const error = getUrlParam('oauth_error');
  if (success) {
    showToast('OAuth authorization completed. The binding is ready to test.', 'success');
    setUrlParam('oauth_success', null);
  } else if (error) {
    showToast('OAuth authorization failed: ' + error, 'danger');
    setUrlParam('oauth_error', null);
  }
})();

function renderHealthResult(data) {
  const total = data.total || 0;
  const passed = data.passed || 0;
  const failed = data.failed || 0;
  const content = document.getElementById('test-result-content');
  if (!content) return;
  const rows = (data.results || []).map(function(r) {
    const status = r.success
      ? '<span class="badge badge-success">Passed</span>'
      : '<span class="badge badge-danger">Failed</span>';
    return '<tr><td>' + escapeHtml(r.binding_name || r.binding_id || '') + '</td><td><code>' + escapeHtml(r.scope || '') + '</code></td><td>' + status + '</td><td>' + escapeHtml(r.error || '') + '</td></tr>';
  }).join('');
  content.innerHTML =
    '<div class="' + (failed ? 'alert alert-warning' : 'alert alert-success') + '">Checked ' + total + ' binding(s): ' + passed + ' passed, ' + failed + ' failed.</div>' +
    (rows ? '<table><thead><tr><th>Binding</th><th>Scope</th><th>Status</th><th>Error</th></tr></thead><tbody>' + rows + '</tbody></table>' : '<div class="empty">No enabled bindings to check.</div>');
  openModal('test-result-modal');
}

async function checkHealth(btn) {
  const label = btn ? btn.textContent : null;
  if (btn) { btn.disabled = true; btn.textContent = 'Checking...'; }
  const j = await apiFetch('/api/connector-types/health-check', { method: 'POST', body: JSON.stringify({}) });
  if (!j.ok) {
    if (btn) { btn.disabled = false; btn.textContent = label; }
    showToast(j.error?.message || 'Health check failed', 'danger');
    return;
  }
  // Persist the result and reload so the Service Catalog health badges reflect the
  // freshly stored test state; the result modal is shown again after reload.
  sessionStorage.setItem('connectorHealthResult', JSON.stringify(j.data));
  location.reload();
}

(function showPendingHealthResult() {
  const stored = sessionStorage.getItem('connectorHealthResult');
  if (!stored) return;
  sessionStorage.removeItem('connectorHealthResult');
  try { renderHealthResult(JSON.parse(stored)); } catch (e) { /* ignore malformed */ }
})();

async function viewExecutions(id) {
  const j = await apiFetch('/api/connector-bindings/' + id + '/executions');
  if (!j.ok) { showToast(j.error?.message || 'Error', 'danger'); return; }
  const execs = j.data.executions || [];
  const rows = execs.map(function(e) {
    return '<tr><td>' + escapeHtml(e.action || '') + '</td><td>' + escapeHtml(e.result_status || '') + '</td><td>' + escapeHtml(e.executed_at || '') + '</td><td>' + escapeHtml(e.error_message || '-') + '</td></tr>';
  }).join('');
  document.getElementById('executions-content').innerHTML = execs.length
    ? '<table><thead><tr><th>Action</th><th>Status</th><th>When</th><th>Error</th></tr></thead><tbody>' + rows + '</tbody></table>'
    : '<em>No executions yet.</em>';
  openModal('executions-modal');
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, function(c) {
    return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
  });
}

function openNewBinding(typeId, defaultName) {
  const el = document.getElementById('binding-connector-type');
  if (el) {
    el.value = typeId;
  }
  updateBindingFormContext(defaultName);
  const guidance = parseBindingGuidance();
  const credentialMode = document.getElementById('binding-credential-mode');
  if (credentialMode && Array.isArray(guidance?.credential_fields) && guidance.credential_fields.length > 1) {
    credentialMode.value = 'new';
  }
  toggleBindingCredentialMode();
  openModal('create-binding-modal');
}

let importSpecPreviewState = null;

function resetImportPreview() {
  importSpecPreviewState = null;
  const preview = document.getElementById('import-spec-preview');
  const importBtn = document.getElementById('import-spec-import-btn');
  if (preview) {
    preview.style.display = 'none';
    preview.innerHTML = '';
  }
  if (importBtn) {
    importBtn.disabled = true;
  }
}

function handleSpecFile(e) {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = function(ev) {
    document.getElementById('import-spec-json').value = ev.target.result;
    resetImportPreview();
    showToast('File loaded', 'success');
  };
  reader.readAsText(file);
}

['import-spec-url', 'import-spec-json', 'import-spec-name'].forEach(function(id) {
  const el = document.getElementById(id);
  if (el) {
    el.addEventListener('input', resetImportPreview);
    el.addEventListener('change', resetImportPreview);
  }
});

async function previewSpec(e) {
  if (e) e.preventDefault();
  const url = document.getElementById('import-spec-url').value.trim();
  const specJson = document.getElementById('import-spec-json').value.trim();
  const displayName = document.getElementById('import-spec-name').value.trim();

  if (!url && !specJson) {
    showToast('Provide a URL or paste/upload a spec', 'danger');
    return;
  }

  const body = {};
  if (url) body.url = url;
  if (specJson) body.spec_json = specJson;
  if (displayName) body.display_name = displayName;

  const j = await apiFetch('/api/connector-types/preview', { method: 'POST', body: JSON.stringify(body) });
  if (!j.ok) {
    showToast(j.error?.message || 'Validation failed', 'danger');
    return;
  }

  const preview = j.data.preview || {};
  importSpecPreviewState = preview;
  const previewEl = document.getElementById('import-spec-preview');
  const importBtn = document.getElementById('import-spec-import-btn');
  if (previewEl) {
    const servers = (preview.servers || []).slice(0, 3).map(escapeHtml).join('<br>');
    const warnings = (preview.warnings || []).map(function(w) {
      return '<li>' + escapeHtml(w) + '</li>';
    }).join('');
    const actions = (preview.supported_actions || []).slice(0, 8).map(function(a) {
      return '<span class="badge" style="margin:0 6px 6px 0;display:inline-block">' + escapeHtml(a) + '</span>';
    }).join('');
    previewEl.innerHTML =
      '<h4 style="margin-top:0">Preview</h4>' +
      '<table style="width:100%">' +
        '<tr><td style="padding:4px 8px 4px 0;color:var(--muted)">Name</td><td>' + escapeHtml(preview.display_name || preview.connector_type_id || 'API') + '</td></tr>' +
        '<tr><td style="padding:4px 8px 4px 0;color:var(--muted)">Connector ID</td><td><code>' + escapeHtml(preview.connector_type_id || '-') + '</code></td></tr>' +
        '<tr><td style="padding:4px 8px 4px 0;color:var(--muted)">Auth</td><td>' + escapeHtml(preview.auth_type || 'none') + '</td></tr>' +
        '<tr><td style="padding:4px 8px 4px 0;color:var(--muted)">Servers</td><td style="word-break:break-word">' + (servers || '<em>none</em>') + '</td></tr>' +
        '<tr><td style="padding:4px 8px 4px 0;color:var(--muted)">Actions</td><td>' + escapeHtml(String(preview.operation_count || 0)) + '</td></tr>' +
      '</table>' +
      (actions ? '<div style="margin-top:10px">' + actions + '</div>' : '') +
      (warnings ? '<div style="margin-top:10px"><strong>Warnings</strong><ul style="margin:6px 0 0 18px">' + warnings + '</ul></div>' : '');
    previewEl.style.display = '';
  }
  if (importBtn) {
    importBtn.disabled = false;
  }
  const previewName = preview.display_name || preview.connector_type_id || 'API';
  const previewCount = preview.operation_count != null ? preview.operation_count : 0;
  if (previewEl && previewEl.scrollIntoView) {
    previewEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  showToast('Validated ' + previewName + ' (' + previewCount + ' actions)', 'success');
}

async function importSpec(e) {
  if (e) e.preventDefault();
  if (!importSpecPreviewState) {
    showToast('Validate the spec before creating it', 'danger');
    return;
  }
  const url = document.getElementById('import-spec-url').value.trim();
  const specJson = document.getElementById('import-spec-json').value.trim();
  const displayName = document.getElementById('import-spec-name').value.trim();

  if (!url && !specJson) {
    showToast('Provide a URL or paste/upload a spec', 'danger');
    return;
  }

  const body = {};
  if (url) body.url = url;
  if (specJson) body.spec_json = specJson;
  if (displayName) body.display_name = displayName;

  const j = await apiFetch('/api/connector-types/import', { method: 'POST', body: JSON.stringify(body) });
  if (!j.ok) {
    showToast(j.error?.message || 'Import failed', 'danger');
    return;
  }

  const ct = j.data.connector_type || {};
  const actionCount = j.data.operation_count || (ct.supported_actions || []).length;
  showToast('Imported ' + (ct.display_name || 'API') + ' (' + actionCount + ' actions)', 'success');
  closeModal('import-spec-modal');
  document.getElementById('import-spec-form').reset();
  resetImportPreview();
  location.reload();
}

async function importMcpServer(e) {
  if (e) e.preventDefault();
  const prefix = document.getElementById('directory-import-mcp-url') ? 'directory-' : '';
  const url = document.getElementById(prefix + 'import-mcp-url').value.trim();
  const displayName = document.getElementById(prefix + 'import-mcp-name').value.trim();
  const transportType = document.getElementById(prefix + 'import-mcp-transport').value || 'streamable_http';
  const timeoutMs = parseInt(document.getElementById(prefix + 'import-mcp-timeout').value || '60000', 10);
  const authHeader = document.getElementById(prefix + 'import-mcp-auth-header')?.value.trim();
  const authValue = document.getElementById(prefix + 'import-mcp-auth-value')?.value.trim();
  const description = document.getElementById(prefix + 'import-mcp-description')?.value.trim();

  if (!url) {
    showToast('Provide an MCP server URL', 'danger');
    return;
  }

  const body = { url, transport_type: transportType, timeout_ms: timeoutMs };
  if (displayName) body.display_name = displayName;
  if (description) body.description = description;
  if (authHeader && authValue) body.headers_json = JSON.stringify({[authHeader]: authValue});

  const j = await apiFetch('/api/connector-types/import-mcp', { method: 'POST', body: JSON.stringify(body) });
  if (!j.ok) {
    showToast(j.error?.message || 'MCP import failed', 'danger');
    return;
  }

  closeModal(prefix + 'import-mcp-modal');
  showToast('Imported ' + (j.data.connector_type?.display_name || 'MCP server') + ' (' + j.data.tool_count + ' tools)', 'success');
  document.getElementById(prefix + 'import-mcp-form').reset();
  location.reload();
}

function updateHttpAuthFields() {
  const authType = document.getElementById('http-auth-type').value;
  const headerGroup = document.getElementById('http-auth-header-group');
  const schemeGroup = document.getElementById('http-auth-scheme-group');
  const headerLabel = headerGroup.querySelector('label');
  const headerHint = headerGroup.querySelector('.form-hint');
  if (authType === 'none') {
    headerGroup.style.display = 'none';
    schemeGroup.style.display = 'none';
  } else if (authType === 'query') {
    headerGroup.style.display = '';
    schemeGroup.style.display = 'none';
    headerLabel.textContent = 'Query Parameter Name';
    headerHint.textContent = 'Leave blank to use api_key';
    document.getElementById('http-auth-header').placeholder = 'api_key';
  } else {
    headerGroup.style.display = '';
    schemeGroup.style.display = authType === 'bearer' ? '' : 'none';
    headerLabel.textContent = 'Auth Header Name';
    headerHint.textContent = 'Leave blank to use Authorization';
    document.getElementById('http-auth-header').placeholder = 'Authorization';
  }
}

async function addHttpConnector(e) {
  e.preventDefault();
  const displayName = document.getElementById('http-display-name').value.trim();
  const baseUrl = document.getElementById('http-base-url').value.trim();
  const authType = document.getElementById('http-auth-type').value;
  const authHeader = document.getElementById('http-auth-header').value.trim();
  const authScheme = document.getElementById('http-auth-scheme').value.trim();
  const headersJson = document.getElementById('http-extra-headers').value.trim();

  if (!displayName || !baseUrl) {
    showToast('Display name and base URL are required', 'danger');
    return;
  }

  const body = { display_name: displayName, base_url: baseUrl, auth_type: authType };
  if (authHeader) body.auth_header = authHeader;
  if (authScheme) body.auth_scheme = authScheme;
  if (headersJson) body.headers_json = headersJson;

  const j = await apiFetch('/api/connector-types/create-http', { method: 'POST', body: JSON.stringify(body) });
  if (!j.ok) {
    showToast(j.error?.message || 'Failed to create connector', 'danger');
    return;
  }

  closeModal('add-http-modal');
  showToast('Created ' + (j.data.connector_type?.display_name || displayName), 'success');
  document.getElementById('add-http-form').reset();
  updateHttpAuthFields();
  location.reload();
}

function setAdapterButtonState(btn, label) {
  if (!btn) return;
  if (!btn.dataset.originalLabel) {
    btn.dataset.originalLabel = btn.textContent || '';
  }
  btn.disabled = label !== null;
  btn.textContent = label === null ? (btn.dataset.originalLabel || btn.textContent || '') : label;
}

async function installAdapter(btn, adapterId) {
  setAdapterButtonState(btn, 'Installing...');
  const j = await apiFetch('/api/connector-types/adapters/' + adapterId + '/install', {
    method: 'POST',
    body: JSON.stringify({})
  });
  if (!j.ok) {
    setAdapterButtonState(btn, null);
    showToast(j.error?.message || 'Failed to install adapter', 'danger');
    return;
  }
  showToast('Installed ' + (j.data.adapter?.connector_type?.display_name || adapterId), 'success');
  location.reload();
}

async function updateAdapter(btn, adapterId) {
  setAdapterButtonState(btn, 'Updating...');
  const j = await apiFetch('/api/connector-types/adapters/' + adapterId + '/update', {
    method: 'POST',
    body: JSON.stringify({})
  });
  if (!j.ok) {
    setAdapterButtonState(btn, null);
    showToast(j.error?.message || 'Failed to update adapter', 'danger');
    return;
  }
  showToast('Updated ' + (j.data.adapter?.connector_type?.display_name || adapterId), 'success');
  location.reload();
}

async function uninstallAdapter(btn, adapterId) {
  setAdapterButtonState(btn, 'Uninstalling...');
  const j = await apiFetch('/api/connector-types/adapters/' + adapterId + '/install', {
    method: 'DELETE'
  });
  if (!j.ok) {
    setAdapterButtonState(btn, null);
    showToast(j.error?.message || 'Failed to uninstall adapter', 'danger');
    return;
  }
  showToast('Uninstalled ' + adapterId, 'success');
  location.reload();
}

let actionsState = { ctId: null, offset: 0, all: [] };

async function viewActions(ctId, displayName, totalCount) {
  actionsState = { ctId: ctId, offset: 0, all: [] };
  const title = document.getElementById('view-actions-title');
  if (title) {
    title.dataset.baseTitle = displayName + ' \u2014 ' + totalCount + ' Actions';
    title.textContent = title.dataset.baseTitle;
  }
  document.getElementById('view-actions-filter').value = '';
  document.getElementById('view-actions-content').innerHTML = '<em>Loading...</em>';
  openModal('view-actions-modal');
  await loadActionsBatch(ctId, totalCount);
}

async function loadActionsBatch(ctId, totalCount) {
  const j = await apiFetch('/api/connector-types/' + ctId + '/tools?include_disabled=1&limit=1000');
  if (!j.ok) {
    document.getElementById('view-actions-content').innerHTML = '<em>Could not load actions</em>';
    return;
  }
  actionsState.all = j.data.tools || [];
  const enabledCount = actionsState.all.filter(function(t) { return t.enabled; }).length;
  const title = document.getElementById('view-actions-title');
  if (title) {
    const baseTitle = title.dataset.baseTitle || title.textContent;
    title.textContent = baseTitle + ' (' + enabledCount + ' enabled)';
  }
  renderActions();
}

function renderActions() {
  const filter = (document.getElementById('view-actions-filter').value || '').toLowerCase();
  const filtered = actionsState.all.filter(function(t) {
    if (!filter) return true;
    return t.name.toLowerCase().includes(filter) ||
           (t.description || '').toLowerCase().includes(filter) ||
           (t.path || '').toLowerCase().includes(filter);
  });
  const html = filtered.length ? (
    '<table style="width:100%">' +
      '<thead><tr><th style="width:72px">Enable</th><th>Action</th><th>Details</th></tr></thead>' +
      '<tbody>' + filtered.map(function(t) {
        return '<tr>' +
          '<td><label class="checkbox-label" style="margin:0"><input type="checkbox" ' +
          'data-action="' + encodeURIComponent(t.action) + '" ' +
          (t.enabled ? 'checked ' : '') +
          'onchange="toggleActionEnabled(decodeURIComponent(this.dataset.action), this.checked)"></label></td>' +
          '<td><strong style="font-size:0.9em">' + escapeHtml(t.name) + '</strong></td>' +
          '<td>' +
            (t.method ? '<span class="badge" style="font-size:0.75em;margin-right:6px">' + escapeHtml(t.method) + '</span>' : '') +
            (t.path ? '<code style="font-size:0.8em">' + escapeHtml(t.path) + '</code>' : '') +
            (t.auth_summary ? '<div style="font-size:0.8em;color:var(--muted);margin-top:4px">Auth: ' + escapeHtml(t.auth_summary) + '</div>' : '') +
            (t.description ? '<div style="font-size:0.85em;color:var(--muted);margin-top:4px">' + escapeHtml(t.description) + '</div>' : '') +
            (!t.enabled ? '<div class="text-muted" style="font-size:0.8em;margin-top:4px">Disabled</div>' : '') +
          '</td>' +
        '</tr>';
      }).join('') + '</tbody>' +
    '</table>'
  ) : '<em>No actions found</em>';
  document.getElementById('view-actions-content').innerHTML = html;
}

function filterActions() {
  renderActions();
}

function bulkSetActions(enabled) {
  const filter = (document.getElementById('view-actions-filter').value || '').toLowerCase();
  actionsState.all.forEach(function(t) {
    if (!filter || 
        t.name.toLowerCase().includes(filter) || 
        (t.description || '').toLowerCase().includes(filter) || 
        (t.path || '').toLowerCase().includes(filter)) {
      t.enabled = enabled;
    }
  });
  renderActions();
}

function toggleActionEnabled(actionId, enabled) {
  const item = actionsState.all.find(function(t) { return t.action === actionId; });
  if (item) {
    item.enabled = enabled;
  }
}

async function saveActionSettings() {
  if (!actionsState.ctId) return;
  const disabledActions = actionsState.all
    .filter(function(t) { return !t.enabled; })
    .map(function(t) { return t.action; });
  const j = await apiFetch('/api/connector-types/' + actionsState.ctId + '/actions', {
    method: 'PUT',
    body: JSON.stringify({ disabled_actions: disabledActions }),
  });
  if (j.ok) {
    showToast('Action settings saved', 'success');
    closeModal('view-actions-modal');
    location.reload();
  } else {
    showToast(j.error?.message || 'Failed to save actions', 'danger');
  }
}

window.onAgentCoreEvent = function(event) {
  if (event.type !== 'connector_executed') return;
  var header = document.querySelector('#executions .section-header h3');
  if (!header || document.getElementById('executions-live-dot')) return;
  var dot = document.createElement('span');
  dot.id = 'executions-live-dot';
  dot.title = 'New execution recorded';
  dot.style.cssText = 'display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--accent,#4f8ef7);margin-left:8px;vertical-align:middle';
  header.appendChild(dot);
  setTimeout(function() {
    var el = document.getElementById('executions-live-dot');
    if (el) el.remove();
  }, 4000);
};

</script>"""
    js = js.replace("window.onAgentCoreEvent", "window." + JS_WINDOW_EVENT)

    return render_page("Connectors", body, "/connectors", js, session=session)


@router.get("/connectors/adapters")
async def connectors_adapters_page(
    request: Request,
    session: dict = Depends(require_auth),
):
    ctx = build_user_context(session)
    adapter_entries = adapter_loader.list_available_adapters()
    adapter_cards = _render_adapter_cards(adapter_entries, ctx)

    body = f"""
    <div class="page-header">
      <div>
        <h1>Browse Adapters</h1>
        <p class="text-muted" style="max-width:760px;margin-top:8px">
          Built-in adapter templates and user-local adapter folders available to install into the
          service catalog.
        </p>
      </div>
      <div class="page-actions">
        <a class="btn btn-secondary" href="/connectors">&larr; Back to Connectors</a>
      </div>
    </div>

    <div class="card">
      <div class="section-header" style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px">
        <div>
          <h3>Browse Adapters</h3>
          <div class="section-note">Install one into the service catalog, then bind it like any other connector.</div>
        </div>
        <div class="section-note">{len(adapter_entries)} available</div>
      </div>
      <div class="directory-controls" style="margin-bottom:12px">
        <input type="text" id="adapter-search" placeholder="Search by name, description, or source..." class="dir-search-input" />
      </div>
      <div id="adapter-grid" class="connector-types-grid">{adapter_cards or "<div class='empty'>No adapters available yet.</div>"}</div>
    </div>
    """

    js = """
<script>
function setAdapterButtonState(btn, label) {
  if (!btn) return;
  if (!btn.dataset.originalLabel) {
    btn.dataset.originalLabel = btn.textContent || '';
  }
  btn.disabled = label !== null;
  btn.textContent = label === null ? (btn.dataset.originalLabel || btn.textContent || '') : label;
}

async function installAdapter(btn, adapterId) {
  setAdapterButtonState(btn, 'Installing...');
  const j = await apiFetch('/api/connector-types/adapters/' + adapterId + '/install', {
    method: 'POST',
    body: JSON.stringify({})
  });
  if (!j.ok) {
    setAdapterButtonState(btn, null);
    showToast(j.error?.message || 'Failed to install adapter', 'danger');
    return;
  }
  showToast('Installed ' + (j.data.adapter?.connector_type?.display_name || adapterId), 'success');
  location.reload();
}

async function updateAdapter(btn, adapterId) {
  setAdapterButtonState(btn, 'Updating...');
  const j = await apiFetch('/api/connector-types/adapters/' + adapterId + '/update', {
    method: 'POST',
    body: JSON.stringify({})
  });
  if (!j.ok) {
    setAdapterButtonState(btn, null);
    showToast(j.error?.message || 'Failed to update adapter', 'danger');
    return;
  }
  showToast('Updated ' + (j.data.adapter?.connector_type?.display_name || adapterId), 'success');
  location.reload();
}

async function uninstallAdapter(btn, adapterId) {
  setAdapterButtonState(btn, 'Uninstalling...');
  const j = await apiFetch('/api/connector-types/adapters/' + adapterId + '/install', {
    method: 'DELETE'
  });
  if (!j.ok) {
    setAdapterButtonState(btn, null);
    showToast(j.error?.message || 'Failed to uninstall adapter', 'danger');
    return;
  }
  showToast('Uninstalled ' + adapterId, 'success');
  location.reload();
}

function filterAdapters() {
  const q = (document.getElementById('adapter-search').value || '').trim().toLowerCase();
  document.querySelectorAll('[data-adapter-card]').forEach(function(card) {
    const hay = (card.dataset.searchText || '').toLowerCase();
    card.style.display = !q || hay.includes(q) ? '' : 'none';
  });
}

document.getElementById('adapter-search').addEventListener('input', filterAdapters);
</script>"""

    body = body.replace("Agent Core", APP_NAME)
    return render_page("Browse Adapters", body, "/connectors", js, session=session)


@router.get("/credentials")
async def credentials_page(request: Request, session: dict = Depends(require_auth)):
    ctx = build_user_context(session)
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )

    credential_entries = [
        e
        for e in (credential_service.list_credentials(limit=500) or [])
        if enforcer.can_read(e.get("scope", ""))
    ]

    workspaces = (
        workspace_service.list_workspaces()
        if ctx.is_admin
        else workspace_service.list_workspaces(owner_user_id=ctx.user_id)
    )
    agents = list_agents() if ctx.is_admin else list_agents(owner_user_id=ctx.user_id)

    user_scope = f"user:{session['user_id']}"
    workspace_scope_opts = "".join(
        f'<option value="workspace:{p["id"]}">workspace:{p["id"]}</option>'
        for p in workspaces
    )
    agent_scope_opts = "".join(
        f'<option value="agent:{a["id"]}">agent:{a["id"]}</option>'
        for a in agents
        if a.get("is_active")
    )
    scope_options = (
        f'<option value="{user_scope}">{user_scope}</option>\n'
        f"{workspace_scope_opts}\n"
        f"{agent_scope_opts}"
    )

    credential_rows = ""
    for e in credential_entries:
        credential_rows += f"""
        <tr data-credential-id="{e["id"]}">
          <td>{escape_html(e.get("name", ""))}</td>
          <td><code>{escape_html(e.get("scope", ""))}</code></td>
          <td><code>{escape_html(e.get("reference_name", ""))}</code></td>
          <td class='actions-cell'>
            <button type='button' class='btn btn-sm btn-secondary' onclick='editCredential("{e["id"]}")'>Edit</button>
            <button type='button' class='btn btn-sm btn-danger icon-delete-btn' onclick='deleteCredential("{e["id"]}")' title='Delete credential' aria-label='Delete credential'>{get_icon("delete")}</button>
          </td>
        </tr>"""

    if credential_entries:
        credentials_html = f"""
        <table><thead><tr><th>Name</th><th>Scope</th><th>Reference</th><th class='actions-cell'>Actions</th></tr></thead>
        <tbody>{credential_rows}</tbody></table>"""
    else:
        credentials_html = "<div class='empty'>No credentials yet.</div>"

    body = f"""
    <div class="page-header">
      <div>
        <h1>Credentials</h1>
        <p class="text-muted" style="max-width:640px;margin-top:8px">
          Encrypted secrets used by connector bindings and agents. Credentials are scoped and never returned raw via the API.
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" onclick="openModal('create-credential-modal')">+ New Credential</button>
      </div>
    </div>

    <div class="card">
      <div id="credentials-list">{credentials_html}</div>
    </div>

    <!-- Create Credential Modal -->
    <div class="modal-overlay" id="create-credential-modal" style="display:none">
      <div class="modal">
        <h3>New Credential</h3>
        <form id="create-credential-form" onsubmit="createCredential(event)">
          <div class="form-group">
            <label>Name *</label>
            <input type="text" id="credential-name" placeholder="e.g. service-token" autocomplete="off" required>
          </div>
          <div class="form-group">
            <label>Label</label>
            <input type="text" id="credential-label" placeholder="e.g. Service token" autocomplete="off">
          </div>
          <div class="form-group">
            <label>Scope *</label>
            <select id="credential-scope" required>
              <option value="">Select scope...</option>
              {scope_options}
            </select>
          </div>
          <div class="form-group">
            <label>Secret Value *</label>
            <input type="password" id="credential-value" autocomplete="new-password" required>
          </div>
          <button type="submit" class="btn btn-primary">Create Credential</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('create-credential-modal')">Cancel</button>
        </form>
      </div>
    </div>

    <!-- Edit Credential Modal -->
    <div class="modal-overlay" id="edit-credential-modal" style="display:none">
      <div class="modal">
        <h3>Edit Credential</h3>
        <form id="edit-credential-form" onsubmit="submitEditCredential(event)">
          <input type="hidden" id="edit-credential-id">
          <div class="form-group">
            <label>Name *</label>
            <input type="text" id="edit-credential-name" autocomplete="off" required>
          </div>
          <div class="form-group">
            <label>Label</label>
            <input type="text" id="edit-credential-label" autocomplete="off">
          </div>
          <div class="form-group">
            <label>Scope</label>
            <input type="text" id="edit-credential-scope" autocomplete="off" disabled>
          </div>
          <div class="form-group">
            <label>Replace Secret Value</label>
            <input type="password" id="edit-credential-value" autocomplete="new-password" placeholder="Leave blank to keep current value">
          </div>
          <button type="submit" class="btn btn-primary">Save Credential</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('edit-credential-modal')">Cancel</button>
        </form>
      </div>
    </div>
    """

    js = """
<script>
async function createCredential(e) {
  e.preventDefault();
  const body = {
    name: document.getElementById('credential-name').value,
    label: document.getElementById('credential-label').value || null,
    scope: document.getElementById('credential-scope').value,
    value: document.getElementById('credential-value').value,
  };
  const j = await apiFetch('/api/credentials/entries', { method: 'POST', body: JSON.stringify(body) });
  if (j.ok) {
    showToast('Credential created', 'success');
    closeModal('create-credential-modal');
    document.getElementById('create-credential-form').reset();
    location.reload();
  } else {
    showToast(j.error?.message || 'Failed to create credential', 'danger');
  }
}

async function deleteCredential(id) {
  if (!confirm('Delete this credential? Connector bindings using it will stop working.')) return;
  const j = await apiFetch('/api/credentials/entries/' + id, { method: 'DELETE' });
  if (j.ok) { showToast('Credential deleted', 'success'); location.reload(); }
  else { showToast(j.error?.message || 'Failed to delete credential', 'danger'); }
}

async function editCredential(id) {
  const j = await apiFetch('/api/credentials/entries/' + id);
  if (!j.ok) { showToast(j.error?.message || 'Error', 'danger'); return; }
  const c = j.data.entry;
  document.getElementById('edit-credential-id').value = id;
  document.getElementById('edit-credential-name').value = c.name || '';
  document.getElementById('edit-credential-label').value = c.label || '';
  document.getElementById('edit-credential-scope').value = c.scope || '';
  document.getElementById('edit-credential-value').value = '';
  openModal('edit-credential-modal');
}

async function submitEditCredential(e) {
  e.preventDefault();
  const id = document.getElementById('edit-credential-id').value;
  const replacementValue = document.getElementById('edit-credential-value').value;
  const body = {
    name: document.getElementById('edit-credential-name').value,
    label: document.getElementById('edit-credential-label').value || null,
  };
  if (replacementValue) body.value = replacementValue;
  const j = await apiFetch('/api/credentials/entries/' + id, { method: 'PUT', body: JSON.stringify(body) });
  if (j.ok) {
    showToast('Credential updated', 'success');
    closeModal('edit-credential-modal');
    document.getElementById('edit-credential-form').reset();
    location.reload();
  } else {
    showToast(j.error?.message || 'Failed to update credential', 'danger');
  }
}
</script>"""

    return render_page("Credentials", body, "/credentials", js, session=session)


@router.get("/connectors/help")
async def connectors_help_page(request: Request, session: dict = Depends(require_auth)):
    body = """
    <div class="page-header"><h1>Connector Help</h1></div>

    <div class="card">
      <h3>What is an OpenAPI Spec?</h3>
      <p>
        An OpenAPI (formerly Swagger) spec is a machine-readable JSON or YAML file that describes every
        endpoint, parameter, authentication method, and response format for a REST API. When you import
        one into Agent Core, the system automatically discovers all available actions and generates tool
        definitions that AI agents can use directly.
      </p>
    </div>

    <div class="card">
      <h3>How to Find a Spec</h3>
      <p>Many APIs publish their OpenAPI spec at a standard URL. Try these approaches:</p>
      <table>
        <thead><tr><th>Method</th><th>What to Try</th></tr></thead>
        <tbody>
          <tr><td>Common paths</td><td>Append <code>/openapi.json</code>, <code>/swagger.json</code>, or <code>/api-docs</code> to the API's base URL</td></tr>
          <tr><td>API documentation</td><td>Look for a "Download OpenAPI spec" or "Export" link in the docs</td></tr>
          <tr><td>GitHub repositories</td><td>Many vendors publish specs in public GitHub orgs or release repos. Search for the service name plus <code>openapi</code> or <code>swagger</code>.</td></tr>
          <tr><td>apis.guru</td><td>Search <a href="https://apis.guru" target="_blank">apis.guru</a> for 2,000+ public specs</td></tr>
        </tbody>
      </table>
    </div>

    <div class="card">
      <h3>After Importing</h3>
      <p>Once you import a spec, the workflow is:</p>
      <ol>
        <li><strong>Create a credential</strong> — store your API key or token (PAT, bearer token, etc.)</li>
        <li><strong>Create a binding</strong> — link the connector type to your credential and choose a scope</li>
        <li><strong>Test the connection</strong> — click Test to verify the credential works</li>
        <li><strong>Use it</strong> — AI agents discover available actions via MCP and execute them through <code>connectors_run</code></li>
      </ol>
    </div>

    <div class="card">
      <h3>Supported Spec Formats</h3>
      <ul>
        <li><strong>OpenAPI 3.0+</strong> (JSON or YAML)</li>
        <li><strong>Swagger 2.0</strong> (JSON or YAML)</li>
      </ul>
      <p>The spec must define at least one <code>server</code> URL with a full hostname (e.g. <code>https://api.example.com</code>). Relative-only server URLs are not supported.</p>
      <p>Specs larger than 5MB cannot be imported via URL. For large specs, download the file locally, trim it if needed, and upload it via the file picker.</p>
    </div>

    <div class="card">
      <h3>Troubleshooting</h3>
      <table>
        <thead><tr><th>Problem</th><th>Solution</th></tr></thead>
        <tbody>
          <tr><td>"Spec too large"</td><td>Download the spec file, remove unused endpoints, and upload via file picker</td></tr>
          <tr><td>"No server URL"</td><td>The spec must define a <code>servers</code> array with at least one full URL</td></tr>
          <tr><td>"Unrecognized spec format"</td><td>Ensure the file is valid JSON or YAML with an <code>openapi</code> or <code>swagger</code> version field</td></tr>
          <tr><td>Connection test fails</td><td>Check that your credential (API key/token) is valid and has the right permissions</td></tr>
          <tr><td>Action fails with HTTP error</td><td>Required path/query parameters may be missing; check the action's input schema in View Actions</td></tr>
        </tbody>
      </table>
    </div>

    <div style="margin-top:16px">
      <a href="/connectors" class="btn btn-secondary">Back to Connectors</a>
    </div>
    """
    body = body.replace("Agent Core", APP_NAME)
    return render_page("Connector Help", body, "/connectors", session=session)


@router.get("/connectors/directory")
async def connectors_directory_page(
    request: Request,
    session: dict = Depends(require_auth),
):
    body = """
    <div class="page-header">
      <h1>API Directory</h1>
        <div class="page-actions">
        <a class="btn btn-secondary" href="/connectors">&larr; Back to Connectors</a>
        <div class="dropdown" style="display:inline-block;position:relative">
          <button class="btn btn-secondary" onclick="toggleDropdown(this)" type="button">+ Add <span style="font-size:0.8em">&#9662;</span></button>
          <div class="dropdown-menu" style="display:none;position:absolute;right:0;top:100%;background:#fff;border:1px solid #ddd;border-radius:4px;box-shadow:0 2px 8px rgba(0,0,0,0.15);z-index:1000;min-width:200px;text-align:left">
            <button class="dropdown-item" style="display:block;width:100%;padding:10px 16px;border:none;background:none;text-align:left;cursor:pointer;font-size:14px" onclick="resetImportPreview();openModal('import-spec-modal');closeAllDropdowns()">Import API Spec</button>
            <button class="dropdown-item" style="display:block;width:100%;padding:10px 16px;border:none;background:none;text-align:left;cursor:pointer;font-size:14px" onclick="openModal('directory-import-mcp-modal');closeAllDropdowns()">Import MCP Server</button>
          </div>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="section-header">
        <h3>Browse 2,500+ OpenAPI Specs</h3>
        <div class="section-note">Powered by <a href="https://apis.guru" target="_blank">apis.guru</a>. Search by name or provider, filter by category, and import with one click.</div>
      </div>
      <div class="directory-controls">
        <input type="text" id="dir-search" placeholder="Search by name, description, or provider..." class="dir-search-input" />
        <select id="dir-category" class="dir-category-select"><option value="">All categories</option></select>
      </div>
      <div id="directory-grid" class="connector-types-grid"><em>Loading directory...</em></div>
      <div id="directory-pagination" class="directory-pagination"></div>
    </div>

    <div class="modal-overlay" id="import-spec-modal" style="display:none">
      <div class="modal">
        <h3>Import API Spec</h3>
        <div class="form-group">
          <label>Spec URL</label>
          <input type="url" id="import-spec-url" placeholder="https://example.com/openapi.json" />
        </div>
        <div class="form-group">
          <label>Or paste JSON/YAML</label>
          <textarea id="import-spec-json" rows="4" placeholder='{"openapi":"3.0.0",...}'></textarea>
        </div>
        <div class="form-group">
          <label>Display Name (optional)</label>
          <input type="text" id="import-spec-name" />
        </div>
        <div id="import-spec-preview" class="card" style="display:none;margin:12px 0 0 0"></div>
        <div class="modal-actions">
          <button class="btn btn-secondary" type="button" onclick="previewSpec(event)">Preview Spec</button>
          <button class="btn" id="import-spec-import-btn" type="button" onclick="importSpec(event)" disabled>Create</button>
          <button class="btn btn-secondary" onclick="closeModal('import-spec-modal')">Cancel</button>
          <a href="/connectors/help" target="_blank" class="help-link">Where do I find specs?</a>
        </div>
      </div>
    </div>

    <div class="modal-overlay" id="directory-import-mcp-modal" style="display:none">
      <div class="modal">
        <h3>Import MCP Server</h3>
        <form id="directory-import-mcp-form" onsubmit="importMcpServer(event)">
          <div class="form-group">
            <label>Server URL</label>
            <input type="url" id="directory-import-mcp-url" placeholder="https://mcp.example.com/mcp" required />
          </div>
          <div class="form-group">
            <label>Display Name (optional)</label>
            <input type="text" id="directory-import-mcp-name" placeholder="e.g. Firecrawl MCP" autocomplete="off" />
          </div>
          <div class="form-group">
            <label>Transport</label>
            <select id="directory-import-mcp-transport">
              <option value="streamable_http" selected>streamable_http</option>
              <option value="http">http</option>
            </select>
            <div class="form-hint">
              Use <code>streamable_http</code> for native MCP servers exposed over HTTP. Stdio-only MCP servers need a bridge or proxy that Agent Core can reach over HTTP.
            </div>
          </div>
          <div class="form-group">
            <label>Timeout (ms)</label>
            <input type="number" id="directory-import-mcp-timeout" min="1000" step="1000" value="60000" />
          </div>
          <div class="form-group">
            <label>Discovery Auth (optional)</label>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.5rem">
              <input type="text" id="directory-import-mcp-auth-header" placeholder="Header name" autocomplete="off">
              <input type="text" id="directory-import-mcp-auth-value" placeholder="Value" autocomplete="off">
            </div>
            <div class="form-hint">Used only during import and refresh — not stored in the binding.</div>
          </div>
          <div class="modal-actions">
            <button type="submit" class="btn">Import</button>
            <button type="button" class="btn btn-secondary" onclick="closeModal('directory-import-mcp-modal')">Cancel</button>
          </div>
        </form>
      </div>
    </div>

    <div class="modal-overlay" id="dir-detail-modal" style="display:none">
      <div class="modal" style="max-width:650px">
        <div id="dir-detail-content"></div>
        <div class="modal-actions" id="dir-detail-actions"></div>
      </div>
    </div>

    <script>
    let _dirPage = 1;
    let _dirSearch = '';
    let _dirCategory = '';

    async function loadDirectory(page) {
      if (page !== undefined) _dirPage = page;
      const grid = document.getElementById('directory-grid');
      grid.innerHTML = '<em>Loading...</em>';

      const params = new URLSearchParams({ page: _dirPage, limit: 30 });
      if (_dirSearch) params.set('q', _dirSearch);
      if (_dirCategory) params.set('category', _dirCategory);

      const j = await apiFetch('/api/connector-types/directory?' + params.toString());
      if (!j.ok) {
        grid.innerHTML = '<em>Could not load directory. Try again later.</em>';
        return;
      }
      const entries = j.data.entries || [];
      _dirEntriesCache = entries;
      const total = j.data.total || 0;

      const catSel = document.getElementById('dir-category');
      if (catSel.options.length <= 1 && j.data.categories) {
        j.data.categories.forEach(function(c) {
          const opt = document.createElement('option');
          opt.value = c;
          opt.textContent = c.charAt(0).toUpperCase() + c.slice(1);
          catSel.appendChild(opt);
        });
      }

      if (!entries.length) {
        grid.innerHTML = '<em>No APIs found matching your search.</em>';
        document.getElementById('directory-pagination').innerHTML = '';
        return;
      }

      const cards = entries.map(function(e) {
        const btn = e.variant_count > 1
          ? '<button type="button" class="btn btn-sm btn-primary" onclick="showDirectoryDetail(&apos;' + escapeHtml(e.id) + '&apos;)">View Variants</button>'
          : (e.installed
            ? '<button type="button" class="btn btn-sm btn-secondary" disabled>Already imported</button>'
            : '<button type="button" class="btn btn-sm btn-primary" onclick="importFromDirectory(&apos;' + escapeHtml(e.id) + '&apos;)">Import</button>');
        return '<div class="connector-type-card">' +
          '<div class="connector-type-head"><div>' +
          '<div class="connector-type-name"><a href="#" onclick="event.preventDefault();showDirectoryDetail(&apos;' + escapeHtml(e.id) + '&apos;)" style="color:var(--text);text-decoration:none">' + escapeHtml(e.display_name) + '</a></div>' +
          '<div class="connector-type-desc">' + escapeHtml((e.description || '').substring(0, 150)) + '</div>' +
          '</div></div>' +
          '<div class="connector-type-meta">' +
          '<span class="badge badge-stale">' + escapeHtml(e.category || '') + '</span> ' +
          (e.provider ? '<span class="badge badge-info">' + escapeHtml(e.provider) + '</span>' : '') +
          (e.variant_count > 1 ? ' <span class="badge badge-ok">' + e.variant_count + ' variants</span>' : '') +
          '</div>' +
          '<div class="connector-type-footer">' + btn + '</div>' +
          '</div>';
      }).join('');
      grid.innerHTML = cards;

      const totalPages = Math.ceil(total / 30);
      const pag = document.getElementById('directory-pagination');
      if (totalPages <= 1) {
        pag.innerHTML = '<span class="page-info">' + total + ' APIs</span>';
      } else {
        pag.innerHTML =
          '<button ' + (_dirPage <= 1 ? 'disabled' : '') + ' onclick="loadDirectory(' + (_dirPage - 1) + ')">Prev</button>' +
          '<span class="page-info">Page ' + _dirPage + ' of ' + totalPages + ' (' + total + ' APIs)</span>' +
          '<button ' + (_dirPage >= totalPages ? 'disabled' : '') + ' onclick="loadDirectory(' + (_dirPage + 1) + ')">Next</button>';
      }
    }

    let _dirSearchTimer = null;
    document.getElementById('dir-search').addEventListener('input', function(e) {
      _dirSearch = e.target.value.trim();
      clearTimeout(_dirSearchTimer);
      _dirSearchTimer = setTimeout(function() { loadDirectory(1); }, 300);
    });

    document.getElementById('dir-category').addEventListener('change', function(e) {
      _dirCategory = e.target.value;
      loadDirectory(1);
    });

    let _dirEntriesCache = [];

    async function showDirectoryDetail(entryId) {
      let entry = (_dirEntriesCache || []).find(function(e) { return e.id === entryId; });
      if (!entry) {
        const j = await apiFetch('/api/connector-types/directory?q=' + encodeURIComponent(entryId) + '&limit=100');
        if (j.ok) entry = (j.data.entries || []).find(function(e) { return e.id === entryId; });
      }
      if (!entry) { showToast('API not found', 'danger'); return; }

      const el = document.getElementById('dir-detail-content');
      const logo = entry.logo_url ? '<img src="' + escapeHtml(entry.logo_url) + '" style="max-height:40px;max-width:40px;border-radius:6px;margin-right:10px;vertical-align:middle" onerror="this.style.display=&apos;none&apos;">' : '';
      const desc = entry.description || 'No description available.';
      const cats = (entry.categories || [entry.category]).filter(Boolean).map(function(c) { return '<span class="badge badge-stale">' + escapeHtml(c) + '</span>'; }).join(' ');
      const provider = entry.provider ? '<span class="badge badge-info">' + escapeHtml(entry.provider) + '</span>' : '';
      const variants = entry.variant_count > 1 ? '<span class="badge badge-ok">' + entry.variant_count + ' variants (GHES, GHEC, etc.)</span>' : '';
      const variantRows = (entry.variants || []).map(function(v) {
        const installed = v.installed ? '<span class="badge badge-stale">Imported</span>' : '';
        const importBtn = v.installed
          ? '<button class="btn btn-sm btn-secondary" disabled>Already imported</button>'
          : '<button class="btn btn-sm btn-primary" onclick="startDirectoryImport(&apos;' + escapeHtml(v.id) + '&apos;, &apos;' + escapeHtml(v.spec_url) + '&apos;, &apos;' + escapeHtml(v.display_name) + '&apos;)">Import</button>';
        return '<tr>' +
          '<td><code>' + escapeHtml(v.id) + '</code></td>' +
          '<td>' + escapeHtml(v.version || '-') + '</td>' +
          '<td style="word-break:break-all">' + escapeHtml(v.spec_url || '-') + '</td>' +
          '<td>' + installed + '</td>' +
          '<td>' + importBtn + '</td>' +
        '</tr>';
      }).join('');

      el.innerHTML =
        '<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">' +
          logo +
          '<h3 style="margin:0">' + escapeHtml(entry.display_name) + '</h3>' +
        '</div>' +
        '<div style="margin-bottom:10px">' + cats + ' ' + provider + ' ' + variants + '</div>' +
        '<div style="margin-bottom:10px;color:var(--muted);font-size:0.9em;white-space:pre-wrap;word-break:break-word;max-height:200px;overflow-y:auto">' + escapeHtml(desc) + '</div>' +
        '<table style="width:100%;font-size:0.85em">' +
          '<tr><td style="color:var(--muted);padding:4px 8px 4px 0;white-space:nowrap">Provider</td><td>' + escapeHtml(entry.provider || '-') + '</td></tr>' +
          '<tr><td style="color:var(--muted);padding:4px 8px 4px 0;white-space:nowrap">Version</td><td>' + escapeHtml(entry.version || '-') + '</td></tr>' +
          (entry.website ? '<tr><td style="color:var(--muted);padding:4px 8px 4px 0;white-space:nowrap">Website</td><td><a href="' + escapeHtml(entry.website) + '" target="_blank" rel="noopener">' + escapeHtml(entry.website) + '</a></td></tr>' : '') +
          (entry.origin_url ? '<tr><td style="color:var(--muted);padding:4px 8px 4px 0;white-space:nowrap">Spec source</td><td><a href="' + escapeHtml(entry.origin_url) + '" target="_blank" rel="noopener">' + escapeHtml(entry.origin_url.substring(0, 80)) + '</a></td></tr>' : '') +
          '<tr><td style="color:var(--muted);padding:4px 8px 4px 0;white-space:nowrap">Spec URL</td><td style="word-break:break-all">' + escapeHtml(entry.spec_url) + '</td></tr>' +
        '</table>';
      if (entry.variant_count > 1) {
        el.innerHTML +=
          '<h4 style="margin:16px 0 8px">Variants</h4>' +
          '<table style="width:100%;font-size:0.9em">' +
            '<thead><tr><th>Variant</th><th>Version</th><th>Spec URL</th><th>Status</th><th class="actions-cell">Actions</th></tr></thead>' +
            '<tbody>' + variantRows + '</tbody>' +
          '</table>';
      }

      const actions = document.getElementById('dir-detail-actions');
      if (entry.installed) {
        actions.innerHTML = '<button class="btn btn-secondary" disabled>Already imported</button> <button class="btn btn-secondary" onclick="closeModal(&apos;dir-detail-modal&apos;)">Close</button>';
      } else {
        actions.innerHTML = entry.variant_count > 1
          ? '<button class="btn btn-secondary" onclick="closeModal(&apos;dir-detail-modal&apos;)">Close</button>'
          : '<button class="btn" onclick="closeModal(&apos;dir-detail-modal&apos;);startDirectoryImport(&apos;' + escapeHtml(entry.id) + '&apos;, &apos;' + escapeHtml(entry.spec_url) + '&apos;, &apos;' + escapeHtml(entry.display_name) + '&apos;)">Import</button> <button class="btn btn-secondary" onclick="closeModal(&apos;dir-detail-modal&apos;)">Close</button>';
      }
      openModal('dir-detail-modal');
    }

    async function startDirectoryImport(entryId, specUrl, displayName) {
      document.getElementById('import-spec-url').value = specUrl || '';
      document.getElementById('import-spec-json').value = '';
      document.getElementById('import-spec-name').value = displayName || entryId || '';
      resetImportPreview();
      closeModal('dir-detail-modal');
      openModal('import-spec-modal');
    }

    async function importFromDirectory(entryId) {
      const params = new URLSearchParams({ page: _dirPage, limit: 30, q: _dirSearch, category: _dirCategory });
      const j = await apiFetch('/api/connector-types/directory?' + params.toString());
      if (!j.ok) { showToast('Failed to look up API', 'danger'); return; }
      const allPages = j.data.entries || [];
      let entry = allPages.find(function(e) { return e.id === entryId; });
      if (!entry) {
        const single = await apiFetch('/api/connector-types/directory?q=' + encodeURIComponent(entryId) + '&limit=100');
        if (single.ok) entry = (single.data.entries || []).find(function(e) { return e.id === entryId; });
      }
      if (!entry) { showToast('API not found', 'danger'); return; }

      startDirectoryImport(entry.id, entry.spec_url, entry.display_name);
    }

    let importSpecPreviewState = null;

    function resetImportPreview() {
      importSpecPreviewState = null;
      const preview = document.getElementById('import-spec-preview');
      const importBtn = document.getElementById('import-spec-import-btn');
      if (preview) {
        preview.style.display = 'none';
        preview.innerHTML = '';
      }
      if (importBtn) {
        importBtn.disabled = true;
      }
    }

    function handleSpecFile(e) {
      const file = e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = function(ev) {
        document.getElementById('import-spec-json').value = ev.target.result;
        resetImportPreview();
        showToast('File loaded', 'success');
      };
      reader.readAsText(file);
    }

    ['import-spec-url', 'import-spec-json', 'import-spec-name'].forEach(function(id) {
      const el = document.getElementById(id);
      if (el) {
        el.addEventListener('input', resetImportPreview);
        el.addEventListener('change', resetImportPreview);
      }
    });

    async function previewSpec(e) {
      if (e) e.preventDefault();
      const url = document.getElementById('import-spec-url').value.trim();
      const specJson = document.getElementById('import-spec-json').value.trim();
      const displayName = document.getElementById('import-spec-name').value.trim();

      if (!url && !specJson) {
        showToast('Provide a URL or paste/upload a spec', 'danger');
        return;
      }

      const body = {};
      if (url) body.url = url;
      if (specJson) body.spec_json = specJson;
      if (displayName) body.display_name = displayName;

      const j = await apiFetch('/api/connector-types/preview', { method: 'POST', body: JSON.stringify(body) });
      if (!j.ok) {
        showToast(j.error?.message || 'Validation failed', 'danger');
        return;
      }

      const preview = j.data.preview || {};
      importSpecPreviewState = preview;
      const previewEl = document.getElementById('import-spec-preview');
      const importBtn = document.getElementById('import-spec-import-btn');
      if (previewEl) {
        const servers = (preview.servers || []).slice(0, 3).map(escapeHtml).join('<br>');
        const warnings = (preview.warnings || []).map(function(w) {
          return '<li>' + escapeHtml(w) + '</li>';
        }).join('');
        const actions = (preview.supported_actions || []).slice(0, 8).map(function(a) {
          return '<span class="badge" style="margin:0 6px 6px 0;display:inline-block">' + escapeHtml(a) + '</span>';
        }).join('');
        previewEl.innerHTML =
          '<h4 style="margin-top:0">Preview</h4>' +
          '<table style="width:100%">' +
            '<tr><td style="padding:4px 8px 4px 0;color:var(--muted)">Name</td><td>' + escapeHtml(preview.display_name || preview.connector_type_id || 'API') + '</td></tr>' +
            '<tr><td style="padding:4px 8px 4px 0;color:var(--muted)">Connector ID</td><td><code>' + escapeHtml(preview.connector_type_id || '-') + '</code></td></tr>' +
            '<tr><td style="padding:4px 8px 4px 0;color:var(--muted)">Auth</td><td>' + escapeHtml(preview.auth_type || 'none') + '</td></tr>' +
            '<tr><td style="padding:4px 8px 4px 0;color:var(--muted)">Servers</td><td style="word-break:break-word">' + (servers || '<em>none</em>') + '</td></tr>' +
            '<tr><td style="padding:4px 8px 4px 0;color:var(--muted)">Actions</td><td>' + escapeHtml(String(preview.operation_count || 0)) + '</td></tr>' +
          '</table>' +
          (actions ? '<div style="margin-top:10px">' + actions + '</div>' : '') +
          (warnings ? '<div style="margin-top:10px"><strong>Warnings</strong><ul style="margin:6px 0 0 18px">' + warnings + '</ul></div>' : '');
        previewEl.style.display = '';
      }
      if (importBtn) {
        importBtn.disabled = false;
      }
      const previewName = preview.display_name || preview.connector_type_id || 'API';
      const previewCount = preview.operation_count != null ? preview.operation_count : 0;
      if (previewEl && previewEl.scrollIntoView) {
        previewEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
      showToast('Validated ' + previewName + ' (' + previewCount + ' actions)', 'success');
    }

    async function importSpec(e) {
      if (e) e.preventDefault();
      if (!importSpecPreviewState) {
        showToast('Validate the spec before creating it', 'danger');
        return;
      }
      const url = document.getElementById('import-spec-url').value.trim();
      const specJson = document.getElementById('import-spec-json').value.trim();
      const displayName = document.getElementById('import-spec-name').value.trim();

      if (!url && !specJson) {
        showToast('Provide a URL or paste/upload a spec', 'danger');
        return;
      }

      const body = {};
      if (url) body.url = url;
      if (specJson) body.spec_json = specJson;
      if (displayName) body.display_name = displayName;

      const j = await apiFetch('/api/connector-types/import', { method: 'POST', body: JSON.stringify(body) });
      if (!j.ok) {
        showToast(j.error?.message || 'Import failed', 'danger');
        return;
      }

      closeModal('import-spec-modal');
      showToast('Created ' + (j.data.connector_type?.display_name || 'spec') + ' (' + j.data.operation_count + ' actions)', 'success');
      document.getElementById('import-spec-url').value = '';
      document.getElementById('import-spec-json').value = '';
      document.getElementById('import-spec-name').value = '';
      resetImportPreview();
      loadDirectory();
    }

    async function importMcpServer(e) {
      if (e) e.preventDefault();
      const prefix = document.getElementById('directory-import-mcp-url') ? 'directory-' : '';
      const url = document.getElementById(prefix + 'import-mcp-url').value.trim();
      const displayName = document.getElementById(prefix + 'import-mcp-name').value.trim();
      const transportType = document.getElementById(prefix + 'import-mcp-transport').value || 'streamable_http';
      const timeoutMs = parseInt(document.getElementById(prefix + 'import-mcp-timeout').value || '60000', 10);
      const authHeader = document.getElementById(prefix + 'import-mcp-auth-header')?.value.trim();
      const authValue = document.getElementById(prefix + 'import-mcp-auth-value')?.value.trim();

      if (!url) {
        showToast('Provide an MCP server URL', 'danger');
        return;
      }

      const body = { url, transport_type: transportType, timeout_ms: timeoutMs };
      if (displayName) body.display_name = displayName;
      if (authHeader && authValue) body.headers_json = JSON.stringify({[authHeader]: authValue});

      const j = await apiFetch('/api/connector-types/import-mcp', { method: 'POST', body: JSON.stringify(body) });
      if (!j.ok) {
        showToast(j.error?.message || 'MCP import failed', 'danger');
        return;
      }

      closeModal(prefix + 'import-mcp-modal');
      showToast('Imported ' + (j.data.connector_type?.display_name || 'MCP server') + ' (' + j.data.tool_count + ' tools)', 'success');
      document.getElementById(prefix + 'import-mcp-form').reset();
      location.reload();
    }

    function escapeHtml(s) {
      if (!s) return '';
      return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    (typeof apiFetch !== 'undefined') ? loadDirectory() : document.addEventListener('DOMContentLoaded', function() { loadDirectory(); });
    </script>
    """
    body = body.replace("Agent Core", APP_NAME)
    return render_page("API Directory", body, "/connectors", session=session)
