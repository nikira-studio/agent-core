import asyncio
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

    display_name = ct.get("display_name") or ct.get("id") or "Connection"
    setup = (adapter_entry or {}).get("setup") or {}
    auth_type = ct.get("auth_type") or "none"
    return {
        "suggested_binding_name": display_name,
        "suggested_credential_name": f"{display_name} credentials",
        "credential_fields": credential_fields,
        "credential_required": bool(credential_fields),
        "auth_type": auth_type,
        "config_fields": config_fields,
        "setup_instructions": setup.get("instructions") or "",
        "documentation_url": setup.get("documentation_url") or "",
    }


def _binding_recipe_line(guidance: dict) -> str:
    bits = [f"Suggested name: {guidance.get('suggested_binding_name') or ''}".strip()]
    credential_fields = guidance.get("credential_fields") or []
    config_fields = guidance.get("config_fields") or []
    if credential_fields:
        bits.append("Credential JSON fields: " + ", ".join(credential_fields))
    if config_fields:
        bits.append("Config JSON fields: " + ", ".join(config_fields))
    return " · ".join([b for b in bits if b and b != "Suggested name: "])


def _render_adapter_cards(adapter_entries: list[dict], ctx) -> str:
    adapter_cards = ""
    for adapter in adapter_entries:
        source_label = "Built-in" if adapter.get("shipped") else "Added here"
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
            "suggested_binding_name": adapter.get("display_name") or adapter.get("id") or "Connection",
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
            req_bits.append("Connection config JSON: " + ", ".join(required_config))
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
                        f'data-adapter-update="{escape_html(adapter["id"])}">Update</button>'
                    )
                action_btn += (
                    f"<button type='button' class='btn btn-sm btn-danger' "
                    f'data-adapter-uninstall="{escape_html(adapter["id"])}">Uninstall</button>'
                )
        elif installable:
            state_badge = '<span class="badge badge-warning">Available</span>'
            action_btn = (
                f"<button type='button' class='btn btn-sm btn-primary' "
                f'data-adapter-install="{escape_html(adapter["id"])}">Install</button>'
                if ctx.is_admin
                else ""
            )
        else:
            state_badge = '<span class="badge badge-danger">Unavailable</span>'
            action_btn = ""

        requirement_line = ""
        if req_text:
            requirement_line = (
                "<div style='font-size:0.875em;"
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
                "<div style='font-size:0.875em;color:var(--text-muted);margin-top:0.25rem'>"
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
                "<div style='font-size:0.875em;color:var(--text-muted);margin-top:0.1rem'>"
                + f"Installed {escape_html(str(installed_version))}"
                + "</div>"
            )

        adapter_cards += f"""
        <div class='connector-type-card' data-adapter-card data-search-text="{escape_html((adapter.get("display_name", "") or "") + " " + (adapter.get("description", "") or "") + " " + (source_version or "") + " " + (installed_version or "") + " " + source_label + " " + req_text)}">
          <div style="padding:0 0 0.5rem">
            <div class='connector-type-name' style="margin:0">{escape_html(adapter["display_name"])}</div>
            <div style="font-size:0.875em;color:var(--text-muted);margin-top:0.1rem">
              {version_line}
            </div>
            {installed_version_line}
            <div class='connector-type-desc' style="margin-top:0.35rem">{escape_html(adapter.get("description", "") or "")}</div>
            {requirement_line}
            {binding_recipe_line}
          </div>
          <div class='connector-type-footer' style="margin-top:auto; display:flex; flex-direction:column; gap:8px; align-items:stretch;">
            <div style="display:flex;align-items:center;gap:0.4rem;">{state_badge}</div>
            <div class="connector-card-actions">
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

    # Adapter setup metadata belongs to the filesystem-backed browse catalog,
    # not the service catalog.  Fetch it separately so ordinary connector-type
    # reads elsewhere stay SQLite-only.
    available_adapters = {
        entry["id"]: entry for entry in await asyncio.to_thread(adapter_loader.list_available_adapters)
    }
    connector_types = connector_service.list_connector_types()
    binding_guidance = {
        ct["id"]: _binding_guidance_for_connector_type(
            ct, available_adapters.get(ct["id"])
        )
        for ct in connector_types
    }

    all_bindings = connector_service.list_bindings()
    # Indexed once for the connection table below; see failing_actions_by_binding.
    failing_by_binding = connector_service.failing_actions_by_binding()
    visible_bindings = [b for b in all_bindings if enforcer.can_read(b["scope"])]
    connector_type_count = len(connector_types)
    visible_binding_count = len(visible_bindings)
    failed_binding_count = len([b for b in visible_bindings if b.get("last_error")])
    readable_scopes = None if ctx.is_admin else enforcer.filter_readable_scopes(ctx.read_scopes)
    with get_db() as conn:
        scope_clause = ""
        scope_params = []
        if readable_scopes is not None:
            if not readable_scopes:
                execution_rows = []
            else:
                placeholders = ",".join("?" for _ in readable_scopes)
                scope_clause = f"WHERE cb.scope IN ({placeholders})"
                scope_params = readable_scopes
        if readable_scopes is None or readable_scopes:
            execution_rows = conn.execute(
                f"""
                SELECT ce.id, ce.binding_id, ce.action, ce.result_status, ce.error_message, ce.failure_category,
                       ce.executed_at, cb.name as binding_name, cb.scope, ct.display_name as connector_display_name
                FROM connector_executions ce
                JOIN connector_bindings cb ON ce.binding_id = cb.id
                JOIN connector_types ct ON cb.connector_type_id = ct.id
                {scope_clause}
                ORDER BY ce.executed_at DESC
                LIMIT 8
                """,
                scope_params,
            ).fetchall()
    visible_executions = [dict(row) for row in execution_rows]

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
    scope_names = {user_scope: "Just me (personal)"}
    scope_names.update({f"workspace:{p['id']}": (p.get("name") or p["id"]) for p in workspaces})
    scope_names.update({f"agent:{a['id']}": (a.get("display_name") or a["id"]) for a in agents})
    workspace_scope_opts = "".join(
        f'<option value="workspace:{p["id"]}">{escape_html(p.get("name") or p["id"])}</option>'
        for p in workspaces
    )
    agent_scope_opts = "".join(
        f'<option value="agent:{a["id"]}">{escape_html(a.get("display_name") or a["id"])}</option>'
        for a in agents
        if a.get("is_active")
    )
    scope_options = (
        f'<optgroup label="You"><option value="{user_scope}">Just me (personal)</option></optgroup>\n'
        f'<optgroup label="Workspaces">{workspace_scope_opts}</optgroup>\n'
        f'<optgroup label="Agents (private to one agent)">{agent_scope_opts}</optgroup>'
    )

    connector_type_opts = "".join(
        (
            f'<option value="{ct["id"]}" '
            f'data-display-name="{escape_html(ct["display_name"])}" '
            f'data-provider-type="{escape_html(ct.get("provider_type") or "openapi")}" '
            f'data-needs-base-url="{"1" if ((ct.get("backend_type") == "generic_http" or ct.get("provider_type") == "generic_http") and not ct.get("endpoint_url")) or "base_url" in (binding_guidance.get(ct["id"], {}).get("config_fields") or []) else "0"}" '
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
    # visible enabled connection. Surfaced as a badge on the Service Catalog card and
    # refreshed when the operator runs Check Health.
    ct_health = {}
    ct_last_check = {}
    for ct in connector_types:
        enabled = [
            b
            for b in visible_bindings
            if b["connector_type_id"] == ct["id"] and b.get("enabled")
        ]
        failed = [b for b in enabled if b.get("last_error")]
        cannot_test = [b for b in enabled if b.get("last_tested_at") and not b.get("last_error") and b.get("last_test_note")]
        tested_ok = [
            b for b in enabled if b.get("last_tested_at") and not b.get("last_error") and not b.get("last_test_note")
        ]
        checked = [b["last_tested_at"] for b in enabled if b.get("last_tested_at")]
        ct_last_check[ct["id"]] = (
            max(checked)[:10] if checked else None,
            len(tested_ok), len(cannot_test), len(failed),
        )
        if not enabled:
            ct_health[ct["id"]] = ("none", 0)
        elif failed:
            ct_health[ct["id"]] = ("issues", len(failed))
        elif len(tested_ok) + len(cannot_test) < len(enabled):
            ct_health[ct["id"]] = ("untested", len(enabled) - len(tested_ok) - len(cannot_test))
        elif not tested_ok:
            ct_health[ct["id"]] = ("cannot_test", len(cannot_test))  # nothing safe to try; not a problem
        else:
            ct_health[ct["id"]] = ("healthy", len(enabled))
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
            status_text = f"Error: {str(b['last_error'])[:70]}"
        elif b.get("last_tested_at") and b.get("last_test_note"):
            status_cls = "status-neutral"
            status_text = f"Can't be tested ({b['last_tested_at'][:10]})"
        elif b.get("last_tested_at"):
            status_text = f"OK ({b['last_tested_at'][:10]})"

        # A passing health check only proves the connection works. Report actions
        # that keep failing, which the probe cannot see.
        failing = failing_by_binding.get(b["id"], [])
        if failing:
            worst = failing[0]
            status_cls = "status-error"
            status_text = (
                f"{worst['action']} failing {worst['failures']}/{worst['calls']}"
                + (f" (+{len(failing) - 1} more)" if len(failing) > 1 else "")
            )
        oauth_button = ""
        if ct and ct.get("auth_type") == "oauth2":
            oauth_label = (
                "Authorize Google"
                if ct.get("id", "").startswith("google_")
                else "Authorize OAuth"
            )
            oauth_button = (
                f"<button type='button' class='btn btn-sm btn-primary' "
                f'data-binding-oauth="{escape_html(b["id"])}">{escape_html(oauth_label)}</button>'
            )
        bindings_rows += f"""
        <tr data-binding-id="{b["id"]}" data-connector-type-id="{escape_html(b.get("connector_type_id", ""))}" data-provider-type="{escape_html(ct.get("provider_type", "") if ct else "")}">
          <td style="{text_style}">{escape_html(b.get("name", ""))}</td>
          <td data-label="Service" style="{text_style}">{escape_html(ct.get("display_name", "") if ct else b.get("connector_type_id", ""))}</td>
          <td data-label="Workspace" style="{text_style}" title="{escape_html(b.get("scope", ""))}">{escape_html(scope_names.get(b.get("scope", ""), b.get("scope", "")))}</td>
          <td data-label="Status" class="{status_cls}" style="{text_style}"{(' title="' + escape_html(b["last_test_note"]) + '"') if (b.get("last_test_note") and not b.get("last_error")) else ((' title="' + escape_html(b["last_error"]) + '"') if b.get("last_error") else "")}>{escape_html(status_text)}</td>
          <td class='actions-cell'>
            {oauth_button}
            <button type='button' class='btn btn-sm btn-secondary' data-binding-edit="{escape_html(b["id"])}">Edit</button>
            <button type='button' class='btn btn-sm btn-secondary' data-binding-test="{escape_html(b["id"])}">Test</button>
            <details class='row-more'><summary class='btn btn-sm btn-secondary' title='History, delete'>More</summary><div class='row-more-list'>
              <button type='button' class='btn btn-sm btn-secondary' data-binding-history="{escape_html(b["id"])}">History</button>
              <button type='button' class='btn btn-sm btn-danger icon-delete-btn' data-binding-delete="{escape_html(b["id"])}" title='Delete connection' aria-label='Delete connection'>{get_icon("delete")} Delete</button>
            </div></details>
          </td>
        </tr>"""

    if visible_bindings:
        bindings_html = f"""
        <table class="stack-phone"><thead><tr><th>Name</th><th>Service</th><th>Workspace</th><th>Status</th><th class='actions-cell'>Actions</th></tr></thead>
        <tbody>{bindings_rows}</tbody></table>"""
    else:
        bindings_html = (
            "<div class='empty'>No connections yet. Use <strong>Add Connection</strong> on a service above to let agents use it.</div>"
        )

    execution_rows_html = ""
    for execution in visible_executions:
        status = execution.get("result_status") or "unknown"
        failure_category = execution.get("failure_category") or ""
        error_message = execution.get("error_message") or ""
        execution_note = failure_category.replace("_", " ") or str(error_message)[:48]
        if status == "success":
            badge_style = "background:rgba(80,200,120,0.15);color:var(--success)"
        elif status in ("failure", "error"):
            badge_style = "background:rgba(224,80,80,0.15);color:var(--danger)"
        else:
            badge_style = "background:rgba(107,114,128,0.15);color:var(--muted)"
        execution_rows_html += f"""
        <tr>
          <td><button type="button" class="btn btn-sm btn-secondary" data-binding-history="{escape_html(execution.get("binding_id", ""))}">{escape_html(execution.get("binding_name", ""))}</button></td>
          <td data-label="Action"><code>{escape_html(execution.get("action", ""))}</code></td>
          <td data-label="Result"><span class="badge" style="{badge_style}">{escape_html(status)}</span></td>
          <td data-label="When">{local_dt(execution.get("executed_at"))}</td>
          <td data-label="Notes">{escape_html(execution_note)}</td>
        </tr>"""
    if execution_rows_html:
        executions_html = f"""
        <table class="stack-phone">
          <thead><tr><th>Connection</th><th>Action</th><th>Result</th><th>When</th><th>Notes</th></tr></thead>
          <tbody>{execution_rows_html}</tbody>
        </table>"""
    else:
        executions_html = "<div class='empty'>Nothing has run yet. Test a connection or run an action and it will appear here.</div>"

    ct_cards = ""
    for ct in connector_types:
        supported_actions = ct.get("supported_actions", [])
        supported_action_names = connector_service.normalize_action_names(
            supported_actions
        )
        disabled_actions = ct.get("disabled_actions") or []
        provider_type = ct.get("provider_type") or "openapi"
        # One origin chip with one meaning for every card: "Built-in" ships with Agent Core, "Added here" is everything
        # else (an imported MCP server or API spec, a web service, an adapter someone added to this install).
        adapter_for_chip = available_adapters.get(ct["id"])
        if adapter_for_chip is not None:
            built_in = bool(adapter_for_chip.get("shipped"))
        else:
            built_in = ct["id"] == "generic_http"  # the one service type Agent Core seeds itself; a web service someone added is "Added here"
        chip_style = (
            "display:inline-block;font-size:0.875em;font-weight:600;line-height:1;padding:3px 8px;border-radius:10px;"
            "letter-spacing:0.03em;white-space:nowrap;"
        )
        if built_in:
            type_chip = f'<span class="origin-chip" style="{chip_style}background:#6b7280;color:#fff">Built-in</span>'
        else:
            type_chip = f'<span class="origin-chip" style="{chip_style}border:1px solid var(--border);color:var(--muted)">Added here</span>'
        # How it talks is detail, not a chip: it goes on the meta line.
        if provider_type == "mcp":
            transport_text = "MCP server"
        elif provider_type == "openapi":
            transport_text = "API spec"
        elif ct.get("backend_type") in ("http", "generic_http") or provider_type == "generic_http":
            transport_text = "HTTP"
        else:
            transport_text = ""
        action_count = len(supported_action_names)
        enabled_action_count = max(
            action_count
            - len([a for a in disabled_actions if a in supported_action_names]),
            0,
        )
        view_actions_btn = (
            # Carried as data rather than spliced into the handler: HTML
            # entities are decoded before the JavaScript is parsed, so an
            # escaped quote in an imported connector's name would still end the
            # argument and start executing.
            f'<button type="button" class="btn btn-sm btn-secondary"'
            f' data-actions-type-id="{escape_html(ct["id"])}"'
            f' data-actions-name="{escape_html(ct["display_name"])}"'
            f' data-actions-count="{action_count}">View Actions</button>'
            if action_count
            else ""
        )
        binding_count = binding_counts.get(ct["id"], 0)
        binding_action_line = f"Connections: {binding_count}"
        if action_count:
            binding_action_line += (
                f" &middot; {enabled_action_count}/{action_count} actions"
            )
        if transport_text:
            binding_action_line += f" &middot; {transport_text}"
        health_state, health_n = ct_health.get(ct["id"], ("none", 0))
        if health_state == "issues":
            health_badge = f'<span class="badge badge-danger" title="Enabled connections whose last health check failed">{health_n} issue(s)</span>'
        elif health_state == "healthy":
            health_badge = '<span class="badge badge-success" title="All enabled connections passed their last health check">Healthy</span>'
        elif health_state == "untested":
            health_badge = f'<span class="badge badge-warning" title="Enabled connections not yet health-checked">{health_n} untested</span>'
        elif health_state == "cannot_test":
            health_badge = '<span class="badge" title="This service has no read-only action to try. It is checked when it is next used.">Can\'t be tested</span>'
        else:
            health_badge = ""
        last_check_line = ""
        last_date, n_ok, n_skip, n_bad = ct_last_check.get(ct["id"], (None, 0, 0, 0))
        if last_date:
            bits = [f"{n_ok} passed"] if n_ok else []
            if n_skip:
                bits.append(f"{n_skip} can't be tested")
            if n_bad:
                bits.append(f"{n_bad} failed")
            last_check_line = f'<div class="form-hint" style="margin:0" title="Result of the last health check">Last check {last_date}: {", ".join(bits)}</div>'

        adapter_entry = available_adapters.get(ct["id"])
        adapter_installed = bool(adapter_entry and adapter_entry.get("installed"))
        adapter_update_available = bool(
            adapter_entry and adapter_entry.get("update_available")
        )
        # Every card here is installed by definition, so there is no "Installed" chip; only an update is worth saying.
        adapter_badges = ""
        if adapter_installed and adapter_update_available:
            adapter_badges = '<span class="badge badge-warning">Update available</span>'
        tools_btn = (
            f"<button type='button' class='btn btn-sm btn-secondary' data-tools-update-card=\"{escape_html(ct['id'])}\" "
            f"title='Read this service&#39;s tool list again and review what changed'>Update tool list</button>"
            if ct.get("provider_type") == "mcp" and ctx.is_admin and binding_count
            else ""
        )
        update_btn = ""
        if adapter_update_available and ctx.is_admin:
            update_btn = (
                f"<button type='button' class='btn btn-sm btn-primary' "
                f'data-adapter-update="{escape_html(ct["id"])}">Update</button>'
            )
        ct_cards += f"""
        <div class='connector-type-card'>
          <div style="padding:0 0 0.5rem">
            <div class='connector-type-name' style="margin:0">{escape_html(ct["display_name"])}</div>
            <div style="font-size:0.875em;color:var(--text-muted);margin-top:0.1rem">{binding_action_line}</div>
            <div class='connector-type-desc' style="margin-top:0.35rem">{escape_html(ct.get("description", "") or "")}</div>
          </div>
          <div class='connector-type-footer' style="margin-top:auto; display:flex; flex-direction:column; gap:8px; align-items:stretch;">
            <div style="display:flex;align-items:center;gap:0.4rem;min-width:0;flex-wrap:wrap;white-space:nowrap;">{type_chip}{health_badge}{adapter_badges}</div>
            {last_check_line}
            <div class="connector-card-actions">
              {view_actions_btn}
              <button type='button' class='btn btn-sm btn-secondary' data-newbinding-type-id="{escape_html(ct["id"])}" data-newbinding-name="{escape_html(ct["display_name"])}">Add Connection</button>
              {update_btn}
              {tools_btn}
              <button type='button' class='btn btn-sm btn-danger icon-delete-btn' data-connector-type-delete="{escape_html(ct["id"])}" title='{ "Uninstall adapter" if adapter_installed else "Delete service" }' aria-label='{ "Uninstall adapter" if adapter_installed else "Delete service" }'>{get_icon("delete")} Delete</button>
            </div>
          </div>
        </div>"""

    quick_start_html = "" if visible_binding_count else """<div class="card">
      <div class="section-header">
        <h2>Getting a service working</h2>
        <div class="section-note">Four steps, shown until the first connection exists.</div>
      </div>
      <ol style="margin:0;padding-left:20px;line-height:1.6">
        <li>Add the service with the <strong>Add a service</strong> button above.</li>
        <li>Store its key or token on the <a href="/credentials">Credentials</a> page.</li>
        <li>Create a connection: it says which workspace may use the service, and with which credential.</li>
        <li>Test the connection, then ask an agent to use it.</li>
      </ol>
    </div>"""

    body = f"""
    <div class="page-header">
      <div>
        <h1>Services</h1>
        <p class="text-muted" style="max-width:760px;margin-top:8px">
          The outside services agents can use. Add a service, then create a connection to say who may
          use it and with which credential.
        </p>
      </div>
      <div class="page-actions">
        <div class="dropdown" style="display:inline-block;position:relative">
          <button class="btn btn-primary" onclick="toggleDropdown(this)" type="button">Add a service <span style="font-size:0.875em">&#9662;</span></button>
          <div class="dropdown-menu" style="display:none;position:absolute;right:0;top:100%;background:#fff;border:1px solid #ddd;border-radius:4px;box-shadow:0 2px 8px rgba(0,0,0,0.15);z-index:1000;min-width:280px;text-align:left">
            <button class="dropdown-item" style="display:block;width:100%;padding:10px 16px;border:none;background:none;text-align:left;cursor:pointer;font-size:14px" onclick="resetImportPreview();openModal('import-spec-modal');closeAllDropdowns()">From an API spec<br><small class="text-muted">Most web services publish one (an OpenAPI file).</small></button>
            <button class="dropdown-item" style="display:block;width:100%;padding:10px 16px;border:none;background:none;text-align:left;cursor:pointer;font-size:14px" onclick="openModal('import-mcp-modal');closeAllDropdowns()">From an MCP server<br><small class="text-muted">A tool server that already speaks MCP.</small></button>
            <button class="dropdown-item" style="display:block;width:100%;padding:10px 16px;border:none;background:none;text-align:left;cursor:pointer;font-size:14px" onclick="openModal('add-http-modal');closeAllDropdowns()">A plain web service<br><small class="text-muted">Just a web address and how to sign in.</small></button>
          </div>
        </div>
      </div>
    </div>

    <p class="text-muted" id="services-summary" style="margin:0 0 12px">{connector_type_count} service{"" if connector_type_count == 1 else "s"}, {visible_binding_count} connection{"" if visible_binding_count == 1 else "s"}{(", <strong style='color:var(--danger)'>" + str(failed_binding_count) + " with errors</strong>") if failed_binding_count else ""}</p>

    {quick_start_html}

     <div class="card" id="service-catalog">
      <div class="section-header" style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px">
        <div>
          <h2>Service Catalog</h2>
          <div class="section-note">Built-in and added services, shared across this install. Ready-made ones are in the <a href="/connectors/directory">service directory</a> and <a href="/connectors/adapters">ready-made services</a>.</div>
        </div>
        <button type="button" class="btn btn-sm btn-secondary" onclick="checkHealth(this)" title="Test every enabled connection now">Check Health</button>
      </div>
      <div class="connector-types-grid">{ct_cards or "<div class='empty'>No services yet. Add one so agents can act in an outside system, or browse the <a href='/connectors/directory'>service directory</a>.</div>"}</div>
    </div>

    <div class="card" id="bindings">
      <div class="section-header">
        <h2>Connections</h2>
        <div class="section-note">A connection says which workspace may use a service, and with which credential.</div>
      </div>
      <div id="bindings-list">{bindings_html}</div>
    </div>

    <details class="card fold-card" id="executions">
      <summary><span class="fold-title">Recent runs ({len(visible_executions)})</span><span class="fold-hint">What agents and tests have run through connections</span></summary>
      <div id="executions-list" style="margin-top:12px">{executions_html}</div>
    </details>

    <!-- Create Connection Modal -->
    <div class="modal-overlay" id="create-binding-modal" style="display:none">
      <div class="modal">
        <h2>New Connection</h2>
        <p class="form-hint" style="margin-bottom:12px">A connection lets agents in one workspace use a service with one credential.</p>
        <form id="create-binding-form" onsubmit="createBinding(event)">
          <div class="form-group">
            <label>Service *</label>
            <select id="binding-connector-type" required onchange="bindingConnectorTypeChanged()">
              <option value="">-- Select --</option>
              {connector_type_opts}
            </select>
          </div>
          <div class="form-group">
            <div id="binding-recipe" class="form-hint">Choose a service to see which credential it needs.</div>
          </div>
          <div class="form-group" id="binding-setup-group" style="display:none">
            <label>Setup Instructions</label>
            <div id="binding-setup" class="form-hint" style="white-space:pre-line"></div>
          </div>
          <div class="form-group">
            <label>Name *</label>
            <input type="text" id="binding-name" placeholder="e.g. Workspace API" autocomplete="off" required>
            <div class="form-hint">A label for this connection, such as the account or environment it covers.</div>
          </div>
          <div class="form-group">
            <label>Who may use it *</label>
            <select id="binding-scope" required>
              <option value="">Select a workspace...</option>
              {scope_options}
            </select>
            <div class="form-hint">Only agents that can reach this workspace may use the connection.</div>
          </div>
          <div class="form-group">
            <label>Credential</label>
            <div class="form-hint">The key or token Core uses for this service. Agents never see it.</div>
            <select id="binding-credential-mode" onchange="toggleBindingCredentialMode()">
              <option value="none">No credential</option>
              <option value="existing">Use stored credential</option>
              <option value="new">Create new credential</option>
            </select>
          </div>
          <div id="binding-existing-credential-fields" class="form-group" style="display:none">
            <label for="binding-credential">Stored Credential</label>
            <select id="binding-credential">
              <option value="">-- Select stored credential --</option>
              {credential_opts}
            </select>
            <div class="form-hint">The stored credential Core uses when it calls this service.</div>
          </div>
          <div id="binding-new-credential-fields" style="display:none">
            <div class="form-group">
              <label>Credential Name *</label>
              <input type="text" id="binding-new-credential-name" placeholder="e.g. service-token" autocomplete="off">
              <div class="form-hint">A name for the new credential, so you can find it on the Credentials page.</div>
            </div>
            <div id="binding-new-credential-editor"></div>
          </div>
          <div class="form-group" id="binding-base-url-group" style="display:none">
            <label for="binding-base-url">Service address *</label>
            <input type="url" id="binding-base-url" placeholder="https://api.example.com" autocomplete="off">
            <div class="form-hint">The address Core calls for this service. It is tested as soon as the connection is created.</div>
          </div>
          <details class="access-details">
            <summary>Advanced</summary>
            <div class="form-group">
              <label>Extra settings (JSON, optional)</label>
              <textarea id="binding-config" rows="2" placeholder='{{"repo": "owner/name"}}'></textarea>
              <div class="form-hint">Non-secret settings such as <code>base_url</code> or <code>default_params</code>. Leave blank unless the service's setup notes ask for them. A service that signs in with a username and password, or has a self-signed certificate, is set up here too: see "A service that signs in first" in the integrations guide.</div>
            </div>
            <div class="form-group" id="binding-mcp-endpoint-group" style="display:none">
              <label>Different MCP server address</label>
              <input type="url" id="binding-mcp-endpoint" placeholder="https://instance.example/mcp" autocomplete="off">
              <div class="form-hint">Use another server for this connection only. It must offer the same tools, or add it as a separate service.</div>
            </div>
          </details>
          <div class="form-group">
            <label class="checkbox-label">
              <input type="checkbox" id="binding-enabled" checked> Enabled <span class="form-hint">(untick to pause it without deleting)</span>
            </label>
          </div>
          <button type="submit" class="btn btn-primary">Create Connection</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('create-binding-modal')">Cancel</button>
        </form>
      </div>
    </div>

    <!-- Edit Connection Modal -->
    <div class="modal-overlay" id="edit-binding-modal" style="display:none">
      <div class="modal">
        <h2>Edit Connection</h2>
        <form id="edit-binding-form" onsubmit="submitEditBinding(event)">
          <input type="hidden" id="edit-binding-id">
          <div class="form-group">
            <label>Name</label>
            <input type="text" id="edit-binding-name" autocomplete="off">
            <div class="form-hint">A label for this connection, such as the account or environment it covers.</div>
          </div>
          <div class="form-group">
            <label>Who may use it</label>
            <select id="edit-binding-scope">
              {scope_options}
            </select>
            <div class="form-hint">Only agents that can reach this workspace may use the connection.</div>
          </div>
          <div class="form-group">
            <label>Credential</label>
            <select id="edit-binding-credential">
              <option value="">-- Select --</option>
              {credential_opts}
            </select>
            <div class="form-hint">The stored credential Core uses when it calls this service.</div>
          </div>
          <details class="access-details">
            <summary>Advanced</summary>
            <div class="form-group">
              <label>Extra settings (JSON)</label>
              <textarea id="edit-binding-config" rows="2"></textarea>
              <div class="form-hint">Non-secret settings such as <code>base_url</code> or <code>default_params</code>. Leave blank unless the service's setup notes ask for them. A service that signs in with a username and password, or has a self-signed certificate, is set up here too: see "A service that signs in first" in the integrations guide.</div>
            </div>
            <div class="form-group" id="edit-binding-mcp-endpoint-group" style="display:none">
              <label>Different MCP server address</label>
              <input type="url" id="edit-binding-mcp-endpoint" placeholder="https://instance.example/mcp" autocomplete="off">
              <div class="form-hint">Leave blank to use the service's own address. A different one must offer the same tools.</div>
            </div>
          </details>
          <div class="form-group">
            <label class="checkbox-label">
              <input type="checkbox" id="edit-binding-enabled"> Enabled <span class="form-hint">(untick to pause it without deleting)</span>
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
        <h2>Import API Spec</h2>
        <form id="import-spec-form" onsubmit="importSpec(event)">
          <div class="form-group">
            <label>Spec URL</label>
            <input type="url" id="import-spec-url" placeholder="https://example.com/openapi.json">
            <div class="form-hint">Where the API's OpenAPI file lives. Use this, or paste or upload the file below.</div>
          </div>
          <div class="form-group">
            <label>Or paste JSON / upload file</label>
            <textarea id="import-spec-json" rows="6" placeholder='Paste OpenAPI JSON here, or use the file picker below'></textarea>
            <div class="form-hint">For a spec that has no web address. JSON or YAML.</div>
          </div>
          <div class="form-group">
            <input type="file" id="import-spec-file" accept=".json,.yaml,.yml" onchange="handleSpecFile(event)">
          </div>
          <div class="form-group">
            <label>Display Name (optional)</label>
            <input type="text" id="import-spec-name" placeholder="e.g. Example REST API" autocomplete="off">
            <div class="form-hint">How it appears in the catalog.</div>
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
        <h2>Import MCP Server</h2>
        <div style="background:var(--bg-secondary);border-left:3px solid var(--warning-color);padding:0.6rem 0.85rem;border-radius:4px;margin-bottom:1rem;font-size:0.875em">
          <strong>HTTP transport only.</strong> {APP_NAME} connects to MCP servers over HTTP — it cannot launch local processes.
          Servers configured with <code>command</code>/<code>args</code> (stdio) are not supported here.
          Use <strong>Add a service → A plain web service</strong> for REST APIs, or run a stdio server behind an HTTP bridge first.
        </div>
        <form id="import-mcp-form" onsubmit="importMcpServer(event)">
          <div class="form-group">
            <label>Server URL</label>
            <input type="url" id="import-mcp-url" placeholder="https://mcp.example.com/mcp" required>
            <div class="form-hint">The MCP server's web address, usually ending in <code>/mcp</code>.</div>
          </div>
          <div class="form-group">
            <label>Display Name (optional)</label>
            <input type="text" id="import-mcp-name" placeholder="e.g. Context7 MCP" autocomplete="off">
            <div class="form-hint">How it appears in the catalog.</div>
          </div>
          <div class="form-group">
            <label>Description (optional)</label>
            <input type="text" id="import-mcp-description" placeholder="What this MCP server does" autocomplete="off">
            <div class="form-hint">Optional. A line shown with it in the catalog.</div>
          </div>
          <div class="form-group">
            <label>Transport</label>
            <select id="import-mcp-transport">
              <option value="streamable_http" selected>streamable_http (recommended)</option>
              <option value="http">http</option>
            </select>
            <div class="form-hint">Leave this on streamable_http unless the server's own documentation says otherwise.</div>
          </div>
          <div class="form-group">
            <label>Timeout (ms)</label>
            <input type="number" id="import-mcp-timeout" min="1000" step="1000" value="60000">
            <div class="form-hint">How long Core waits for the server to answer before giving up. 60000 is one minute.</div>
          </div>
          <div class="form-group">
            <label>Discovery Auth (optional)</label>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.5rem">
              <input type="text" id="import-mcp-auth-header" placeholder="Header name (e.g. Authorization)" autocomplete="off">
              <input type="text" id="import-mcp-auth-value" placeholder="Value (e.g. Bearer YOUR_KEY)" autocomplete="off">
            </div>
            <div class="form-hint">
              Used only during import and refresh — not stored in the connection. For standard Bearer auth use header <code>Authorization</code> and value <code>Bearer YOUR_KEY</code>. For custom headers like Context7, use <code>CONTEXT7_API_KEY</code> and the raw key as the value.
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
        <h2>Add a plain web service</h2>
        <form id="add-http-form" onsubmit="addHttpConnector(event)">
          <div class="form-group">
            <label>Display Name</label>
            <input type="text" id="http-display-name" placeholder="e.g. OpenRouter" required autocomplete="off">
            <div class="form-hint">How it appears in the catalog.</div>
          </div>
          <div class="form-group">
            <label>Base URL</label>
            <input type="url" id="http-base-url" placeholder="https://openrouter.ai/api/v1" required>
            <div class="form-hint">The address every call starts from, before the specific path.</div>
          </div>
          <div class="form-group">
            <label>Auth Type</label>
            <select id="http-auth-type" onchange="updateHttpAuthFields()">
              <option value="bearer" selected>Bearer token (Authorization: Bearer ...)</option>
              <option value="header">Custom header</option>
              <option value="query">Query parameter</option>
              <option value="none">None</option>
            </select>
            <div class="form-hint">How the service expects its key. Choose Bearer unless its documentation says otherwise.</div>
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
            <div class="form-hint">Headers sent with every call, as JSON. Keep secrets out of here; store them as a credential.</div>
          </div>
          <button type="submit" class="btn btn-primary">Add service</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('add-http-modal')">Cancel</button>
        </form>
      </div>
    </div>

    <!-- View Actions Modal -->
    <div class="modal-overlay" id="view-actions-modal" style="display:none" data-admin="{'1' if ctx.is_admin else '0'}" data-agents="{escape_html(json.dumps([{'id': a['id'], 'name': a.get('display_name') or a['id']} for a in agents if a.get('is_active')]) if ctx.is_admin else '[]')}">
      <div class="modal modal-detail">
        <h2 id="view-actions-title">Actions</h2>
        <div style="display:flex;gap:8px;margin-bottom:12px">
          <input type="text" id="view-actions-filter" placeholder="Filter actions..." oninput="filterActions()" style="flex:1" autocomplete="off">
          <button type="button" class="btn btn-sm btn-secondary" onclick="bulkSetActions(true)">Select All</button>
          <button type="button" class="btn btn-sm btn-secondary" onclick="bulkSetActions(false)">Clear All</button>
        </div>
        <div id="view-actions-content"></div>
        <div style="margin-top:12px;display:flex;gap:8px;justify-content:flex-end">
          <button type="button" class="btn btn-primary" onclick="saveActionSettings()">Save Changes</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('view-actions-modal')">Close</button>
        </div>
      </div>
    </div>

    <!-- Test Result Modal -->
    <div class="modal-overlay" id="test-result-modal" style="display:none">
      <div class="modal modal-detail">
        <h2>Connection Test Result</h2>
        <div id="test-result-content"></div>
        <button type="button" class="btn" onclick="closeTestResult()">Close</button>
      </div>
    </div>

    <!-- Update tool list: what changed on the server, saved only when you say so -->
    <div class="modal-overlay" id="tool-update-modal" style="display:none">
      <div class="modal modal-detail">
        <h2>Update tool list</h2>
        <div id="tool-update-content"></div>
        <div class="modal-actions">
          <button type="button" class="btn" id="tool-update-save" onclick="saveToolUpdate()">Save the new tool list</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('tool-update-modal')">Close</button>
        </div>
      </div>
    </div>

    <!-- OAuth Redirect Modal -->
    <div class="modal-overlay" id="oauth-redirect-modal" style="display:none">
      <div class="modal modal-wide">
        <h2>Google OAuth Redirect URI</h2>
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

    <!-- Connection Executions Modal -->
    <div class="modal-overlay" id="executions-modal" style="display:none">
      <div class="modal modal-detail">
        <h2>Execution History</h2>
        <div id="executions-content"></div>
        <button type="button" class="btn" onclick="closeModal('executions-modal')">Close</button>
      </div>
    </div>
    """

    extra_js = (
        '<script src="/static/js/adapter-actions.js?v=20260902"></script>'
        '<script src="/static/js/connectors.js?v=20261113a"></script>'
    )
    return render_page("Services", body, "/connectors", extra_js, session=session)


@router.get("/connectors/adapters")
async def connectors_adapters_page(
    request: Request,
    session: dict = Depends(require_auth),
):
    ctx = build_user_context(session)
    adapter_entries = await asyncio.to_thread(adapter_loader.list_available_adapters)
    adapter_cards = _render_adapter_cards(adapter_entries, ctx)

    body = f"""
    <div class="page-header">
      <div>
        <h1>Ready-made services</h1>
        <p class="text-muted" style="max-width:760px;margin-top:8px">
          Ready-made services, built in or kept in a local folder, that you can install into the
          service catalog.
        </p>
      </div>
      <div class="page-actions">
        <a class="btn btn-secondary" href="/connectors">&larr; Back to Services</a>
      </div>
    </div>

    <div class="card">
      <div class="section-header" style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px">
        <div>
          <h2>Ready-made services</h2>
          <div class="section-note">Install one into the service catalog, then add a connection for it like any other service.</div>
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
    extra_js = (
        '<script src="/static/js/adapter-actions.js?v=20260902"></script>'
        '<script src="/static/js/connectors-adapters.js?v=20260902"></script>'
    )
    return render_page("Ready-made services", body, "/connectors", extra_js, session=session)


@router.get("/connectors/help")
def connectors_help_page(request: Request, session: dict = Depends(require_auth)):
    body = """
    <div class="page-header"><h1>Services help</h1></div>

    <div class="card">
      <h2>What is an OpenAPI Spec?</h2>
      <p>
        An OpenAPI (formerly Swagger) spec is a machine-readable JSON or YAML file that describes every
        endpoint, parameter, authentication method, and response format for a REST API. When you import
        one into Agent Core, the system automatically discovers all available actions and generates tool
        definitions that AI agents can use directly.
      </p>
    </div>

    <div class="card">
      <h2>How to Find a Spec</h2>
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
      <h2>After Importing</h2>
      <p>Once you import a spec, the workflow is:</p>
      <ol>
        <li><strong>Create a credential</strong> — store your API key or token (a personal access token, for example)</li>
        <li><strong>Create a connection</strong> — link the service to your credential and choose a workspace</li>
        <li><strong>Test the connection</strong> — click Test to verify the credential works</li>
        <li><strong>Use it</strong> — AI agents discover available actions via MCP and execute them through <code>connectors_run</code></li>
      </ol>
    </div>

    <div class="card">
      <h2>Supported Spec Formats</h2>
      <ul>
        <li><strong>OpenAPI 3.0+</strong> (JSON or YAML)</li>
        <li><strong>Swagger 2.0</strong> (JSON or YAML)</li>
      </ul>
      <p>The spec must define at least one <code>server</code> URL with a full hostname (e.g. <code>https://api.example.com</code>). Relative-only server URLs are not supported.</p>
      <p>Specs larger than 5MB cannot be imported via URL. For large specs, download the file locally, trim it if needed, and upload it via the file picker.</p>
    </div>

    <div class="card">
      <h2>Troubleshooting</h2>
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
      <a href="/connectors" class="btn btn-secondary">Back to Services</a>
    </div>
    """
    body = body.replace("Agent Core", APP_NAME)
    return render_page("Services help", body, "/connectors", session=session)


@router.get("/connectors/directory")
def connectors_directory_page(
    request: Request,
    session: dict = Depends(require_auth),
):
    body = """
    <div class="page-header">
      <h1>Service directory</h1>
        <div class="page-actions">
        <a class="btn btn-secondary" href="/connectors">&larr; Back to Services</a>
        <div class="dropdown" style="display:inline-block;position:relative">
          <button class="btn btn-secondary" onclick="toggleDropdown(this)" type="button">Add a service <span style="font-size:0.875em">&#9662;</span></button>
          <div class="dropdown-menu" style="display:none;position:absolute;right:0;top:100%;background:#fff;border:1px solid #ddd;border-radius:4px;box-shadow:0 2px 8px rgba(0,0,0,0.15);z-index:1000;min-width:200px;text-align:left">
            <button class="dropdown-item" style="display:block;width:100%;padding:10px 16px;border:none;background:none;text-align:left;cursor:pointer;font-size:14px" onclick="resetImportPreview();openModal('import-spec-modal');closeAllDropdowns()">From an API spec</button>
            <button class="dropdown-item" style="display:block;width:100%;padding:10px 16px;border:none;background:none;text-align:left;cursor:pointer;font-size:14px" onclick="openModal('directory-import-mcp-modal');closeAllDropdowns()">From an MCP server</button>
          </div>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="section-header">
        <h2>Browse 2,500+ OpenAPI Specs</h2>
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
        <h2>Import API Spec</h2>
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
        <h2>Import MCP Server</h2>
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
            <div class="form-hint">Used only during import and refresh — not stored in the connection.</div>
          </div>
          <div class="modal-actions">
            <button type="submit" class="btn">Import</button>
            <button type="button" class="btn btn-secondary" onclick="closeModal('directory-import-mcp-modal')">Cancel</button>
          </div>
        </form>
      </div>
    </div>

    <div class="modal-overlay" id="dir-detail-modal" style="display:none">
      <div class="modal modal-detail">
        <div id="dir-detail-content"></div>
        <div class="modal-actions" id="dir-detail-actions"></div>
      </div>
    </div>

    <script src="/static/js/connectors-directory.js?v=20260626"></script>
    """
    body = body.replace("Agent Core", APP_NAME)
    return render_page("Service directory", body, "/connectors", session=session)
