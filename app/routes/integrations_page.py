"""Integrations dashboard page and agent-setup/connection generators.

Last feature area split out of the former dashboard.py monolith
dashboard.py no longer exists.
"""

from fastapi import APIRouter, Request, Depends
from fastapi.responses import PlainTextResponse
import contextvars
import json
from pathlib import Path

from app.time_utils import utc_now_iso
from app.services.auth_service import get_user_by_id
from app.security.response_helpers import success_response, error_response
from app.security.scope_enforcer import ScopeEnforcer
from app.branding import APP_NAME, APP_SLUG, CREDENTIAL_PREFIX, ENV_PREFIX
from app.routes.dashboard_shared import (
    escape_html,
    require_auth,
    render_page,
)
from app.security.public_url import public_base_url


router = APIRouter()


# The capabilities of the agent a file is being generated for, so the file names the tools that
# agent can call and no others (an Assistant has no approval_propose). None means all of them.
_LISTING_CAPABILITIES: contextvars.ContextVar = contextvars.ContextVar("listing_capabilities", default=None)


def _mcp_tool_listing() -> str:
    """The MCP tools this agent can call, as prose, derived from the manifest.

    Every generated prompt names the available tools. Hardcoded copies of that
    list drifted independently (three templates, three different subsets), so
    it is built from the same MANIFEST and the same capability rules that /mcp
    serves, for the agent the file is for.
    """
    from app.routes.mcp import MANIFEST, _TOOL_CAPABILITIES

    caps = _LISTING_CAPABILITIES.get()

    def allowed(name: str) -> bool:
        required = _TOOL_CAPABILITIES.get(name)
        if caps is None or required is None:
            return True
        return required in caps or (required == "connectors_read" and "connectors_execute" in caps)

    names = [f"`{tool['name']}`" for tool in MANIFEST["tools"] if allowed(tool["name"])]
    return ", ".join(names[:-1]) + f", and {names[-1]}"


# Shared memory-discipline guidance interpolated into every generated agent
# prompt (CLAUDE.md, AGENTS.md, session prompt, assistant onboarding). The
# specific rules come from observed failures: three different live agents
# independently wrote one fact/decision record PER monitor tick / watchdog
# refire / idle heartbeat (140+ near-duplicate records) because the generic
# "no routine progress" rule didn't register for records they classified as
# "findings". This block names that failure mode explicitly.
# The classes only earn their keep if they mean the same thing to every agent
# writing into the workspace. The dividing line is what could settle the
# question: a fact can be checked against the world, a decision can only be
# changed by whoever made it. That distinction is what lets an automated pass
# ever re-verify facts without touching decisions, so it is stated in one place
# and reused by every generator rather than paraphrased per template.
MEMORY_CLASS_GUIDANCE = """Pick the class by asking what would settle it if someone doubted the record:

- `fact` — an observation about how things ARE, which someone could go and check against the code, a host, or a service. It stops being true when the world changes. Examples: "the build server is 192.0.2.10", "the ingest route is POST /api/webhook/health", "the CDN blocks requests sent with a default Python user agent".
- `decision` — a choice about how things SHOULD be, which holds until someone changes their mind. Nothing you could look up confirms or refutes it. Examples: "the app should support light and dark mode", "do not edit vendored dependencies directly", "prefer a deploy key over a personal SSH key".
- `preference` — a standing preference of the user or team, in the user scope.
- `scratchpad` — a temporary note that is expected to stop mattering.

If it would be settled by checking, it is a `fact`. If it would be settled by someone deciding, it is a `decision`. Reporting what you just did is neither — that belongs in `activity_update`.

A recommendation nobody has committed to yet — candidate next steps, options under consideration — is not a `decision`. Write it as `scratchpad` so it stays cheaply searchable, then promote it to a `decision` once someone actually picks one. Don't let it live only inside an activity's `task_result`: the activity trail is for what happened, and digging through it for "what should we do next" is far more expensive than a scoped memory lookup.

On a `fact`, set `subject_anchor` to what a later session would check: `repo:<path>`, `host:<name-or-ip>`, or `service:<binding_id>`, with repo paths relative to the workspace root. Omit it when nothing could verify the record. A `repo:` anchor is checked only where the operator mapped the workspace to a directory on the Core host and a `service:` anchor through that service connection; a `host:` anchor is only a label. A check that finds the subject there changes nothing visible; one that finds it gone queues a suggestion. If a check reports the subject missing but it only moved, call `memory_reanchor` to repoint the record instead of letting it be proposed for removal.

`valid_from` and `valid_to` say when the content was true in the world, separately from when you wrote it. Set them when a record covers a period other than "from now on", and pass `as_of` to `memory_search` to ask what was true at a past moment.

Search results carry `days_since_confirmed`. Treat a fact nobody has confirmed in months as a lead, not as current truth. Check it, then `memory_confirm` it with evidence naming what you checked, or supersede it if it has changed."""

MEMORY_WRITE_WHEN = "Write a memory only for something a future session cannot get from the code, the config or the task history, and that will still be true: a decision with its reason, a constraint that cost effort to find, who or where something is (an account, a host, an owner), or how things are wired. Do not write: that you finished, deployed or fixed something (use `activity_update`); what the code or API already says; an experiment or benchmark result; a ticket or review log; a status that was true for a day; a guess about a cause. One record per lasting insight; replace it when it changes."

MEMORY_DISCIPLINE_GUIDANCE = """Write a memory only for something a future session cannot get from the code, the config or the task history, and that will still be true: a decision with its reason, a constraint that cost effort to find, who or where something is (an account, a host, an owner), or how things are wired. Do not write: that you finished, deployed or fixed something (use activity_update); what the code or API already says; an experiment or benchmark result; a ticket or review log; a status that was true for a day; a guess about a cause. One record per lasting insight; replace it when it changes.

One memory per insight, not per occurrence: when a recurring task (heartbeat, monitor tick, scheduled review, watchdog retry) keeps producing the same finding, write ONE record the first time and supersede it if the situation changes — never a new record per tick, per fire, or per re-check. Per-occurrence status belongs in `activity_update` (`task_note`/`task_result`) or the source system, not in memory. To find what has already been done — "what did we do on X", "has this been tried" — search the activity trail with `activity_search` rather than memory; memory is for what stays true, the trail is for what happened. Before writing a `fact` or `decision`, search for an existing record on the same topic and supersede it (`supersedes_id`) instead of adding a near-duplicate. For notes that stop being true on their own (for example "service X is down right now"), use `scratchpad` or set `expires_at`."""

REPOSITORY_BOUNDARY_GUIDANCE = """Workspace context is not repository access. If a task points to files outside the current checkout, switch to an authorized checkout or report the mismatch. Do not use memory summaries as a substitute for reviewing the source."""

WORKSPACE_SYNC_OPERATING_RULES = """Pass the session's `execution_id` to supported writes, including `activity_update` and `memory_write`. If a fresh `activity_update` returns `since_last_active`, process it as a bounded fallback digest, not complete history; a caught-up execution normally suppresses it. If `has_more` is true, process and acknowledge the current page, then sync again until `has_more` is false. If `cursor_reset` is true, treat the returned bootstrap as incomplete history and inspect the relevant memory and activity trail. If sync fails, continue only when the task does not depend on current shared state. For a review, resume, or handoff, stop and report the sync failure."""

CONNECTOR_BINDING_GUIDANCE = """A visible connector connection grants access to every action enabled on its connector type. Use the connector type's action controls to disable actions for all connections."""


# ─── INTEGRATIONS ─────────────────────────────────────────────────────────────


@router.post("/api/integrations/apply-recommended-access")
@router.post("/api/integrations/apply-access")
async def apply_recommended_access(
    request: Request,
    session: dict = Depends(require_auth),
):
    from app.services import agent_service
    from app.services import workspace_service
    from pydantic import BaseModel

    class ApplyAccessBody(BaseModel):
        user_id: str
        workspace_id: str
        agent_id: str
        include_user_write: bool = False

    body = ApplyAccessBody.model_validate(await request.json())

    is_admin = session.get("role") == "admin"
    current_user_id = session["user_id"]

    if not is_admin and current_user_id != body.user_id:
        return error_response("FORBIDDEN", "Access denied", 403)

    user = get_user_by_id(body.user_id)
    if not user:
        return error_response("NOT_FOUND", "User not found", 404)

    workspace = workspace_service.get_workspace_by_id(body.workspace_id)
    if not workspace:
        return error_response("NOT_FOUND", "Workspace not found", 404)

    can_read_workspace = is_admin or workspace_service.can_user_read_workspace(
        current_user_id, body.workspace_id
    )
    can_write_workspace = is_admin or workspace_service.can_user_write_workspace(
        current_user_id, body.workspace_id
    )

    if not can_read_workspace:
        return error_response("FORBIDDEN", "Access denied", 403)

    agent = agent_service.get_agent_by_id(body.agent_id)
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)

    if not is_admin and agent.get("owner_user_id") != current_user_id:
        return error_response("FORBIDDEN", "Access denied", 403)
    if not _agent_user_matches(agent, body.user_id):
        return error_response(
            "AGENT_USER_MISMATCH",
            "Agents are tied to one owner/default user. Create a separate agent for this user and share workspace access through workspace scopes.",
            400,
        )

    agent_scope = f"agent:{body.agent_id}"
    workspace_scope = f"workspace:{body.workspace_id}"
    user_scope = f"user:{body.user_id}"

    current_read = agent.get("read_scopes_json", "[]")
    current_write = agent.get("write_scopes_json", "[]")

    from app.services.agent_service import parse_scopes

    read_scopes = parse_scopes(current_read) if current_read else []
    write_scopes = parse_scopes(current_write) if current_write else []

    def add_scope(scopes, scope):
        if scope not in scopes:
            scopes.append(scope)

    add_scope(read_scopes, agent_scope)
    add_scope(write_scopes, agent_scope)
    if can_read_workspace:
        add_scope(read_scopes, workspace_scope)
    if can_write_workspace:
        add_scope(write_scopes, workspace_scope)
    add_scope(read_scopes, user_scope)
    if body.include_user_write:
        add_scope(write_scopes, user_scope)

    agent_service.update_agent(
        body.agent_id,
        read_scopes=read_scopes,
        write_scopes=write_scopes,
    )

    from app.services import audit_service

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="scope_grant",
        resource_type="agent",
        resource_id=body.agent_id,
        result="success",
        details={
            "workspace_id": body.workspace_id,
            "user_id": body.user_id,
            "new_read_scopes": read_scopes,
            "new_write_scopes": write_scopes,
            "include_user_write": body.include_user_write,
        },
    )

    return success_response(
        {
            "message": "Access updated",
            "read_scopes": read_scopes,
            "write_scopes": write_scopes,
        }
    )


def _agent_context_user_id(agent):
    return agent.get("default_user_id") or agent.get("owner_user_id")


def _agent_user_matches(agent, user_id):
    return _agent_context_user_id(agent) == user_id


def _label_sort_key(item, label_field):
    """Sort selector entries by their visible label, with IDs breaking ties."""
    return (
        str(item.get(label_field) or item["id"]).casefold(),
        str(item["id"]).casefold(),
    )


def _agent_setup_output_options(target=None):
    return [
        ("instructions", "Instructions", f"{APP_SLUG}-instructions.md"),
        ("mcp_json", "MCP Config", f"{APP_SLUG}-mcp-config.txt"),
        ("env", "Environment Variables", f"{APP_SLUG}.env"),
        ("claude_md", "CLAUDE.md", "CLAUDE.md"),
        ("agents_md", "AGENTS.md", "AGENTS.md"),
        ("assistants_md", "Assistants", "Assistants"),
        ("session", "Session Prompt", f"{APP_SLUG}-session-prompt.md"),
        ("verification", "Verification Prompt", f"{APP_SLUG}-verification.md"),
        ("claude_code_hook", "Claude Code Hook", f"{APP_SLUG}-claude-code-hook.md"),
        ("codex_hook", "Codex Hook", f"{APP_SLUG}-codex-hook.md"),
    ]


CONNECTION_OUTPUT_TYPES = frozenset({"mcp_json", "env", "assistants_md"})


def _agent_setup_output_groups(target=None):
    options = _agent_setup_output_options(target)
    return (
        [
            option
            for option in options
            if option[0] in CONNECTION_OUTPUT_TYPES
        ],
        [
            option
            for option in options
            if option[0] not in CONNECTION_OUTPUT_TYPES
        ],
    )


def _agent_setup_target_label(target):
    return {
        "claude_code": "Claude Code",
        "codex": "Codex",
        "cursor": "Cursor",
        "windsurf": "Windsurf",
        "antigravity": "Antigravity",
        "generic_mcp": "Other MCP tool",
    }.get(target, "Other MCP tool")


# What each tool needs, in the order a person does it. The text is the same generated
# output as the "Other outputs" tabs; this only chooses which pieces to show.
SETUP_TOOLS = (
    ("claude_code", "Claude Code",
     "Run the <code>claude mcp add</code> command, or save the JSON as <code>.mcp.json</code> in your repo root.",
     "claude_md", "CLAUDE.md", "CLAUDE.md",
     "Save this as <code>CLAUDE.md</code> in the root folder of your project (the folder you map to the workspace later). Claude Code reads it automatically."),
    ("codex", "Codex",
     "Add this to <code>~/.codex/config.toml</code> (on Windows, <code>config.toml</code> in the <code>.codex</code> folder of your user folder).",
     "agents_md", "AGENTS.md", "AGENTS.md",
     "Save this as <code>AGENTS.md</code> in the root folder of your project (the folder you map to the workspace later). Codex reads it automatically."),
    ("cursor", "Cursor",
     "Save this as <code>.cursor/mcp.json</code> in your workspace, or <code>~/.cursor/mcp.json</code> for every project, then run Reload MCP Servers.",
     "instructions", "Setup guide", f"{APP_SLUG}-instructions.md",
     "Read this yourself: which output to use, where to put it, and when to generate a one-time key."),
    ("windsurf", "Windsurf",
     "Add this to Windsurf's MCP settings for your workspace. Restart Windsurf if it does not appear.",
     "instructions", "Setup guide", f"{APP_SLUG}-instructions.md",
     "Read this yourself: which output to use, where to put it, and when to generate a one-time key."),
    ("antigravity", "Antigravity",
     "Add this to your MCP settings. It uses <code>serverUrl</code> instead of <code>url</code>. Restart Antigravity if it does not appear.",
     "instructions", "Setup guide", f"{APP_SLUG}-instructions.md",
     "Read this yourself: which output to use, where to put it, and when to generate a one-time key."),
    ("generic_mcp", "Other MCP tool",
     "This has a block for each common tool. Use the one that matches yours; file locations vary (see docs/integrations.md).",
     "instructions", "Setup guide", f"{APP_SLUG}-instructions.md",
     "Read this yourself: which output to use, where to put it, and when to generate a one-time key."),
)


def _agent_setup_access_model(
    agent,
    workspace,
    user_id,
    is_admin=False,
):
    from app.services import workspace_service
    from app.security.scope_enforcer import build_agent_context

    agent_id = agent["id"]
    workspace_scope = f"workspace:{workspace['id']}" if workspace else ""
    user_scope = f"user:{user_id}"
    agent_scope = f"agent:{agent_id}"
    context = build_agent_context(agent)
    read_scopes = context.read_scopes
    write_scopes = context.write_scopes
    workspace_ids = {
        scope.split(":", 1)[1]
        for scope in read_scopes + write_scopes
        if scope.startswith("workspace:") and ":" in scope
    }
    workspace_can_read = False
    workspace_can_write = False
    if workspace:
        workspace_can_read = workspace_service.can_user_read_workspace(user_id, workspace["id"])
        workspace_can_write = workspace_service.can_user_write_workspace(user_id, workspace["id"])
    enforcer = ScopeEnforcer(
        read_scopes,
        write_scopes,
        agent_id,
        active_workspace_ids=workspace_service.get_active_workspace_ids(workspace_ids),
    )
    checks = []

    checks.append(
        {
            "label": "Agent active",
            "status": "ok" if agent.get("is_active") else "blocked",
        }
    )
    if workspace:
        if workspace.get("is_active"):
            checks.append({"label": "Workspace active", "status": "ok"})
        else:
            checks.append({"label": "Workspace inactive", "status": "warning"})

        can_read_workspace = enforcer.can_read(workspace_scope) and workspace_can_read
        can_write_workspace = enforcer.can_write(workspace_scope) and workspace_can_write
        if can_read_workspace and can_write_workspace:
            checks.append({"label": "Workspace read/write access", "status": "ok"})
        elif can_read_workspace:
            from app.services import agent_access_service

            read_only = next(
                (n for n in agent_access_service.no_effect(agent)
                 if n["workspace"] == workspace["id"] and n["reason"] == "read_only_member"), None
            )
            checks.append({"label": read_only["message"] if read_only else "Workspace read-only access", "status": "warning"})
        else:
            from app.services import agent_access_service

            no_effect = next(
                (n for n in agent_access_service.no_effect(agent) if n["workspace"] == workspace["id"]), None
            )
            if no_effect:
                # The workspace is in the agent's lists but does nothing: say why, not "add it".
                checks.append({"label": no_effect["message"], "status": "blocked"})
            else:
                checks.append({"label": "No workspace access", "status": "blocked"})
                checks.append(
                    {
                        "label": "Recommended: add workspace scope to agent",
                        "status": "warning",
                    }
                )
    else:
        checks.append({"label": "No workspace selected", "status": "info"})

    can_read_user = enforcer.can_read(user_scope)
    can_write_user = enforcer.can_write(user_scope)
    if can_read_user and can_write_user:
        checks.append(
            {"label": "User preference read/write access", "status": "warning"}
        )
        checks.append(
            {"label": "Warning: user-scope write access granted", "status": "warning"}
        )
    elif can_read_user:
        checks.append({"label": "User preference read access", "status": "ok"})
    else:
        checks.append({"label": "Selected user is not this agent's owner", "status": "warning"})

    if enforcer.can_read(agent_scope) and enforcer.can_write(agent_scope):
        checks.append({"label": "Agent private scope", "status": "ok"})
    else:
        checks.append({"label": "Agent private scope incomplete", "status": "warning"})

    if workspace_scope and enforcer.can_read(workspace_scope):
        checks.append({"label": "Credential access (workspace scope)", "status": "ok"})
    elif enforcer.can_read("shared") or enforcer.can_read(user_scope):
        checks.append(
            {"label": "Credential access (user/shared scope)", "status": "ok"}
        )
    else:
        checks.append({"label": "Credential access", "status": "warning"})

    if agent.get("owner_user_id") == user_id or is_admin:
        checks.append({"label": "Activity tracking", "status": "ok"})
    else:
        checks.append({"label": "Activity tracking (limited)", "status": "warning"})

    checks.append(
        {
            "label": "Access comes from the workspaces this agent belongs to.",
            "status": "info",
        }
    )
    recommended_read = read_scopes + [agent_scope, user_scope]
    recommended_write = write_scopes + [agent_scope]
    if workspace_scope and workspace_can_read:
        recommended_read.append(workspace_scope)
    if workspace_scope and workspace_can_write:
        recommended_write.append(workspace_scope)
    recommended = {
        "read": sorted(set(recommended_read)),
        "write": sorted(set(recommended_write)),
    }
    return checks, recommended


@router.post("/api/integrations/preview")
async def preview_agent_setup(
    request: Request,
    session: dict = Depends(require_auth),
):
    from pydantic import BaseModel
    from app.services.agent_service import list_agents
    from app.services import workspace_service
    from app.database import get_db

    class PreviewBody(BaseModel):
        user_id: str
        workspace_id: str = ""
        agent_id: str
        target: str = "claude_code"
        output_type: str = "claude_md"

    body = PreviewBody.model_validate(await request.json())
    is_admin = session.get("role") == "admin"
    current_user_id = session["user_id"]

    if not is_admin and body.user_id != current_user_id:
        return error_response("FORBIDDEN", "Access denied", 403)

    with get_db() as conn:
        user_row = conn.execute(
            "SELECT id, email, display_name FROM users WHERE id = ?",
            (body.user_id,),
        ).fetchone()
    if not user_row:
        return error_response("NOT_FOUND", "User not found", 404)
    user = dict(user_row)

    workspace = None
    if body.workspace_id:
        workspace = workspace_service.get_workspace_by_id(body.workspace_id)
        if not workspace:
            return error_response("NOT_FOUND", "Workspace not found", 404)
        if not is_admin and not workspace_service.can_user_read_workspace(current_user_id, body.workspace_id):
            return error_response("FORBIDDEN", "Access denied", 403)

    agents = list_agents() if is_admin else list_agents(owner_user_id=current_user_id)
    agent = next(
        (a for a in agents if a["id"] == body.agent_id and a.get("is_active")), None
    )
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)
    if not _agent_user_matches(agent, body.user_id):
        return error_response(
            "AGENT_USER_MISMATCH",
            "Agents are tied to one owner/default user. Create a separate agent for this user and share workspace access through workspace scopes.",
            400,
        )

    access_checks, recommended = _agent_setup_access_model(
        agent=agent,
        workspace=workspace,
        user_id=body.user_id,
        is_admin=is_admin,
    )
    base_url = public_base_url(request)
    outputs = {}
    for output_type, _label, _filename in _agent_setup_output_options(body.target):
        _out_label, output = _build_agent_setup_output(
            user=user,
            workspace=workspace,
            agent=agent,
            target=body.target,
            output_type=output_type,
            base_url=base_url,
        )
        outputs[output_type] = output

    return success_response(
        {
            "recommended_scopes": recommended,
            "access_checks": access_checks,
            "outputs": outputs,
            "selected_output": outputs.get(
                body.output_type, outputs.get("instructions", "")
            ),
        }
    )


@router.post("/api/integrations/generate-connection")
async def generate_agent_connection(
    request: Request,
    session: dict = Depends(require_auth),
):
    from pydantic import BaseModel
    from app.services import agent_service, workspace_service, audit_service
    from app.database import get_db

    class GenerateConnectionBody(BaseModel):
        user_id: str
        workspace_id: str = ""
        agent_id: str
        target: str = "claude_code"
        output_type: str = "env"
        key_label: str = ""

    body = GenerateConnectionBody.model_validate(await request.json())
    if body.output_type not in KNOWN_OUTPUT_TYPES:
        return error_response(
            "INVALID_OUTPUT_TYPE",
            f"Unknown output type '{body.output_type}'. Use one of: {', '.join(sorted(KNOWN_OUTPUT_TYPES))}.",
            400,
        )
    is_admin = session.get("role") == "admin"
    current_user_id = session["user_id"]

    if not is_admin and body.user_id != current_user_id:
        return error_response("FORBIDDEN", "Access denied", 403)

    with get_db() as conn:
        user_row = conn.execute(
            "SELECT id, email, display_name FROM users WHERE id = ?",
            (body.user_id,),
        ).fetchone()
    if not user_row:
        return error_response("NOT_FOUND", "User not found", 404)
    user = dict(user_row)

    workspace = None
    if body.workspace_id:
        workspace = workspace_service.get_workspace_by_id(body.workspace_id)
        if not workspace:
            return error_response("NOT_FOUND", "Workspace not found", 404)
        if not is_admin and not workspace_service.can_user_read_workspace(current_user_id, body.workspace_id):
            return error_response("FORBIDDEN", "Access denied", 403)

    agent = agent_service.get_agent_by_id(body.agent_id)
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)
    if not agent.get("is_active"):
        return error_response(
            "AGENT_INACTIVE", "Cannot generate config for inactive agent", 400
        )
    if not is_admin and agent.get("owner_user_id") != current_user_id:
        return error_response("FORBIDDEN", "Access denied", 403)
    if not _agent_user_matches(agent, body.user_id):
        return error_response(
            "AGENT_USER_MISMATCH",
            "Agents are tied to one owner/default user. Create a separate agent for this user and share workspace access through workspace scopes.",
            400,
        )

    # Only add a key when the output actually carries one: generating a repo instruction file (AGENTS.md) must not
    # leave a key nobody holds. The new key sits beside the agent's others, so a machine already connected keeps
    # working; the person revokes what they no longer use on the Agents page.
    api_key = None
    key_record = None
    if body.output_type in KEY_BEARING_OUTPUTS:
        label = body.key_label.strip() or f"{_agent_setup_target_label(body.target)}, {utc_now_iso()[:10]}"
        try:
            key_record, shown_once = agent_service.add_agent_key(agent["id"], label, created_by=session["user_id"])
            api_key = shown_once
        except agent_service.AgentKeyError as exc:
            return error_response(exc.code, str(exc), 404 if exc.code == "NOT_FOUND" else 400)

    output_label, output = _build_agent_setup_output(
        user=user,
        workspace=workspace,
        agent=agent,
        target=body.target,
        output_type=body.output_type,
        base_url=public_base_url(request),
        api_key=api_key,
    )

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="agent_key_created" if api_key else "integration_output_generated",
        resource_type="agent",
        resource_id=agent["id"],
        result="success",
        details={
            **({"record_id": key_record["id"], "label": key_record["label"]} if key_record else {}),
            "user_id": body.user_id,
            "workspace_id": body.workspace_id,
            "target": body.target,
            "output_type": body.output_type,
        },
    )

    filename = next(
        (
            f
            for v, _l, f in _agent_setup_output_options(body.target)
            if v == body.output_type
        ),
        f"{APP_SLUG}-output.txt",
    )
    return success_response(
        {
            "agent_id": agent["id"],
            "output_type": body.output_type,
            "output_label": output_label,
            "filename": filename,
            "output": output,
            "api_key": api_key,
            "key_label": key_record["label"] if key_record else None,
            "warning": "This key is shown once. It is a new key: the agent's other keys keep working. Revoke keys you no longer use on the Agents page.",
        }
    )


HOOK_SCRIPTS = {
    "claude-code/agent_core_hook.py": Path(__file__).resolve().parents[2]
    / "integrations"
    / "claude-code"
    / "agent_core_hook.py",
    "codex/agent_core_hook.py": Path(__file__).resolve().parents[2]
    / "integrations"
    / "codex"
    / "agent_core_hook.py",
}


@router.get("/integrations/hooks/{name:path}")
def integration_hook_script(name: str):
    """Serve a client hook script. Public: the scripts carry no secrets and read
    the key from the client's own MCP configuration."""
    path = HOOK_SCRIPTS.get(name)
    if path is None or not path.is_file():
        return error_response("NOT_FOUND", "Unknown hook script", 404)
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/x-python")


def _output_section(label, text, file_name=None, key_button=""):
    """A labelled copy block. A file_name adds a Download button; a command does not need one."""
    download = (
        f"<button type='button' class='btn btn-sm btn-secondary' data-download-filename='{escape_html(file_name)}'>Download</button>"
        if file_name
        else ""
    )
    return (
        "<div class='output-section'>"
        # The key button comes first, in reading order, not below a screenful of config text.
        + (f"<div class='output-actions' style='margin-bottom:8px'>{key_button}</div>" if key_button else "")
        + (f"<div class='output-label'>{escape_html(label)}</div>" if label else "")
        + f"<pre class='output-block'>{escape_html(text)}</pre>"
        "<div class='output-actions'>"
        "<button type='button' class='btn btn-sm btn-secondary' onclick=\"copyGeneratedOutput(this)\">Copy</button>"
        f"{download}"
        "</div></div>"
    )


def _inline_code(text):
    """Escape a sentence and turn its `backtick` spans into <code>."""
    out, parts = [], escape_html(text).split("`")
    for i, part in enumerate(parts):
        out.append(f"<code>{part}</code>" if i % 2 else part)
    return "".join(out)


def _ago(stamp):
    """'3 hours ago' for a stored UTC timestamp, or '' when there is none."""
    if not stamp:
        return ""
    from app.time_utils import parse_utc_datetime, utc_now

    try:
        seconds = int((utc_now() - parse_utc_datetime(stamp)).total_seconds())
    except (TypeError, ValueError):
        return ""
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} minutes ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = seconds // 86400
    return f"{days} day{'s' if days != 1 else ''} ago"


def _agent_signals(agent_id, scope=None):
    """(last call, last hook start, last hand test, the OS of that hook start) for this agent.

    A session start hook asks over REST and names why the session started; the agent's own session_context
    call arrives over MCP. Only a REST start counts as the hook, so an MCP delivery never does. With a scope,
    only starts that resolved to that workspace count: another machine's hook for the same agent elsewhere
    is not this tool's. A session id beginning "try-" is the "Try it by hand" command, reported apart.
    """
    from app.database import get_db

    started = (
        "SELECT a.timestamp, json_extract(a.details_json, '$.client_os') FROM audit_log a "
        "LEFT JOIN agent_executions e ON e.id = a.resource_id "
        "WHERE a.actor_type = 'agent' AND a.actor_id = ? AND a.action = 'context_injected' "
        "AND json_extract(a.details_json, '$.channel') = 'rest' "
        "AND json_extract(a.details_json, '$.source') IN ('startup', 'clear', 'compact', 'resume')"
    )
    args = [agent_id]
    if scope:
        started += " AND json_extract(a.details_json, '$.memory_scope') = ?"
        args.append(scope)
    latest = " ORDER BY a.id DESC LIMIT 1"
    with get_db() as conn:
        seen = conn.execute(
            "SELECT MAX(timestamp) FROM audit_log WHERE actor_type = 'agent' AND actor_id = ?", (agent_id,)
        ).fetchone()[0]
        hooked = conn.execute(started + " AND COALESCE(e.host_session_ref, '') NOT LIKE 'try-%'" + latest, args).fetchone()
        by_hand = conn.execute(started + " AND COALESCE(e.host_session_ref, '') LIKE 'try-%'" + latest, args).fetchone()
    return seen, (hooked[0] if hooked else None), (by_hand[0] if by_hand else None), (hooked[1] if hooked else None)


def _step_status(ok, text):
    """A small chip beside a step's heading: green when Agent Core has seen it work, grey when not yet."""
    cls = "step-status step-status-ok" if ok else "step-status"
    return f"<span class='{cls}'>{escape_html(text)}</span>"


def _no_repo_folder_note(workspace):
    """Say so when the chosen workspace has no Repository folders: without one a session cannot be matched to it."""
    from app.services import workspace_service

    if workspace_service.get_repo_roots(workspace["id"]):
        return ""
    name = workspace.get("name") or workspace["id"]
    return (
        f"<p class='step-note step-warn'>{escape_html(name)} has no Repository folders yet, so a session cannot be matched to it by its folder. "
        "An admin adds one under Workspaces → Edit Workspace → Repository folders, one per machine where this project lives.</p>"
    )


def _hook_step_html(client_key, base_url, workspace=None):
    """The whole hook step, in place: install, register, trust, map; each with a copy block and an OS switch."""
    s = _hook_steps(client_key, base_url)

    def panels(per_os):
        html = ""
        for os_key, os_label in (("unix", "macOS / Linux"), ("windows", "Windows")):
            html += f"<div class='os-panel' data-os='{os_key}'><span class='os-label'>{os_label}</span>{per_os(os_key)}</div>"
        return html

    def install(os_key):
        return _output_section("", s["install"][os_key][1])

    def register(os_key):
        html = f"<p class='step-note'>{_inline_code(s['register_where'][os_key])}</p>"
        if os_key == "windows" and s["register_cmd"]:
            return html + _output_section("", s["register_cmd"])
        return html + _output_section("", s["register"][os_key])

    n = iter(range(1, 6))
    html = (
        f"<p>{_inline_code(s['intro'])}</p>"
        f"<p class='step-note'>Needs: {_inline_code(s['requirements'])}</p>"
        "<div class='os-switch' role='group' aria-label='Operating system' hidden>"
        "<button type='button' class='btn btn-sm btn-secondary' data-os-pick='unix'>macOS / Linux</button>"
        "<button type='button' class='btn btn-sm btn-secondary' data-os-pick='windows'>Windows</button></div>"
        f"<div class='hook-sub'><h4>c{next(n)} · Install the script</h4>{panels(install)}</div>"
        f"<div class='hook-sub'><h4>c{next(n)} · Register it</h4>{panels(register)}</div>"
    )
    if s["trust"]:
        html += (
            f"<div class='hook-sub'><h4>c{next(n)} · Trust it once</h4><p class='step-note'>{_inline_code(s['trust'])}</p>"
            f"<div class='os-panel' data-os='windows'><p class='step-note'>{_inline_code(s['trust_windows'])}</p></div></div>"
        )
    html += (
        f"<div class='hook-sub'><h4>c{next(n)} · Map your project folder to a workspace</h4>"
        + (_no_repo_folder_note(workspace) if workspace else "")
        + f"<p class='step-note'>{_inline_code(s['mapping'])}</p></div>"
    )
    return f"<div class='hook-steps'>{html}</div>"


@router.get("/integrations")
def integrations_page(
    request: Request,
    session: dict = Depends(require_auth),
):
    from app.services.agent_service import list_agents
    from app.services import workspace_service
    from app.database import get_db

    is_admin = session.get("role") == "admin"
    current_user_id = session["user_id"]

    user_id = request.query_params.get("user_id", current_user_id)
    workspace_id = request.query_params.get("workspace_id", "")
    agent_id = request.query_params.get("agent_id", "")
    target = request.query_params.get("target", "generic_mcp")
    output_type = request.query_params.get("output_type", "instructions")

    users = []
    with get_db() as conn:
        if is_admin:
            rows = conn.execute(
                "SELECT id, email, display_name FROM users ORDER BY display_name"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, email, display_name FROM users WHERE id = ?",
                (current_user_id,),
            ).fetchall()
    users = [
        {"id": r["id"], "email": r["email"], "display_name": r["display_name"]}
        for r in rows
    ]
    users.sort(
        key=lambda user: (
            user["id"] != current_user_id,
            *_label_sort_key(user, "display_name"),
        )
    )

    workspaces = (
        workspace_service.list_workspaces()
        if is_admin
        else workspace_service.list_workspaces(owner_user_id=current_user_id)
    )
    workspaces.sort(key=lambda workspace: _label_sort_key(workspace, "name"))
    if not workspace_id and "workspace_id" not in request.query_params and len(workspaces) == 1:
        workspace_id = workspaces[0]["id"]  # one workspace: that is the one, no choosing
    project_options = "".join(
        f'<option value="{p["id"]}" {"selected" if p["id"] == workspace_id else ""}>{escape_html(p["name"])}</option>'
        for p in workspaces
    )

    agents = list_agents() if is_admin else list_agents(owner_user_id=current_user_id)
    visible_agents = {a["id"]: a for a in agents if a.get("is_active")}
    agent_options = "".join(
        f'<option value="{a["id"]}" {"selected" if a["id"] == agent_id else ""}>{escape_html(a.get("display_name", a["id"]))}</option>'
        for a in sorted(
            visible_agents.values(), key=lambda agent: _label_sort_key(agent, "display_name")
        )
    )

    user_options = "".join(
        f'<option value="{u["id"]}" {"selected" if u["id"] == user_id else ""}>{escape_html(u.get("display_name", u["id"]))}</option>'
        for u in users
    )

    access_checks = []
    tool_ctx = None
    generated_output = ""
    output_label = ""
    base_url = public_base_url(request)
    page_path = request.url.path
    recommended_scopes = None

    if agent_id:
        agent = visible_agents.get(agent_id)
        workspace = (
            next((p for p in workspaces if p["id"] == workspace_id), None)
            if workspace_id
            else None
        )
        user = next((u for u in users if u["id"] == user_id), None)

        if agent and agent.get("is_active"):
            access_checks.append({"label": "Agent active", "status": "ok"})
        elif agent:
            access_checks.append({"label": "Agent inactive", "status": "blocked"})
        else:
            access_checks.append({"label": "Agent not found", "status": "blocked"})

        if not workspace_id:
            access_checks.append(
                {"label": "Workspace optional for this output", "status": "info"}
            )
        elif workspace and workspace.get("is_active"):
            access_checks.append({"label": "Workspace active", "status": "ok"})
        elif workspace:
            access_checks.append({"label": "Workspace inactive", "status": "warning"})
        else:
            access_checks.append({"label": "Workspace not found", "status": "blocked"})

        if agent:
            access_checks, recommended_scopes = _agent_setup_access_model(
                agent=agent,
                workspace=workspace,
                user_id=user_id,
                is_admin=is_admin,
            )

        if agent and user:
            tool_ctx = (user, workspace, agent)
            output_label, generated_output = _build_agent_setup_output(
                user=user,
                workspace=workspace,
                agent=agent,
                target=target,
                output_type=output_type,
                base_url=base_url,
            )

    checks_html = ""
    if access_checks:
        checks_html = "<div class='access-checks'>"
        for check in access_checks:
            cls = {
                "ok": "check-ok",
                "warning": "check-warn",
                "blocked": "check-blocked",
                "info": "check-info",
            }[check["status"]]
            icon = {
                "ok": "&#10003;",
                "warning": "&#9888;",
                "blocked": "&#10007;",
                "info": "&#8505;",
            }[check["status"]]
            checks_html += f"<div class='{cls}'><span class='check-icon'>{icon}</span>{escape_html(check['label'])}</div>"
        checks_html += "</div>"
        if any(c["status"] in ("warning", "blocked") for c in access_checks):
            checks_html += (
                "<p class='text-muted' style='font-size:0.8rem;margin-top:8px'>Something this agent needs is missing.</p>"
                "<button type='button' class='btn btn-sm' id='apply-access-btn' onclick='applyRecommendedAccess()'>Give access</button> "
                "<span id='apply-status' class='text-muted' style='font-size:0.85rem'></span>"
            )
        else:
            checks_html = (
                "<div class='access-checks'><div class='check-ok'><span class='check-icon'>&#10003;</span>Everything this agent needs is in place</div></div>"
                "<details class='access-details' style='margin-top:8px'><summary>Show the checks</summary>"
                + checks_html
                + "</details>"
            )

    current_params = {
        "user_id": user_id,
        "workspace_id": workspace_id,
        "agent_id": agent_id,
    }
    tool_selected = "target" in request.query_params
    if tool_selected:
        current_params["target"] = target
    from urllib.parse import urlencode

    def output_tabs(options):
        tabs = ""
        for value, label, _filename in options:
            params = dict(current_params)
            params["output_type"] = value
            active = "active" if value == output_type else ""
            tabs += (
                f'<a class="setup-tab {active}" '
                f'href="{page_path}?{urlencode(params)}">{escape_html(label)}</a>\n'
            )
        return tabs

    # What the guided steps already show for the chosen tool is not repeated under Other outputs.
    shown_in_steps = set()
    _chosen = next((t for t in SETUP_TOOLS if t[0] == target), None) if tool_selected and tool_ctx else None
    if _chosen:
        shown_in_steps = {_chosen[3], "verification"}
        if target in _HOOK_CLIENTS:
            shown_in_steps.add(_HOOK_CLIENTS[target]["output_type"])
    other_options = [o for o in _agent_setup_output_options(target) if o[0] not in shown_in_steps]
    other_tabs = output_tabs(other_options)
    filename = next(
        (f for v, _l, f in _agent_setup_output_options(target) if v == output_type),
        f"{APP_SLUG}-output.txt",
    )

    if generated_output:
        output_display = (
            f"<pre class='output-block'>{escape_html(generated_output)}</pre>"
        )
        copy_btn = "<button type='button' class='btn btn-sm btn-secondary' onclick=\"copyGeneratedOutput(this)\">Copy</button>"
        download_btn = (
            f"<button type='button' class='btn btn-sm btn-secondary'"
            f" data-download-filename='{escape_html(filename)}'>Download</button>"
        )
        regenerate_btn = "<button type='submit' class='btn btn-sm btn-secondary'>Refresh Preview</button>"
        connection_label = (
            "Generate One-Time Key + MCP Config"
            if output_type == "mcp_json"
            else "Generate One-Time Key + Environment Variables"
            if output_type == "env"
            else "Generate One-Time Key + Assistants Prompt"
        )
        connection_btn = (
            f"<button type='button' class='btn btn-sm' id='generate-connection-btn' data-label='{escape_html(connection_label)}' data-target='{escape_html(target)}' data-output-type='{escape_html(output_type)}' onclick='generateConnectionConfig(this)'>{escape_html(connection_label)}</button>"
            if output_type in ("env", "mcp_json", "assistants_md")
            else ""
        )
    else:
        output_display = "<div class='empty'>Select a user and agent to generate setup output. Select a workspace only when you want workspace-specific memory guidance.</div>"
        copy_btn = ""
        download_btn = ""
        regenerate_btn = ""
        connection_btn = ""

    destination_guidance = _get_destination_guidance(target, output_type)

    access_check_section = (
        f'<div class="form-section"><h2>Access Check</h2>{checks_html}</div>'
        if checks_html
        else ""
    )
    destination_section = (
        f'<div class="form-section"><h2>Destination</h2><p>{destination_guidance}</p></div>'
        if destination_guidance
        else ""
    )
    output_label_html = (
        f"<div class='output-label'>{output_label}</div>" if output_label else ""
    )

    _section = _output_section

    tool_links = "".join(
        f'<a class="setup-tab {"active" if tool_selected and t[0] == target else ""}" '
        f'href="{page_path}?{urlencode({**{k: v for k, v in current_params.items() if k != "target"}, "target": t[0]})}#tool-step">{escape_html(t[1])}</a>\n'
        for t in SETUP_TOOLS
    )
    selected_tool = next((t for t in SETUP_TOOLS if t[0] == target), None) if tool_selected else None
    tool_steps_html = ""
    verification_html = ""
    if tool_ctx:
        u_, w_, a_ = tool_ctx
        _vl, _vt = _build_agent_setup_output(
            user=u_, workspace=w_, agent=a_, target=target, output_type="verification", base_url=base_url
        )
        verification_html = _section(
            "Verification Prompt — run it in the connected tool", _vt, f"{APP_SLUG}-verification.md"
        )
    if selected_tool and tool_ctx:
        _key, _name, connect_where, file_type, file_label, file_name, file_where = selected_tool
        c_label, c_text = _build_agent_setup_output(
            user=u_, workspace=w_, agent=a_, target=target, output_type="mcp_tool", base_url=base_url
        )
        f_label, f_text = _build_agent_setup_output(
            user=u_, workspace=w_, agent=a_, target=target, output_type=file_type, base_url=base_url
        )
        connect_btn = (
            "<button type='button' class='btn btn-sm' id='generate-tool-connection-btn' "
            f"data-label='Generate One-Time Key + {escape_html(_name)} MCP Config' data-target='{escape_html(target)}' "
            "data-output-type='mcp_tool' onclick='generateConnectionConfig(this)'>"
            f"Generate One-Time Key + {escape_html(_name)} MCP Config</button>"
        )
        seen_at, hooked_at, by_hand_at, hooked_os = _agent_signals(a_["id"], f"workspace:{w_['id']}" if w_ else None)
        connect_status = (
            _step_status(True, f"Connected: last seen {_ago(seen_at)}")
            if seen_at
            else _step_status(False, "No connection seen yet")
        )
        hook_item = ""
        if target in _HOOK_CLIENTS:
            if hooked_at:
                hook_status = _step_status(
                    True, f"Hook working: context delivered {_ago(hooked_at)}" + (f" from {hooked_os}" if hooked_os else "")
                )
            elif by_hand_at:
                hook_status = _step_status(False, f"Run by hand {_ago(by_hand_at)}; the tool has not started it yet")
            else:
                hook_status = _step_status(False, "No session start seen yet")
            hook_item = (
                f"<li><h3>Add the session start hook {hook_status}</h3>"
                f"{_hook_step_html(target, base_url, w_)}</li>"
            )
        yourself = (
            "Do steps a and c yourself, in a terminal. " if target in _HOOK_CLIENTS else "Do step a yourself, in a terminal. "
        ) + (
            "An agent usually cannot: it runs in a sandbox without network access and cannot write to its own settings folder. "
            + (f"Step b is a file in your project, so {_name} can save that one for you." if file_type in ("claude_md", "agents_md") else "")
        )
        # Only an agent Core has already seen can be connected somewhere else; a new key would disconnect that machine.
        second_machine = (
            "<p class='step-note'>Already connected on another machine? Copy that machine's entry, or generate a key for this one: the other machine's key keeps working. Change its address if it says localhost.</p>"
            if seen_at
            else ""
        )
        tool_steps_html = (
            f"<p class='step-callout'>{escape_html(yourself.strip())}</p>"
            '<ol class="setup-steps">'
            f"<li><h3>Connect it {connect_status}</h3><p>{connect_where}</p>{second_machine}"
            f"{_section(c_label, c_text, f'{APP_SLUG}-mcp-config.txt', connect_btn)}</li>"
            f"<li><h3>Give it instructions</h3><p>{file_where}</p>{_section(f_label, f_text, file_name)}</li>"
            f"{hook_item}"
            "</ol>"
        )
    elif selected_tool:
        tool_steps_html = "<div class='empty'>Choose an agent in step 1 to see what to copy.</div>"
    else:
        tool_steps_html = "<p class='form-hint'>Choose your tool to see exactly what to copy, in order.</p>"
    other_open = "open" if "output_type" in request.query_params else ""
    target_hidden = f'<input type="hidden" name="target" value="{escape_html(target)}">' if tool_selected else ""
    body = f"""
    <div class="page-header setup-page-header">
      <h1>Connect</h1>
      <p class="subtitle">Generate setup instructions for connecting an AI tool such as Claude Code, Cursor or Codex to {APP_NAME} as one of your agents. Choose who is connecting, then copy the result into the tool.</p>
    </div>

    <form method="get" action="{page_path}" class="setup-form" onsubmit="saveSetupScrollPosition()">
      {target_hidden}
      <div class="form-section">
        <h2>1. Who is connecting?</h2>
        <div class="form-row">
          <div class="form-group">
            <label for="user_id">User</label>
            <select id="user_id" name="user_id" onchange="this.form.submit()">
              {user_options}
            </select>
            <p class="form-hint">Whose personal memory the tool can read.</p>
          </div>
          <div class="form-group">
            <label for="workspace_id">Workspace <span class="text-muted">(optional)</span></label>
            <select id="workspace_id" name="workspace_id" onchange="this.form.submit()">
              <option value="">-- Optional --</option>
              {project_options}
            </select>
            <p class="form-hint">The shared space the tool saves team memory in. Without one, the tool is told to ask.</p>
          </div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label for="agent_id">Agent</label>
            <select id="agent_id" name="agent_id" onchange="this.form.submit()">
              <option value="">-- Select agent --</option>
              {agent_options}
            </select>
            <p class="form-hint">The agent identity the tool signs in as. It decides what the tool may read and write.</p>
            <p class="form-hint">Choosing here only updates the preview. Nothing is issued until you press <strong>Generate One-Time Key</strong>, which adds a new key to that agent; its other keys keep working. The instruction files and prompts are the same for every agent and safe to regenerate.</p>
          </div>
        </div>
      </div>

      {access_check_section}

      <div class="form-section" id="tool-step">
        <h2>2. Which tool?</h2>
        <div class="setup-tabs">{tool_links}</div>
        {tool_steps_html}
        <div class="alert alert-warning" id="connection-warning" style="display:none"></div>
      </div>

      <div class="form-section" id="check-step">
        <h2>3. Check it works</h2>
        <div class="connection-doctor" id="connection-doctor">
          <div>
            <h3>Connection doctor</h3>
            <p>Use the exact MCP URL below, not the site root. This check never stores the key.</p>
            <code>{escape_html(base_url)}/mcp</code>
          </div>
          <button type="button" class="btn btn-sm btn-secondary" id="test-connection-details-btn" onclick="testConnectionDetails()">Test connection details</button>
        </div>
        <div class="connection-doctor-result" id="connection-doctor-result" aria-live="polite"></div>
        {verification_html}
      </div>

      <details class="card fold-card" id="other-outputs" {other_open}>
        <summary><span class="fold-title">Other outputs</span><span class="fold-hint">Environment variables, session prompt, the full MCP config, files for other tools</span></summary>
        <p class="form-hint">Only what the steps above do not already give you. Where a tab offers Generate One-Time Key, pressing it adds a new key to the agent; its other keys keep working. Revoke keys you no longer use on the Agents page.</p>
        {destination_section}
        <div class="form-section" id="generated-output">
        <div class="setup-tabs">{other_tabs}</div>
        <div class="output-section">
        {output_label_html}
        {output_display}
        <div class="output-actions">{connection_btn}{copy_btn}{download_btn}{regenerate_btn}</div>
        </div>
        </div>
      </details>
    </form>

    """

    return render_page(
        "Connect", body, page_path, _agent_setup_extra_js(base_url), session=session
    )


def _agent_setup_extra_js(base_url):
    # Server-derived constants the static integrations.js reads off window;
    # the rest of the JS lives in /static/js/integrations.js (browser-cached).
    bracket = "{{" + ENV_PREFIX + "API_KEY}}"
    angle = "<" + ENV_PREFIX + "API_KEY>"
    constants = (
        "<script>"
        f"window._AC_APP_SLUG = {json.dumps(APP_SLUG)};"
        f"window._AC_API_KEY_BRACKET = {json.dumps(bracket)};"
        f"window._AC_API_KEY_ANGLE = {json.dumps(angle)};"
        f"window._AC_MCP_URL = {json.dumps(f'{base_url}/mcp')};"
        "</script>"
    )
    return constants + '<script src="/static/js/integrations.js?v=20261117"></script>'


# Outputs that embed an agent key, and therefore add a new one.
# Everything else — the repo instruction files and the prompts — is guidance
# that carries no credential.
# Every output type `_build_agent_setup_output` knows. An unknown name used to fall through to the
# verification prompt without saying so, so a mistyped type looked like it had worked.
KNOWN_OUTPUT_TYPES = frozenset({
    "instructions", "session", "claude_md", "agents_md", "assistants_md", "mcp_json", "mcp_tool",
    "cursor_mcp_json", "windsurf_mcp_json", "env", "claude_code_hook", "codex_hook", "verification",
})

KEY_BEARING_OUTPUTS = frozenset(
    {"env", "mcp_json", "mcp_tool", "cursor_mcp_json", "windsurf_mcp_json", "assistants_md"}
)


def _build_agent_setup_output(
    user,
    workspace,
    agent,
    target,
    output_type,
    base_url,
    api_key=None,
):
    """Build one generated file for this agent; the tool lists in it are the agent's own."""
    from app.services import agent_service

    caps = agent.get("capabilities")
    if caps is None and "capabilities_json" in agent:
        caps = agent_service.parse_capabilities(agent.get("capabilities_json"))
    token = _LISTING_CAPABILITIES.set(None if caps is None else frozenset(caps))
    try:
        return _build_agent_setup_output_for(user, workspace, agent, target, output_type, base_url, api_key)
    finally:
        _LISTING_CAPABILITIES.reset(token)


def _build_agent_setup_output_for(
    user,
    workspace,
    agent,
    target,
    output_type,
    base_url,
    api_key=None,
):
    user_scope = f"user:{user['id']}"
    workspace_scope = f"workspace:{workspace['id']}" if workspace else ""
    default_scope = workspace_scope or user_scope
    agent_scope = f"agent:{agent['id']}"
    agent_display = agent.get("display_name", agent["id"])
    user_display = user.get("display_name", user.get("email", user["id"]))
    workspace_name = (
        workspace.get("name", workspace["id"]) if workspace else "No workspace selected"
    )

    if output_type == "instructions":
        label = "Instructions"
        content = _build_user_instructions(
            target,
            base_url,
            user_scope,
            workspace_scope,
            agent_scope,
            agent_display,
            user_display,
            workspace_name,
        )
    elif output_type == "session":
        label = "Session Prompt"
        content = _build_session_prompt(
            target,
            base_url,
            user_scope,
            workspace_scope,
            agent_scope,
            agent_display,
            user_display,
            workspace_name,
        )
    elif output_type == "claude_md":
        label = "CLAUDE.md — paste into workspace repository root"
        content = _build_claude_md(
            base_url,
            user_scope,
            workspace_scope,
            agent_scope,
            agent_display,
            workspace_name,
        )
    elif output_type == "agents_md":
        label = "AGENTS.md — paste into workspace repository root"
        content = _build_agents_md(
            base_url, user_scope, workspace_scope, agent_scope, workspace_name
        )
    elif output_type == "assistants_md":
        label = "Assistants — paste into the assistant's onboarding or instruction field"
        content = _build_assistants_md(
            base_url, user_scope, workspace_scope, agent_scope, api_key=api_key,
            default_recall_scopes_json=agent.get("default_recall_scopes_json"),
        )
    elif output_type == "mcp_json":
        label = "MCP Config"
        content = _build_mcp_json(base_url, api_key)
    elif output_type == "mcp_tool":
        # The MCP config for the chosen tool only; the "Other outputs" tab keeps the whole text.
        label = f"MCP Config ({_agent_setup_target_label(target)})"
        content = _build_mcp_json(base_url, api_key, tool=target)
    elif output_type == "cursor_mcp_json":
        label = "MCP Config (Cursor)"
        content = _build_cursor_mcp_json(base_url, api_key)
    elif output_type == "windsurf_mcp_json":
        label = "MCP Config (Windsurf)"
        content = _build_windsurf_mcp_json(base_url, api_key)
    elif output_type == "env":
        label = "Environment Variables"
        content = _build_env_vars(
            base_url, agent["id"], user_scope, workspace_scope, api_key
        )
    elif output_type == "claude_code_hook":
        label = "Claude Code Hook"
        content = _build_claude_code_hook(base_url)
    elif output_type == "codex_hook":
        label = "Codex Hook"
        content = _build_codex_hook(base_url)
    else:
        label = "Verification Prompt"
        content = _build_verification_prompt(workspace_scope or agent_scope)

    return label, content


def _build_instructions(
    target,
    base_url,
    user_scope,
    workspace_scope,
    agent_scope,
    agent_display,
    user_display,
    workspace_name,
):
    scope_guide = ""
    tool_tips = ""
    default_scope = workspace_scope or user_scope
    workspace_scope_label = workspace_scope or "No workspace scope selected"
    workspace_context_line = (
        f"- Use `{workspace_scope}` for workspace facts, decisions, implementation notes, bugs, and architecture."
        if workspace_scope
        else f"- No workspace selected. Use `{user_scope}` for user-level context and `{agent_scope}` for private scratch context."
    )
    if target == "claude_code":
        scope_guide = f"{workspace_context_line}\n- Use `{user_scope}` only for stable preferences or personal working context.\n- Use `{agent_scope}` only for private scratch context."
        tool_tips = f"""## Claude Code Tips

- Claude Code automatically reads `CLAUDE.md` in the workspace root. Consider generating that output instead for a self-contained file.
- Claude Code will inherit the scopes from your {APP_NAME} agent configuration. The scopes above are informational.
- For best results, set `{ENV_PREFIX}API_KEY` in your shell environment before starting Claude Code:
    export {ENV_PREFIX}API_KEY="your-key-here"
"""
    elif target == "codex":
        scope_guide = f"- Work in `{default_scope}` for default context.\n- Read `{user_scope}` for user preferences.\n- Keep private notes in `{agent_scope}`."
        tool_tips = f"""## Codex Tips

- Codex reads `AGENTS.md` in the workspace root. Consider generating that output instead for a self-contained file.
- Codex can use both the MCP tools and the REST API. The MCP endpoint is preferred for memory operations.
- Set `{ENV_PREFIX}API_KEY` in your environment before starting a Codex session.
"""
    elif target == "cursor":
        scope_guide = f"{workspace_context_line}\n- Use `{user_scope}` only for stable preferences or personal working context.\n- Use `{agent_scope}` only for private scratch context."
        tool_tips = f"""## Cursor Tips

- Add the MCP config to `.cursor/mcp.json` in the workspace root for workspace-level access, or `~/.cursor/mcp.json` for global access.
- After adding the MCP config, run the "Reload MCP Servers" command or restart Cursor.
- Cursor's AI chat can use MCP tools directly once the server is connected. Set `{ENV_PREFIX}API_KEY` in Cursor's terminal or your shell profile.
"""
    elif target == "windsurf":
        scope_guide = f"{workspace_context_line}\n- Use `{user_scope}` only for stable preferences or personal working context.\n- Use `{agent_scope}` only for private scratch context."
        tool_tips = f"""## Windsurf Tips

- Add the MCP config to Windsurf's MCP settings for your workspace.
- Windsurf may require a restart after adding MCP servers.
- Set `{ENV_PREFIX}API_KEY` in your shell environment before starting a Windsurf session.
"""
    elif target == "antigravity":
        scope_guide = f"{workspace_context_line}\n- Use `{user_scope}` only for stable preferences or personal working context.\n- Use `{agent_scope}` only for private scratch context."
        tool_tips = f"""## Antigravity Tips

- Add the MCP config using the `serverUrl` field instead of `url`.
- Restart Antigravity after adding MCP servers if the config does not appear immediately.
- Set `{ENV_PREFIX}API_KEY` in your shell environment before starting an Antigravity session.
"""
    else:
        scope_guide = f"- Default memory scope: `{default_scope}`\n- User scope: `{user_scope}`\n- Private scope: `{agent_scope}`"
        tool_tips = f"""## Generic MCP Client Tips

- The MCP endpoint is `{base_url}/mcp`. Your client should send requests as JSON with `{{"tool": "...", "params": {{...}}}}`.
- Authenticate using `Authorization: Bearer ${ENV_PREFIX}API_KEY` header or your client's equivalent auth mechanism.
- Available tools: {_mcp_tool_listing()}.
"""

    context = _compact_workspace_context(base_url, user_scope, workspace_scope, agent_scope, workspace_name)
    return f"""{context}
User: {user_display}. Agent: {agent_display}.

{tool_tips}"""


def _build_user_instructions(
    target,
    base_url,
    user_scope,
    workspace_scope,
    agent_scope,
    agent_display,
    user_display,
    workspace_name,
):
    workspace_line = (
        f"This setup is workspace-aware. Generated prompts use `{workspace_scope}` for workspace facts, decisions, and shared collaboration."
        if workspace_scope
        else f"No workspace is selected. Generated prompts use `{user_scope}` as read-only owner context and keep shared facts/decisions in the default shared scope."
    )
    return f"""# {APP_NAME} Instructions

Use these steps to connect an AI tool to {APP_NAME} for {user_display}.

## What To Generate

1. Generate `MCP Config` when you are ready to connect the tool to {APP_NAME}. This is the normal connection step for MCP-capable tools.
2. Generate `Environment Variables` only when your MCP config or launcher reads values from environment variables.
3. Generate `CLAUDE.md`, `AGENTS.md`, or `Assistants` guidance for reusable repository-level or agent-level instructions.
4. Generate `Session Prompt` when you want one-time agent-specific instructions pasted into a chat/session.
5. Generate `Verification Prompt` after setup and paste it into the connected agent to run the end-to-end verification flow.

Prompts can steer behavior and verify connectivity. They cannot install MCP servers, plugins, or skills by themselves.

## Connection Key

Click the one-time key button on `MCP Config` when you are ready to connect the tool.
Use the one-time key button on `Environment Variables` only when you need shell or launcher environment variables.
That adds a new key for this agent and inserts it into the generated output.
The key is shown once. The agent's other keys keep working; revoke keys you no longer use on the Agents page.
The API key is the authoritative agent identity. {APP_NAME} identifies requests as `{agent_scope}` by looking up the bearer token; repo instruction files do not set identity.

## Where Things Go

- MCP Config belongs in the MCP configuration location for the tool you are connecting. For Claude Code, run `claude mcp add` (CLI) or create `.mcp.json` in the repo root; for Codex CLI, that is `~/.codex/config.toml`; for OpenCode, add the OpenCode block under `mcp` in `~/.config/opencode/opencode.json`.
- Environment variables belong in your shell profile, launcher, service environment, or tool-specific environment settings. They do not connect {APP_NAME} by themselves.
- Session Prompt is pasted into the first message or custom instructions for a single session. Use it to bootstrap behavior when no plugin or hook layer exists.
- `CLAUDE.md` and `AGENTS.md` belong in the workspace/repository root when you want persistent per-repository behavior. `Assistants` guidance is for assistant-style agents that manage their own config. These files are workspace-centric and can be shared by Codex, OpenCode, Claude Code, and other agents using their own MCP keys.

## Selected Context

- {APP_NAME} URL: `{base_url}`
- Connection agent for generated MCP/env output: `{agent_scope}`
- User: `{user_scope}`
- Workspace: `{workspace_scope or "optional / not selected"}`

Use the full prefixed scope names exactly as shown. Do not use plain workspace IDs like `{workspace_name}` or agent IDs as memory scopes.

{workspace_line}

If the client supports hooks or plugins, they can automate some of these behaviors. If not, this session prompt should be used to seed the same expectations manually.
"""


def _build_session_prompt(
    target,
    base_url,
    user_scope,
    workspace_scope,
    agent_scope,
    agent_display,
    user_display,
    workspace_name,
):
    default_scope = workspace_scope or user_scope
    tool_line = {
        "claude_code": "You are Claude Code.",
        "codex": "You are Codex.",
        "cursor": "You are Cursor's AI agent.",
        "windsurf": "You are Windsurf's AI agent.",
    }.get(target, "You are an MCP-capable AI agent.")
    context = _compact_workspace_context(base_url, user_scope, workspace_scope, agent_scope, workspace_name)
    return f"""{tool_line} Paste-in prompt for this session.

{context}"""


def _compact_workspace_context(base_url, user_scope, workspace_scope, agent_scope, workspace_name, extra=""):
    """What is specific to this workspace, and nothing the MCP server already says.

    The tools, their parameters and the memory classes are described by the MCP server's own
    instructions and tool descriptions, so a generated file only needs the scopes, the session rhythm
    and the one rule that decides whether a memory is worth writing.
    """
    default_scope = workspace_scope or agent_scope
    session_line = (
        f"- Each session, before your other Agent Core tools: if your context already holds an \"{APP_NAME} session context\" block (a SessionStart hook injects it), use it; otherwise call `session_context` with `memory_scope` `{workspace_scope}`. It gives the `execution_id` to pass to `activity_update` and `memory_write`."
        if workspace_scope
        else f"- Each session, before your other Agent Core tools: call `session_context` with `memory_scope` `{agent_scope}`: it lists your open work, recent results and key memories, and gives the `execution_id` to pass to `activity_update` and `memory_write`. With no workspace you see only your own work; ask the owner for a workspace to share with other agents."
    )
    scope_line = (
        f"- Workspace: `{workspace_scope}`: read and write its memory and tasks. Preferences of your owner: `{user_scope}` (read). Private scratch only: `{agent_scope}`."
        if workspace_scope
        else f"- No workspace is selected, so work and notes go in your private scope `{agent_scope}`; ask the owner for a workspace before saving anything others should see. Preferences of your owner: `{user_scope}` (read)."
    )
    return f"""# {APP_NAME}: {workspace_name}

{APP_NAME} ({base_url}) is this workspace's shared memory and task log. Your MCP connection supplies the tools and how to use them; this file says only what is specific here.

{scope_line}
{session_line}
- Order: the user's message, project instructions and any starting command come first; do bookkeeping after or beside your first answer and never mention it unless asked. Session context is background, not a resumed conversation; use its open work only if relevant.
- Work: open a task with `activity_update` (`status: active`, `memory_scope: {default_scope}`); a short note every few minutes keeps it alive. Close it with a one- or two-sentence `task_result` saying what was done and where. Put a request to a person in the handoff's `needs_human`.
- Before deciding something earlier sessions may have settled, `memory_search` `{default_scope}`; if it finds nothing, retry with exact words.
- {MEMORY_WRITE_WHEN}
- Secrets: `credential_get` returns an `{CREDENTIAL_PREFIX}*` reference; never ask for or print a raw secret. If one is missing, `credential_request`.
- Use full prefixed scope names exactly as shown.{extra}
"""


def _build_claude_md(
    base_url, user_scope, workspace_scope, agent_scope, agent_display, workspace_name
):
    return _compact_workspace_context(
        base_url, user_scope, workspace_scope, agent_scope, workspace_name,
        extra=(
            f"\n- Claude Code defers MCP schemas: before the first {APP_NAME} call in a session, load them with "
            f"`ToolSearch(\"select:mcp__{APP_SLUG}__session_context,mcp__{APP_SLUG}__activity_update,"
            f"mcp__{APP_SLUG}__memory_search,mcp__{APP_SLUG}__memory_write\")`. Never put an API key in this file."
        ),
    )


def _build_agents_md(
    base_url, user_scope, workspace_scope, agent_scope, workspace_name
):
    return _compact_workspace_context(base_url, user_scope, workspace_scope, agent_scope, workspace_name)


def _build_assistants_md(base_url, user_scope, workspace_scope, agent_scope, api_key=None, default_recall_scopes_json=None):
    # Operational writes can use the selected workspace or the agent's private
    # scope. Durable, shareable knowledge always belongs in a workspace.
    durable_scope = workspace_scope or agent_scope
    durable_label = (
        f"`{workspace_scope}`"
        if workspace_scope
        else "your assigned workspace (ask the owner to create or grant one, not your private agent scope)"
    )
    durable_guidance = (
        f"Write durable, shareable memory to `{workspace_scope}`."
        if workspace_scope
        else (
            "No workspace is selected. Ask the owner to create or grant one before "
            "storing shared or owner-facing facts. Keep only private notes in "
            f"`{agent_scope}` until then."
        )
    )
    recall_list = None
    if default_recall_scopes_json:
        try:
            parsed_recall = json.loads(default_recall_scopes_json)
            if isinstance(parsed_recall, list) and parsed_recall:
                recall_list = parsed_recall
        except (TypeError, ValueError):
            recall_list = None
    if recall_list:
        recall_intro = (
            "Your default recall scopes are "
            + ", ".join(f"`{s}`" for s in recall_list)
            + ". An unscoped `memory_search` or `memory_get` searches only these scopes."
        )
    else:
        recall_intro = f"Use `{durable_scope}` for current work and `{user_scope}` for owner facts."
    connection_key = _connection_key_value(api_key)
    workspace_context_line = (
        f"- Workspace scope: `{workspace_scope}` (use this for shared collaboration in the selected workspace)."
        if workspace_scope
        else ""
    )
    return f"""# {APP_NAME} Assistant Onboarding

Use {APP_NAME} for memory, task tracking, and stored credentials.

## Connect

- **MCP URL:** {base_url}/mcp
- **Bearer token:** {connection_key}

For Hermes, add this server as `mcp_servers.agent_core` in `$HERMES_HOME/config.yaml` or `~/.hermes/config.yaml`. Other assistants should use their normal MCP configuration. Then reload or restart your agent and confirm the server is available. Use the one-time key button only when you need a fresh token.

Your MCP key determines your active user and agent identity. Do not write API keys to instruction files.

## Scopes

{workspace_context_line}
{durable_guidance}
Read `{user_scope}` for stable owner preferences and context. It is read-only unless explicitly granted write access. Use `{agent_scope}` only for private scratch notes.

{recall_intro} An unscoped search stays in those scopes. Do not fan recall across other workspaces. Search another `workspace:<id>` on demand, only when the request is explicitly about that project.

## Work with Agent Core

1. At session start, if your context already contains an "{APP_NAME} session context" block (a SessionStart hook injects it), reuse its `execution_id` and skip the startup sync. Otherwise call `session_context` once for `{durable_scope}`, and reuse its `execution_id`. Later `workspace_sync` calls return only what changed since; acknowledge each processed page with `workspace_sync_ack` and continue until `has_more` is false.
2. Before a meaningful task, call `activity_update` with `memory_scope` set to `{durable_scope}` and `status` set to `active`. Keep updates in that scope, use `task_note` for progress, and when finishing or stopping, set `status` to completed or blocked with `task_result` and `handoff={{completed, next_steps, open_questions, needs_human}}`. When a `needs_human` request you made has been dealt with, call `activity_handoff_resolve`.
3. Search relevant workspace memory before acting. For a handoff, review, or resume, the session context shows recent work with its next steps and open questions; use `activity_search` or `activity_get` for older history.
4. Write durable facts and decisions to {durable_label}. A fact needs a `subject_anchor`; routine progress belongs in activity records, not memory.
5. Before requesting manual setup or a raw secret, check available credential references and connector connections. Run connector actions through {APP_NAME} when possible.

For full operational guidance, use the generated `AGENTS.md` or `CLAUDE.md` for the relevant repository.
"""


def _connection_key_value(api_key=None):
    return api_key or "{{" + ENV_PREFIX + "API_KEY}}"


# What each hook client needs. One source for the guided step on the Connect page and for the
# full-text output tab, so the two cannot drift.
_HOOK_CLIENTS = {
    "claude_code": {
        "name": "Claude Code",
        "slug": "claude-code",
        "dot": ".claude",
        "settings": "settings.json",
        "output_type": "claude_code_hook",
    },
    "codex": {
        "name": "Codex",
        "slug": "codex",
        "dot": ".codex",
        "settings": "hooks.json",
        "output_type": "codex_hook",
    },
}

_WINDOWS_USER_FOLDER = "C:/Users/YOU"

# Alex's Windows test: handed these steps, Codex could not follow them. Its shell had no network
# and could not write to its own settings folder, though its MCP connection worked.
DO_IT_YOURSELF = (
    "Do these steps yourself, in a terminal. An agent usually cannot: it runs in a sandbox without network access "
    "and cannot write to its own settings folder, though its connection to {app} works. "
    "The instructions file (CLAUDE.md or AGENTS.md) is different: it lives in your project, so an agent can save it for you."
)


def _hook_steps(client_key, base_url):
    """The install, register, trust, map and try-it text for one tool, for macOS/Linux and Windows."""
    c = _HOOK_CLIENTS[client_key]
    name, slug, dot = c["name"], c["slug"], c["dot"]
    script_url = f"{base_url}/integrations/hooks/{slug}/agent_core_hook.py"
    unix_script = f"~/{dot}/hooks/agent_core_hook.py"
    # Forward slashes: valid in Windows paths, and nothing to double in JSON.
    win_script = f"{_WINDOWS_USER_FOLDER}/{dot}/hooks/agent_core_hook.py"
    win_env_script = rf"$env:USERPROFILE\{dot}\hooks\agent_core_hook.py"
    win_env_hooks = rf"$env:USERPROFILE\{dot}\hooks.json"
    unix_command = f"python3 {unix_script}"
    matcher = "startup|clear|compact|resume"

    if client_key == "claude_code":
        unix_hook = {
            "hooks": {
                "SessionStart": [{"matcher": matcher, "hooks": [{"type": "command", "command": unix_command, "timeout": 15}]}],
                "Stop": [{"hooks": [{"type": "command", "command": unix_command, "timeout": 10}]}],
            }
        }
        # The plain shell form, written with the full path: verified on Windows in the Claude desktop app (Code tab).
        win_command = f"python {win_script}"
        win_hook = {
            "hooks": {
                "SessionStart": [{"matcher": matcher, "hooks": [{"type": "command", "command": win_command, "timeout": 15}]}],
                "Stop": [{"hooks": [{"type": "command", "command": win_command, "timeout": 10}]}],
            }
        }
        unix_where = (
            f"Add this to `~/{dot}/settings.json` (or `{dot}/settings.local.json` for one project). "
            "Merge it into the existing file; do not replace the file."
        )
        win_where = (
            f"Add this to `settings.json` in the `{dot}` folder of your user folder, merged into the existing file. "
            f"Replace `{_WINDOWS_USER_FOLDER}` with your user folder written with forward slashes (for `C:\\Users\\jo`, `C:/Users/jo`). "
            "If your folder contains a space, write the command as `\"python \\\"C:/Users/jo doe/...\\\"\"`. "
            "If `python` is not recognised, use `py -3` instead."
        )
        requirements = (
            "Python 3 (`python3`; on Windows `python` or `py -3`) and step a done. "
            "The hook reads the key from the MCP entry, so a new key in that entry needs no change here."
        )
        trust = None
        trust_windows = None
    else:
        unix_hook = {
            "hooks": {
                "SessionStart": [{
                    "matcher": matcher,
                    "hooks": [{
                        "type": "command", "command": unix_command, "timeout": 15,
                        "statusMessage": f"Loading {APP_NAME} context", "additionalContextLimit": 4000,
                    }],
                }]
            }
        }
        # `commandWindows` is Codex's documented Windows-only override; the POSIX command stays beside it.
        win_hook = json.loads(json.dumps(unix_hook))
        win_hook["hooks"]["SessionStart"][0]["hooks"][0]["commandWindows"] = f'python "{win_script}"'
        unix_where = (
            f"Save this as `~/{dot}/hooks.json`. If the file already exists, add the SessionStart entry to it instead of replacing it."
        )
        win_where = (
            f"The command creates `hooks.json` in the `{dot}` folder of your user folder if there is none. "
            "If you already have one, copy the SessionStart entry from the command into it instead."
        )
        requirements = (
            "Python 3.11 or newer (`python3`; on Windows `python` or `py -3`) and step a done. "
            "The hook reads the key from `config.toml`, so a new key in that entry needs no change here."
        )
        trust = (
            f"Codex skips a hook until you trust it. In the CLI, start Codex, type `/hooks` and trust the {APP_NAME} hook. "
            "In the desktop app, open Settings > Hooks and trust the hook marked \"needs review\". "
            "Changing the registration asks again; updating the script does not."
        )
        trust_windows = (
            "The Windows desktop app does not start hooks yet, so there the instructions file (step b) loads the context. "
            "See \"Where the session start hook runs\" in the integration guide."
        )

    # Windows PowerShell 5 writes a BOM or UTF-16 with `>` and Out-File; WriteAllText writes plain UTF-8.
    win_create_cmd = None
    if client_key == "codex":
        win_create_cmd = (
            f'$f = "{win_env_hooks}"\n'
            "$json = @'\n" + json.dumps(win_hook, indent=2) + "\n'@\n"
            f"$json = $json.Replace('{_WINDOWS_USER_FOLDER}', ($env:USERPROFILE -replace '\\\\', '/'))\n"
            'if (Test-Path $f) { Write-Host "hooks.json already exists: add the SessionStart entry to it by hand" } '
            "else { [IO.File]::WriteAllText($f, $json) }"
        )
    unix_install = f"mkdir -p ~/{dot}/hooks\ncurl -fsSL {script_url} -o {unix_script}"
    win_install = (
        f'New-Item -ItemType Directory -Force "$env:USERPROFILE\\{dot}\\hooks" | Out-Null\n'
        f'Invoke-WebRequest {script_url} -OutFile "{win_env_script}"'
    )
    unix_try = (
        "echo '{\"hook_event_name\":\"SessionStart\",\"source\":\"startup\",\"session_id\":\"try-1\",\"cwd\":\"'\"$PWD\"'\"}' "
        f"| python3 {unix_script}"
    )
    win_try = (
        "'{\"hook_event_name\":\"SessionStart\",\"source\":\"startup\",\"session_id\":\"try-1\",\"cwd\":\"' + "
        "((Get-Location).Path -replace '\\\\','/') + '\"}' | "
        f'python "{win_env_script}"'
    )
    mapping = (
        f"Add your project folder to the workspace: Workspaces → Edit Workspace → Repository folders, one per machine where the project lives, "
        f"written as that machine names it (on Windows, `C:\\Users\\you\\my-repo`). Admin only. "
        "The agent also needs read access to the workspace: Agents page → Edit the agent → Advanced access → Can Read From. "
        "Create Workspace can set both."
    )
    mapping_rules = (
        "A folder that no workspace claims, or that is claimed by a workspace the agent cannot read, behaves exactly like an unmapped one: "
        "the session starts without context, and in a project folder the hook says so. "
        "An agent that can read exactly one workspace gets that workspace in every folder, mapped or not. "
        f"To pin a workspace, set `AGENT_CORE_SCOPE=workspace:<id>` in the environment {name} runs in."
    )
    return {
        "name": name,
        "output_type": c["output_type"],
        "intro": (
            f"Loads the agent's {APP_NAME} context when a {name} session starts: open work, recent results with their next steps, "
            "changes since its last session, key records."
            + (" It also asks the agent once to record a handoff if a task is left open." if client_key == "claude_code" else "")
        ),
        "requirements": requirements,
        "install": {"unix": ("bash", unix_install), "windows": ("powershell", win_install)},
        "register_where": {"unix": unix_where, "windows": win_where},
        "register": {"unix": json.dumps(unix_hook, indent=2), "windows": json.dumps(win_hook, indent=2)},
        "register_cmd": win_create_cmd,
        "do_it_yourself": DO_IT_YOURSELF.format(app=APP_NAME),
        "trust": trust,
        "trust_windows": trust_windows,
        "mapping": mapping,
        "mapping_rules": mapping_rules,
        "try": {"unix": ("bash", unix_try), "windows": ("powershell", win_try)},
        "try_note": (
            "It prints one line of JSON whose `additionalContext` is the session context, or nothing when no workspace matches the folder. "
            f"This opens a small session record in {APP_NAME}, which is harmless."
        ),
    }


def _hook_markdown(client_key, base_url):
    s = _hook_steps(client_key, base_url)
    name = s["name"]
    claude = client_key == "claude_code"
    n = iter(range(1, 9))

    def pair(key):
        u, w = s[key]["unix"], s[key]["windows"]
        return (
            f"macOS / Linux:\n\n```{u[0]}\n{u[1]}\n```\n\nWindows (PowerShell):\n\n```{w[0]}\n{w[1]}\n```\n"
        )

    windows_register = f"Windows: {s['register_where']['windows']}\n\n"
    windows_register += (
        f"```powershell\n{s['register_cmd']}\n```\n" if s["register_cmd"] else f"```json\n{s['register']['windows']}\n```\n"
    )
    parts = [
        f"# {APP_NAME} hook for {name}\n",
        f"{s['intro']}\n",
        f"**{s['do_it_yourself']}**\n",
        f"Requirements: {s['requirements']}\n",
        f"## {next(n)}. Install the script\n\n{pair('install')}",
        (
            f"## {next(n)}. Register the hook\n\n"
            f"macOS / Linux: {s['register_where']['unix']}\n\n```json\n{s['register']['unix']}\n```\n\n"
            + windows_register
        ),
    ]
    if s["trust"]:
        parts.append(f"## {next(n)}. Trust it once\n\n{s['trust']} On Windows: {s['trust_windows']}\n")
    parts.append(f"## {next(n)}. Map your project folder to a workspace\n\n{s['mapping']}\n")
    parts.append(f"## Try it by hand\n\nFrom a project folder you mapped, feed the hook the event {name} sends and read what it prints:\n\n{pair('try')}\n{s['try_note']}\n")
    behavior = [
        f"- {s['mapping_rules']}",
        "- Session context runs on `startup`, `clear`, `compact` and `resume`. A resumed session gets it only if it never received context for its workspace "
        "(for example the folder was not mapped yet)."
        + ("" if claude else " A forked session keeps its parent's context."),
    ]
    if claude:
        behavior += [
            "- The handoff request blocks a stop at most once per task, and never while Claude is already continuing because of a stop hook. Set `AGENT_CORE_HANDOFF_NUDGE=0` to turn it off.",
            "- Output stays under Claude Code's 10,000-character limit; set `AGENT_CORE_CONTEXT_CHARS` to use less.",
            "- The hook reads the server URL and key from the MCP entry in the project's `.mcp.json` or in `~/.claude.json` (`$CLAUDE_CONFIG_DIR/.claude.json` if set); with no entry it says so in a project folder.",
        ]
    else:
        behavior += [
            "- The context arrives as developer context in the session. `additionalContextLimit` (about 4,000 tokens) is set above the context's size so Codex does not replace it with a preview. Set `AGENT_CORE_CONTEXT_CHARS` to use less.",
            "- Only your own `~/.codex/config.toml` is read for the URL and key, never a repository's, and a redirect from the server is never followed, so a cloned project cannot make the hook send your key anywhere. "
            f"A project that keeps its own {APP_NAME} entry should set `AGENT_CORE_URL` and `AGENT_CORE_API_KEY` in the environment instead.",
            "- With no entry in `config.toml` the hook says so in a project folder.",
        ]
    behavior += [
        f"- If {APP_NAME} cannot be reached, the session starts normally and {name} shows a one-line notice. `AGENT_CORE_NOTICES=0` turns off the notices about a folder with no workspace and a project with no connection.",
        f"- `AGENT_CORE_URL` and `AGENT_CORE_API_KEY` override the MCP configuration, and `AGENT_CORE_MCP_NAME` names a server other than `{APP_SLUG}`.",
    ]
    parts.append("## Behavior\n\n" + "\n".join(behavior) + "\n")
    return "\n".join(parts)


def _build_claude_code_hook(base_url):
    return _hook_markdown("claude_code", base_url)


def _build_codex_hook(base_url):
    return _hook_markdown("codex", base_url)


def _mcp_config_sections(base_url, api_key=None):
    """The MCP config as separate per-tool blocks, in the order the full text prints them."""
    key = _connection_key_value(api_key)
    _shell_api_key = "${" + ENV_PREFIX + "API_KEY}"
    codex_auth = f'http_headers = {{ Authorization = "Bearer {key}" }}'
    generic_json = json.dumps(
        {
            "mcpServers": {
                APP_SLUG: {
                    "type": "http",
                    "url": f"{base_url}/mcp",
                    "headers": {
                        "Authorization": f"Bearer {key}",
                    },
                },
            },
        },
        indent=2,
    )
    opencode_json = json.dumps(
        {
            "mcp": {
                APP_SLUG: {
                    "type": "remote",
                    "url": f"{base_url}/mcp",
                    "enabled": True,
                    "headers": {
                        "Authorization": f"Bearer {key}",
                    },
                },
            },
        },
        indent=2,
    )
    antigravity_json = json.dumps(
        {
            "mcpServers": {
                APP_SLUG: {
                    "type": "http",
                    "serverUrl": f"{base_url}/mcp",
                    "headers": {
                        "Authorization": f"Bearer {key}",
                    },
                },
            },
        },
        indent=2,
    )
    sections = {
        "claude_code": f"""# Claude Code
# Option 1 — CLI (recommended, adds to user-level config so it works in every project):
claude mcp add --transport http --scope user {APP_SLUG} {base_url}/mcp --header "Authorization: Bearer {key}"

# Option 2 — create .mcp.json in your repo root (workspace-level, committable):
# If committing, replace the key with an env var: "Bearer {_shell_api_key}"
{generic_json}""",
        "codex": f"""# Codex CLI: add this to ~/.codex/config.toml
[mcp_servers.{APP_SLUG}]
url = "{base_url}/mcp"
{codex_auth}""",
        "opencode": f"""# OpenCode: add this under ~/.config/opencode/opencode.json
{opencode_json}""",
        "antigravity": f"""# Antigravity/Other MCP clients:
# Use this JSON block. File location varies by tool — see docs/integrations.md.
{antigravity_json}""",
    }
    return sections


# Which blocks of the MCP config each tool needs. Cursor and Windsurf use their own JSON
# (see _build_cursor_mcp_json); a tool not listed here gets the whole config.
_MCP_TOOL_SECTIONS = {
    "claude_code": ("claude_code",),
    "codex": ("codex",),
    "antigravity": ("antigravity",),
}


def _build_mcp_json(base_url, api_key=None, tool=None):
    if tool in ("cursor", "windsurf"):
        return _build_cursor_mcp_json(base_url, api_key)
    sections = _mcp_config_sections(base_url, api_key)
    wanted = _MCP_TOOL_SECTIONS.get(tool, tuple(sections))
    return "\n\n".join(sections[name] for name in wanted) + "\n"


def _build_cursor_mcp_json(base_url, api_key=None):
    key = _connection_key_value(api_key)
    _api_key_var = ENV_PREFIX + "API_KEY"
    _url_var = ENV_PREFIX + "URL"
    return f"""{{
  "mcpServers": {{
    "{APP_SLUG}": {{
      "url": "{base_url}/mcp",
      "headers": {{
        "Authorization": "Bearer {key}"
      }},
      "env": {{
        "{_api_key_var}": "{key}",
        "{_url_var}": "{base_url}"
      }}
    }}
  }}
}}"""


def _build_windsurf_mcp_json(base_url, api_key=None):
    return _build_cursor_mcp_json(base_url, api_key)


def _build_env_vars(base_url, agent_id, user_scope, workspace_scope, api_key=None):
    key = _connection_key_value(api_key)
    workspace_line = (
        f'export {ENV_PREFIX}WORKSPACE_SCOPE="{workspace_scope}"'
        if workspace_scope
        else f'# export {ENV_PREFIX}WORKSPACE_SCOPE="workspace:your-workspace-id"  # Optional'
    )
    return f"""# {APP_NAME} Environment Variables
# Use these only when your MCP config, launcher, or script reads {APP_NAME} values from the environment.
# MCP config is still the normal connection setup for MCP-capable tools.
# Do not commit these to workspace files.
#
# {ENV_PREFIX}API_KEY is the real authenticated agent identity.
# {ENV_PREFIX}AGENT_ID is helper metadata only; the server does not trust it for identity.

export {ENV_PREFIX}URL="{base_url}"
export {ENV_PREFIX}API_KEY="{key}"
export {ENV_PREFIX}AGENT_ID="{agent_id}"
export {ENV_PREFIX}USER_SCOPE="{user_scope}"
{workspace_line}
"""


def _get_destination_guidance(target, output_type):
    if output_type == "claude_md":
        return f"Save this as <code>CLAUDE.md</code> in the workspace repository root. The key you connect with decides which {APP_NAME} agent is active."
    elif output_type == "agents_md":
        return f"Save this as <code>AGENTS.md</code> in the workspace repository root. It can be shared by several agents; the key each one connects with decides which {APP_NAME} agent is active."
    elif output_type == "assistants_md":
        return "Paste this into the assistant's own onboarding or instruction field. It is meant for assistant-style agents that manage their own MCP configuration. Use the one-time key button here when you want the prompt to include a fresh key."
    elif output_type == "mcp_json":
        return f"Use the section that matches your tool. For Claude Code, run the <code>claude mcp add</code> command or save the JSON as <code>.mcp.json</code> in your repo root. Antigravity uses the same MCP shape but expects <code>serverUrl</code> instead of <code>url</code>. The key decides which {APP_NAME} agent is active."
    elif output_type == "env":
        return "Optional. Paste these into your shell profile (<code>~/.bashrc</code>, <code>~/.zshrc</code>) or tool environment only if you want the tool to read values from environment variables. This does not configure MCP by itself."
    elif output_type == "instructions":
        return "Start here: which output to use, where to put it, and when to generate a one-time key."
    elif output_type == "session":
        return "Paste this into the first message or workspace instructions field for a single agent session."
    elif output_type == "claude_code_hook":
        return "Follow these steps on each machine that runs Claude Code. The hook contains no key; it reuses the MCP configuration."
    elif output_type == "codex_hook":
        return "Follow these steps on each machine that runs Codex. The hook contains no key; it reuses the MCP configuration, and you trust it once in Codex with /hooks."
    elif output_type == "verification":
        return "Paste this into your connected agent after setup. It writes, finds and closes one note in a scope the agent can write to, and reports whether each step worked."
    return ""


def _build_verification_prompt(scope):
    """Four steps in one scope the agent can write to: write, search, activity, done."""
    return f"""Check that {APP_NAME} works for you, in `{scope}`:

1. Write a memory record to `{scope}` with `memory_class` set to `scratchpad` that says this agent is connected. Note the returned record ID.
2. Call `memory_search` with `scope` set to `{scope}` and a word from that record. Confirm the record ID from step 1 comes back.
3. Call `activity_update` with `task_description` "{APP_NAME} verification", `memory_scope` `{scope}` and `status` `active`; then again with `status` `completed` and a one-line `task_result`.
4. Report the scope you wrote to and whether each step worked. Do not print any secrets.

Use the full prefixed scope name exactly as shown.
"""
