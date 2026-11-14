"""Integrations dashboard page and agent-setup/connection generators.

Last feature area split out of the former dashboard.py monolith
dashboard.py no longer exists.
"""

from fastapi import APIRouter, Request, Depends
from fastapi.responses import PlainTextResponse
import contextvars
import json
from pathlib import Path

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
     "Save this as <code>CLAUDE.md</code> in the workspace repository root. Claude Code reads it automatically."),
    ("codex", "Codex",
     "Add this to <code>~/.codex/config.toml</code>.",
     "agents_md", "AGENTS.md", "AGENTS.md",
     "Save this as <code>AGENTS.md</code> in the workspace repository root. Codex reads it automatically."),
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

    # Only rotate when the output actually carries a key. Rotating is
    # destructive — it invalidates the key any running session is using — and it
    # was happening for every output type, including AGENTS.md, which contains
    # no agent-specific content at all. Generating a repo instruction file must
    # not knock a working agent offline.
    api_key = None
    if body.output_type in KEY_BEARING_OUTPUTS:
        api_key = agent_service.rotate_agent_key(agent["id"])
        if not api_key:
            return error_response(
                "AGENT_INACTIVE", "Cannot rotate key for inactive agent", 400
            )

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
        action="agent_key_rotated" if api_key else "integration_output_generated",
        resource_type="agent",
        resource_id=agent["id"],
        result="success",
        details={
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
            "warning": "This agent API key is shown once. Generating again rotates it and invalidates the previous one.",
        }
    )


HOOK_SCRIPTS = {
    "claude-code/agent_core_hook.py": Path(__file__).resolve().parents[2]
    / "integrations"
    / "claude-code"
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

    connection_options, instruction_options = _agent_setup_output_groups(target)
    connection_tabs = output_tabs(connection_options)
    instruction_tabs = output_tabs(instruction_options)
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

    def _section(label, text, file_name, key_button=""):
        return (
            "<div class='output-section'>"
            # The key button comes first, in reading order, not below a screenful of config text.
            + (f"<div class='output-actions' style='margin-bottom:8px'>{key_button}</div>" if key_button else "")
            + f"<div class='output-label'>{escape_html(label)}</div>"
            f"<pre class='output-block'>{escape_html(text)}</pre>"
            "<div class='output-actions'>"
            "<button type='button' class='btn btn-sm btn-secondary' onclick=\"copyGeneratedOutput(this)\">Copy</button>"
            f"<button type='button' class='btn btn-sm btn-secondary' data-download-filename='{escape_html(file_name)}'>Download</button>"
            "</div></div>"
        )

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
        hook_item = (
            "<li><h3>Add the session start hook</h3>"
            "<p>A hook runs when a Claude Code session starts and hands the agent its session context (open work, recent results, key records) with no step to remember. The steps are under "
            f"<a href=\"{page_path}?{urlencode({**current_params, 'output_type': 'claude_code_hook'})}#other-outputs\">Other outputs → Claude Code Hook</a>.</p></li>"
            if target == "claude_code"
            else ""
        )
        tool_steps_html = (
            '<ol class="setup-steps">'
            f"<li><h3>Connect it</h3><p>{connect_where}</p>{_section(c_label, c_text, f'{APP_SLUG}-mcp-config.txt', connect_btn)}</li>"
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
            <p class="form-hint">Choosing a user, workspace or agent only updates the preview below. Nothing is issued until you press <strong>Generate One-Time Key</strong> under the output (shown only for outputs that include a key), which replaces that agent's old key. CLAUDE.md, AGENTS.md, Instructions and the prompts are the same for every agent and safe to regenerate at any time.</p>
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
        <summary><span class="fold-title">Other outputs</span><span class="fold-hint">Instruction files for other tools, environment variables, raw config</span></summary>
        <p class="form-hint">Everything this page can generate, as tabs: environment variables, the session prompt, the Claude Code hook, and the full MCP config for every tool.</p>
        {destination_section}
        <div class="form-section" id="generated-output">
        <div class="setup-output-group" data-setup-path="connection">
          <h3>1. Connect a client</h3>
          <p>Use these the first time you connect a tool, or when you deliberately replace its connection settings or key. Generating one creates a new key for the selected agent and replaces its old one.</p>
          <div class="setup-tabs">{connection_tabs}</div>
        </div>
        <div class="setup-output-group" data-setup-path="workspace-instructions">
          <h3>2. Add or refresh workspace instructions</h3>
          <p>Use these outputs for a repository or a single session. They contain no key. You can refresh them after an Agent Core update or reuse them in another workspace without changing an MCP connection.</p>
          <div class="setup-tabs">{instruction_tabs}</div>
        </div>
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
    return constants + '<script src="/static/js/integrations.js?v=20261106"></script>'


# Outputs that embed the agent's API key, and therefore require rotating it.
# Everything else — the repo instruction files and the prompts — is guidance
# that carries no credential.
# Every output type `_build_agent_setup_output` knows. An unknown name used to fall through to the
# verification prompt without saying so, so a mistyped type looked like it had worked.
KNOWN_OUTPUT_TYPES = frozenset({
    "instructions", "session", "claude_md", "agents_md", "assistants_md", "mcp_json", "mcp_tool",
    "cursor_mcp_json", "windsurf_mcp_json", "env", "claude_code_hook", "verification",
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
That rotates this agent's API key and inserts the new key into the generated output.
The key is shown once. Generating again invalidates the previous key for this connection.
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
        f"- FIRST, every session, before any other tool: if your context already holds an \"{APP_NAME} session context\" block (a SessionStart hook injects it), use it; otherwise call `session_context` with `memory_scope` `{workspace_scope}`. It lists open work, recent results and key records, and gives the `execution_id` to pass to `activity_update` and `memory_write`. Other agents' work shows up there, not in real time."
        if workspace_scope
        else f"- FIRST, every session, before any other tool: call `session_context` with `memory_scope` `{agent_scope}`: it lists your open work, recent results and key memories, and gives the `execution_id` to pass to `activity_update` and `memory_write`. With no workspace you see only your own work; ask the owner for a workspace to share with other agents."
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
- Work: open a task with `activity_update` (`status: active`, `memory_scope: {default_scope}`) and keep it alive with a short note every few minutes. Close it with a `task_result` of one or two sentences saying what was done and where (a commit, a file). Put a request to a person in the handoff's `needs_human`.
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


def _build_claude_code_hook(base_url):
    script_url = f"{base_url}/integrations/hooks/claude-code/agent_core_hook.py"
    command = "python3 ~/.claude/hooks/agent_core_hook.py"
    hook = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|clear|compact",
                    "hooks": [{"type": "command", "command": command, "timeout": 15}],
                }
            ],
            "Stop": [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}],
        }
    }
    return f"""# {APP_NAME} hook for Claude Code

With this hook, every Claude Code session starts with {APP_NAME} context already loaded: standing rules, work waiting on a person, unfinished and handed-off work, recent results with their next steps, changes since the agent's last session, and an index of key records. When a task's activity is still open with no handoff, the hook asks the agent once to record one, so the next session can pick the work up. The agent does not have to remember a startup or closing routine.

Requirements: `python3`, and the {APP_NAME} MCP server already added to Claude Code (MCP Config output). The hook reads the server URL and key from that MCP configuration, so a rotated key needs no change here.

## 1. Install the script

```bash
mkdir -p ~/.claude/hooks
curl -fsSL {script_url} -o ~/.claude/hooks/agent_core_hook.py
```

## 2. Register the hook

Merge this into `~/.claude/settings.json` (all projects) or `.claude/settings.local.json` (one project):

```json
{json.dumps(hook, indent=2)}
```

## 3. Map repositories to workspaces

The hook sends the session's working directory. {APP_NAME} matches it against each workspace's repository folder (Workspaces → Edit Workspace, admin only), so the right workspace loads without per-repository setup. Enter the folder as it appears on the machine running Claude Code, not inside the {APP_NAME} container; only the path text is compared. Sessions in a folder no workspace claims start normally, without context. To pin a workspace instead, set `AGENT_CORE_SCOPE=workspace:<id>` in the environment Claude Code runs in.

## Behavior

- Session context runs on `startup`, `clear` and `compact`. A resumed session keeps its earlier context.
- The handoff request blocks a stop at most once per task, and never while Claude is already continuing because of a stop hook. Set `AGENT_CORE_HANDOFF_NUDGE=0` to turn it off.
- Output stays under Claude Code's 10,000-character limit; set `AGENT_CORE_CONTEXT_CHARS` to use less.
- If {APP_NAME} cannot be reached, the session starts normally and Claude Code shows a one-line notice.
- `AGENT_CORE_URL` and `AGENT_CORE_API_KEY` override the MCP configuration, and `AGENT_CORE_MCP_NAME` names a server other than `{APP_SLUG}`.
"""


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
claude mcp add --transport http --scope user {APP_SLUG} {base_url}/mcp \\
  --header "Authorization: Bearer {key}"

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
