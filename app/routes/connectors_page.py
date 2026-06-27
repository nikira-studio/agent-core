import json

from fastapi import APIRouter, Depends, Request
from app.branding import APP_NAME
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

    # Scan the adapter library once and reuse it for both the connector-type
    # filter and the per-type binding guidance (avoids a second full scan).
    available_adapters = {
        entry["id"]: entry for entry in adapter_loader.list_available_adapters()
    }
    connector_types = connector_service.list_connector_types(
        available_adapters=available_adapters
    )
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

    extra_js = '<script src="/static/js/connectors.js?v=20260626"></script>'
    return render_page("Connectors", body, "/connectors", extra_js, session=session)


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


    body = body.replace("Agent Core", APP_NAME)
    extra_js = '<script src="/static/js/connectors-adapters.js?v=20260626"></script>'
    return render_page("Browse Adapters", body, "/connectors", extra_js, session=session)


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

    <script src="/static/js/connectors-directory.js?v=20260626"></script>
    """
    body = body.replace("Agent Core", APP_NAME)
    return render_page("API Directory", body, "/connectors", session=session)
