"""Integrations dashboard page and agent-setup/connection generators.

Last feature area split out of the former dashboard.py monolith
dashboard.py no longer exists.
"""

from fastapi import APIRouter, Request, Depends
import json

from app.services.auth_service import get_user_by_id
from app.security.response_helpers import success_response, error_response
from app.security.scope_enforcer import ScopeEnforcer
from app.branding import APP_NAME, APP_SLUG, CREDENTIAL_PREFIX, ENV_PREFIX
from app.routes.dashboard_shared import (
    escape_html,
    require_auth,
    render_page,
)


router = APIRouter()


def _mcp_tool_listing() -> str:
    """The full MCP tool list as prose, derived from the manifest.

    Every generated prompt names the available tools. Hardcoded copies of that
    list drifted independently (three templates, three different subsets), so
    it is built from the same MANIFEST that /mcp serves.
    """
    from app.routes.mcp import MANIFEST

    names = [f"`{tool['name']}`" for tool in MANIFEST["tools"]]
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

On a `fact`, set `subject_anchor` to what a later session would check: `repo:<path>`, `host:<name-or-ip>`, or `service:<binding_id>`, with repo paths relative to the workspace root. Omit it when nothing could verify the record. Anchored facts are re-checked automatically; if a check reports the subject missing but it only moved, call `memory_reanchor` to repoint the record instead of letting it be proposed for removal.

`valid_from` and `valid_to` say when the content was true in the world, separately from when you wrote it. Set them when a record covers a period other than "from now on", and pass `as_of` to `memory_search` to ask what was true at a past moment.

`workspace_sync` returns pinned records for its workspace. Call `memory_pinned` once at session start only when you also need standing rules from other authorized scopes or no workspace sync applies. Treat pinned records as applying to everything you do, not just the current task. Search results carry `days_since_confirmed`. Treat a fact nobody has confirmed in months as a lead, not as current truth. Check it, then `memory_confirm` it with evidence naming what you checked, or supersede it if it has changed. Call `memory_feedback` when a recalled record clearly did or did not help. If something should apply to every session in the scope rather than only this task, call `memory_pin` to request it. An operator decides because standing context reaches every agent in the scope."""

# The record-shaping rules every template teaches. These were paraphrased
# separately in each generator, which meant a capability could land in the
# system and reach only whichever template someone remembered to edit. They are
# defined once and interpolated, so an agent learns the same contract whichever
# file its host reads.
MEMORY_RECORD_GUIDANCE = """Choosing between `fact` and `decision`: if the record would be settled by checking something, it is a `fact`; if it would be settled by someone deciding, it is a `decision`. "the build server is 192.0.2.10" is a fact; "do not edit vendored dependencies directly" is a decision. Reporting what you just did is neither — that belongs in `activity_update`.

When a record is a `fact`, set `subject_anchor` to the thing a later session would look at to check it: `repo:<path>`, `host:<name-or-ip>`, or `service:<binding_id>`. Repo paths must be **relative to the workspace root**, not absolute — the same directory is mounted at a different path in every agent's container, so an absolute path is only resolvable by whoever wrote it. Name what you actually looked at, and omit it when nothing could verify the record — which is normal for a `decision`.

Anchored facts get re-checked automatically. If a check reports the subject missing but you can see it only moved, call `memory_reanchor` to repoint the record rather than letting it be proposed for removal; use `memory_verify` to check a scope's anchored facts on demand before you rely on them. A check that fails to run leaves the record alone — "could not check" is never treated as "wrong".

`valid_from` and `valid_to` say when the content was true in the world, which is a different timeline from when you wrote it. Set them when a record covers a period other than "from now on". Superseding closes the previous record's window for you, and `memory_search` with `as_of` answers what was true at a past moment.

`workspace_sync` returns pinned records for its workspace. Call `memory_pinned` once at session start only when you also need standing rules from other authorized scopes or no workspace sync applies. Treat pinned records as applying to everything you do, not just the current task. Search results carry `days_since_confirmed`. Treat a fact nobody has confirmed in months as a lead rather than as current truth. Check it, then call `memory_confirm` with evidence naming what you checked, or supersede it if it has changed. Call `memory_feedback` when a recalled record clearly did or did not help. If something should apply to every session in the scope rather than only this task, call `memory_pin` to request it. An operator decides because standing context reaches every agent in the scope."""

MEMORY_DISCIPLINE_GUIDANCE = """One memory per insight, not per occurrence: when a recurring task (heartbeat, monitor tick, scheduled review, watchdog retry) keeps producing the same finding, write ONE record the first time and supersede it if the situation changes — never a new record per tick, per fire, or per re-check. Per-occurrence status belongs in `activity_update` (`task_note`/`task_result`) or the source system, not in memory. To find what has already been done — "what did we do on X", "has this been tried" — search the activity trail with `activity_search` rather than memory; memory is for what stays true, the trail is for what happened. Before writing a `fact` or `decision`, search for an existing record on the same topic and supersede it (`supersedes_id`) instead of adding a near-duplicate. For notes that stop being true on their own (for example "service X is down right now"), use `scratchpad` or set `expires_at`."""

REPOSITORY_BOUNDARY_GUIDANCE = """Workspace context is not repository access. If a task points to files outside the current checkout, switch to an authorized checkout or report the mismatch. Do not use memory summaries as a substitute for reviewing the source."""

WORKSPACE_SYNC_OPERATING_RULES = """Pass the session's `execution_id` to supported writes, including `activity_update`, `memory_write`, and briefing creation. If `has_more` is true, process and acknowledge the current page, then sync again until `has_more` is false. If `cursor_reset` is true, treat the returned bootstrap as incomplete history and inspect the relevant memory, activity trail, and briefings. If sync fails, continue only when the task does not depend on current shared state. For a review, resume, or handoff, stop and report the sync failure."""

CONNECTOR_BINDING_GUIDANCE = """A visible connector binding grants access to every action enabled on its connector type. Use the connector type's action controls to disable actions for all bindings. Use delegated grants to restrict temporary access to exact binding/action pairs."""


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
        "generic_mcp": "Generic MCP",
    }.get(target, "Generic MCP")


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
            checks.append({"label": "Workspace read-only access", "status": "warning"})
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
        checks.append({"label": "Selected user is not the agent's active principal", "status": "warning"})

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
            "label": "Scope model: workspace membership gates agent scopes",
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
    base_url = str(request.base_url).rstrip("/")
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
        base_url=str(request.base_url).rstrip("/"),
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
            "warning": "This key is shown once. Generating again rotates the agent key and invalidates the previous key.",
        }
    )


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
    tool_label = _agent_setup_target_label(target)

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

    workspaces = (
        workspace_service.list_workspaces()
        if is_admin
        else workspace_service.list_workspaces(owner_user_id=current_user_id)
    )
    project_options = "".join(
        f'<option value="{p["id"]}" {"selected" if p["id"] == workspace_id else ""}>{escape_html(p["name"])}</option>'
        for p in workspaces
    )

    agents = list_agents() if is_admin else list_agents(owner_user_id=current_user_id)
    visible_agents = {a["id"]: a for a in agents if a.get("is_active")}
    agent_options = "".join(
        f'<option value="{a["id"]}" {"selected" if a["id"] == agent_id else ""}>{escape_html(a.get("display_name", a["id"]))}</option>'
        for a in visible_agents.values()
    )

    user_options = "".join(
        f'<option value="{u["id"]}" {"selected" if u["id"] == user_id else ""}>{escape_html(u.get("display_name", u["id"]))}</option>'
        for u in users
    )

    access_checks = []
    generated_output = ""
    output_label = ""
    base_url = str(request.base_url).rstrip("/")
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
        if any(c["status"] == "warning" for c in access_checks):
            checks_html += "<p class='text-muted' style='font-size:0.8rem;margin-top:8px'>Warnings indicate missing access. Review access on the Agents page or generate the prompt anyway.</p>"

    current_params = {
        "user_id": user_id,
        "workspace_id": workspace_id,
        "agent_id": agent_id,
    }
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
            f"<button type='button' class='btn btn-sm btn-warning' id='generate-connection-btn' data-label='{escape_html(connection_label)}' onclick='generateConnectionConfig()'>{escape_html(connection_label)}</button>"
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
    body = f"""
    <div class="page-header setup-page-header">
      <h1>Integrations</h1>
      <p class="subtitle">Generate setup instructions, environment variables, MCP config, and AI-facing prompts for connecting tools to {APP_NAME}.</p>
      <div class="text-muted" style="font-size:0.86rem;margin-top:8px">
        Current tool preset: <strong>{escape_html(tool_label)}</strong>. The main presets are Claude Code, Codex, Cursor, Windsurf, Antigravity, and Generic MCP/REST. The generated outputs in this page are the canonical setup files and prompts.
      </div>
    </div>

    <form method="get" action="{page_path}" class="setup-form" onsubmit="saveSetupScrollPosition()">
      <div class="form-section">
        <h2>Choose Context</h2>
        <div class="form-row">
          <div class="form-group">
            <label for="user_id">User</label>
            <select id="user_id" name="user_id" onchange="this.form.submit()">
              {user_options}
            </select>
          </div>
          <div class="form-group">
            <label for="workspace_id">Workspace <span class="text-muted">(optional)</span></label>
            <select id="workspace_id" name="workspace_id" onchange="this.form.submit()">
              <option value="">-- Optional --</option>
              {project_options}
            </select>
          </div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label for="agent_id">Agent</label>
            <select id="agent_id" name="agent_id" onchange="this.form.submit()">
              <option value="">-- Select agent --</option>
              {agent_options}
            </select>
            <p class="form-hint">Identifies which agent connects. Changing this selection only re-renders the preview below — nothing is issued until you press the amber <strong>Generate connection</strong> button, which mints a fresh API key for the selected agent and invalidates the previous one. That button appears only for the outputs that embed a key (MCP Config, Environment Variables, Assistants). CLAUDE.md, AGENTS.md, Instructions and the prompts are repository-level guidance — identical for every agent, and safe to regenerate at any time.</p>
          </div>
        </div>
      </div>

      {access_check_section}

      {destination_section}

      <div class="form-section" id="generated-output">
        <h2>Generated Output</h2>
        <div class="setup-output-group" data-setup-path="connection">
          <h3>1. Connect a client</h3>
          <p>Use these outputs the first time you connect a client, or when you deliberately replace its MCP settings or bearer key. Generating a key-bearing output rotates the selected agent's key.</p>
          <div class="setup-tabs">{connection_tabs}</div>
        </div>
        <div class="setup-output-group" data-setup-path="workspace-instructions">
          <h3>2. Add or refresh workspace instructions</h3>
          <p>Use these outputs for a repository or a single session. They contain no bearer key. You can refresh them after an Agent Core update or reuse them in another workspace without changing an MCP connection.</p>
          <div class="setup-tabs">{instruction_tabs}</div>
        </div>
        <div class="alert alert-warning" id="connection-warning" style="display:none"></div>
        <div class="connection-doctor" id="connection-doctor">
          <div>
            <h3>Connection doctor</h3>
            <p>Use the exact MCP URL below, not the site root. This check never stores a bearer key.</p>
            <code>{escape_html(base_url)}/mcp</code>
          </div>
          <button type="button" class="btn btn-sm btn-secondary" id="test-connection-details-btn" onclick="testConnectionDetails()">Test connection details</button>
        </div>
        <div class="connection-doctor-result" id="connection-doctor-result" aria-live="polite"></div>
        {output_label_html}
        {output_display}
        <div class="output-actions">{connection_btn}{copy_btn}{download_btn}{regenerate_btn}</div>
      </div>
    </form>

    <div class="setup-next-steps">
      <h3>Next Steps</h3>
      <ol>
        <li>Start with Instructions if you are not sure which output to use.</li>
        <li>For most tools, MCP Config is the required connection step. Environment is optional and only stores values for shell or launcher use.</li>
        <li>Add CLAUDE.md, AGENTS.md, or Session Prompt only where the connected tool reads them.</li>
        <li>Run the Verification Prompt in the connected agent to confirm end-to-end memory, credential, and connector access.</li>
      </ol>
    </div>
    """

    return render_page(
        "Integrations", body, page_path, _agent_setup_extra_js(base_url), session=session
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
    return constants + '<script src="/static/js/integrations.js?v=20260828"></script>'


# Outputs that embed the agent's API key, and therefore require rotating it.
# Everything else — the repo instruction files and the prompts — is guidance
# that carries no credential.
KEY_BEARING_OUTPUTS = frozenset(
    {"env", "mcp_json", "cursor_mcp_json", "windsurf_mcp_json", "assistants_md"}
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
    else:
        label = "Verification Prompt"
        content = _build_verification_prompt(user_scope, default_scope)

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

    return f"""# {APP_NAME} Setup Instructions

You are connected to {APP_NAME}.

**User:** {user_display}
**Workspace:** {workspace_name}
**Agent:** {agent_display}
**Base URL:** {base_url}

## Scopes

{scope_guide}

## Getting Started

1. Call `workspace_sync` for `{default_scope}` at session startup. Reuse its `execution_id` for that host session.
2. Sync before resuming, reviewing, handing off, or completing shared work. Process each stable change ID once, including when it appears in both its resource group and `other_session_changes`. Call `workspace_sync_ack` only after you incorporate or intentionally skip the delivered page.
3. Call `activity_pickup` at startup or when idle to check for assigned work. If it returns an activity, read it, start working, and send heartbeats. If it returns null, proceed with the user's task.
4. Search memory in `{default_scope}` for relevant older context. If the search returns little or nothing, retry with exact topic values, exact keywords, or a known record ID. For a handoff, resume, or review, use `activity_list` and `briefing_list` when the sync response does not contain enough history.
5. Search `{user_scope}` for user preferences and owner context. Active agents automatically inherit read access to their principal's user scope.
6. Create or update an activity record when starting a meaningful task. Include `{default_scope}` as `memory_scope` on every heartbeat, progress note, completion, or blocked update. {APP_NAME} updates only the activity in that scope and never moves one across scopes. Use `task_note` for progress and `task_result` when closing the task.
7. Store durable decisions and handoff notes in `{default_scope}`.
8. Use `credential_list` and `credential_get` to retrieve credential references. Never ask for raw secrets.
9. Use connector tools to find and run server-side connector bindings before asking the user to configure an external service manually.

{REPOSITORY_BOUNDARY_GUIDANCE}

{WORKSPACE_SYNC_OPERATING_RULES}

## Memory Write Rules

{MEMORY_CLASS_GUIDANCE}
- Use `{workspace_scope_label}` for workspace memory when a workspace is selected, `{user_scope}` for stable user preferences, and `{agent_scope}` for private scratch context.
- `topic` is a short label for listings. `confidence` is caller-assigned and not used for ranking: ranking uses observed evidence (recall, feedback, confirmation age), not self-assessment.
- {MEMORY_DISCIPLINE_GUIDANCE}

## Credentials And Connectors

When a task may require an external service, credential, API token, repository host, chat service, browser service, or connector, check {APP_NAME} before asking the user for setup details.

1. Use `credential_list` to discover available credential references in authorized scopes.
2. Use `credential_get` only when you need a specific `{CREDENTIAL_PREFIX}*` reference for a local tool or command. Never ask the user for raw secrets and never print raw secrets.
3. Use `connectors_summary` for a compact capability overview, or `connectors_list` and `connectors_bindings_list` when you need raw connector and binding lists.
4. Use `connectors_actions_list` before running an unfamiliar connector action.
5. Use `connectors_bindings_test` when a binding may be stale or unverified.
6. Use `connectors_run` when {APP_NAME} should perform the external action server-side.

Prefer connector bindings over local secret handling when both are available, because the raw credential stays inside {APP_NAME}.
{CONNECTOR_BINDING_GUIDANCE}

## API Key

Set `{ENV_PREFIX}API_KEY` in your local environment using the key shown when this agent was created or rotated.
Do not commit API keys to workspace files.

{tool_tips}
## Tool Configuration

- MCP endpoint: {base_url}/mcp
- REST base: {base_url}
- Auth: Bearer token with your agent API key
"""


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
    return f"""{tool_line} You are working for {user_display} on {workspace_name}.

Use {APP_NAME} MCP for durable workspace memory, handoffs, and workspace context. If this is a handoff, resume, or review of prior work, also inspect the recent activity trail and any generated briefing before making changes. Use `activity_list` and `briefing_list` when you need that trail from MCP.
Default memory scope for this setup is `{default_scope}`.
Core MCP tools include {_mcp_tool_listing()}.
Use your private scope `{agent_scope}` only for tool-specific scratch context.
Use full prefixed scope names exactly as shown; do not use plain workspace IDs or agent IDs as memory scopes.
Read `{user_scope}` for stable {user_display} preferences and other owner-context details. Active agents automatically inherit read access to their principal's user scope.
Use credential references through {APP_NAME} MCP; never request or print raw secrets.
Activity records are operational task tracking, not durable memory. At session startup, call `workspace_sync` for `{default_scope}` before starting meaningful work. Reuse the returned `execution_id` for that session. Synchronize again before resuming, reviewing, handing off, or completing shared work. Process each stable change ID once even when it appears in both a resource group and `other_session_changes`, then call `workspace_sync_ack` after incorporating or intentionally skipping the page. At the start of every non-trivial user task, immediately call `activity_update` with a concise `task_description`, `memory_scope` set to `{default_scope}`, and `status` set to `active`. When you are starting fresh or have gone idle, call `activity_pickup` to claim work a human assigned to you in your scopes. It returns nothing when nothing is waiting; work is pulled, never pushed, so an agent that never asks never sees it.

{REPOSITORY_BOUNDARY_GUIDANCE}

{WORKSPACE_SYNC_OPERATING_RULES}

If the session reloads, a handoff begins, or no active activity exists yet, open a fresh activity first with `status: active` before attempting to close it. While actively working, use `task_note` for short progress updates and call `activity_update` again every 1-2 minutes as a heartbeat. Include `{default_scope}` as `memory_scope` on every later update: Core updates only the activity in that scope and never moves one across scopes. Before your final response, call `activity_update` with `status: completed` and a short `task_result` summary when the task is complete, or `status: blocked` if you cannot proceed and need user input.
If the session has to stop early or hits a token limit, leave the activity current and write durable decisions or handoff notes to memory so another agent can continue from the saved state.
If work needs to move across users or workspaces, make that explicit in the activity scope and handoff notes rather than assuming a hidden policy layer.
If the client has hooks or plugins, use them to automate memory/activity capture; if it does not, treat this prompt as the source of truth for those expectations.
When an external service is needed, use `credential_list`, `credential_get`, `connectors_summary`, `connectors_list`, `connectors_bindings_list`, `connectors_actions_list`, `connectors_bindings_test`, and `connectors_run` instead of asking the user to hand-wire secrets or run manual service calls.
{CONNECTOR_BINDING_GUIDANCE}

## Memory Workflow

At the start of a meaningful task:

1. Start or refresh the activity record using `activity_update`.
2. Search `{default_scope}` with 2-3 focused queries for relevant architecture, decisions, prior bugs, and current project state. If the search returns little or nothing, retry with exact topic values, exact keywords from prior records, or a known record id. When embeddings are unavailable, broad conceptual queries can miss; exact tokens and known ids are more reliable.
3. Search `{user_scope}` when user preferences or personal workflow may matter.
4. Use `memory_get` with a scope to list or read records; use `memory_search` to find records by query, topic, or class (there is no fetch-by-id).

Write memory only when it will help a future session:

- `decision` in `{default_scope}` for durable choices, tradeoffs, rejected options, and why they were chosen — things only a person can revise.
- `fact` in `{default_scope}` for observations someone could re-check later: implementation details, integration details, constraints, and verified behavior.
- `preference` in the authenticated/default user scope only if your key has user-scope write; otherwise treat the user scope as read-only owner context and write the preference to `{default_scope}` instead.
- `scratchpad` in `{agent_scope}` for temporary private notes, or in `{default_scope}` only for short-lived workspace handoff notes.

{MEMORY_RECORD_GUIDANCE}

Do not write memory for routine progress, command output, facts already obvious from files, secrets, raw credentials, or noisy transient debugging notes.
{MEMORY_DISCIPLINE_GUIDANCE} Use concise content and add a short `topic` as a label. Ranking uses observed evidence — how often a record is recalled, whether it helped, and when it was last confirmed — not self-assessment, so do not spend effort tuning `confidence` or `importance`.
Use this prompt to bootstrap behavior in clients without lifecycle hooks; it is not a substitute for a configured MCP server or plugin.

Start by confirming you can reach {APP_NAME} at {base_url}/mcp, then search `{default_scope}` for relevant context before making changes.
"""


def _build_claude_md(
    base_url, user_scope, workspace_scope, agent_scope, agent_display, workspace_name
):
    default_scope = (
        workspace_scope
        or f"the authenticated/default user scope from your {APP_NAME} connection"
    )
    workspace_scope_label = workspace_scope or "No workspace scope selected"
    private_scope_guidance = f"Use your authenticated {APP_NAME} private scope, usually `agent:<your-agent-id>`, only for tool-specific scratch context."
    return f"""# {APP_NAME} Workspace Context

You are working on the {workspace_name} workspace.

Use {APP_NAME} for durable workspace memory, activity tracking, handoffs, and credential references. If this is a handoff, resume, or review of prior work, also inspect the recent activity trail and any generated briefing before making changes. Use `activity_list` and `briefing_list` when you need that trail from MCP.
Core MCP tools include {_mcp_tool_listing()}.

## Connection

- **{APP_NAME} URL:** {base_url}
- **Workspace scope:** {workspace_scope_label}

The active {APP_NAME} user and agent identities are determined by the MCP/API key configured in your tool, not by this file. Do not add API keys to this file.

## Memory Scopes

Use `{default_scope}` for default memory in this setup.
Read the authenticated/default user scope from your {APP_NAME} connection for stable personal preferences and owner-context details. Active agents automatically inherit read access to their principal's user scope.
{private_scope_guidance}
Use full prefixed scope names exactly as shown. Do not use plain workspace IDs like `{workspace_name}` or agent IDs like `{agent_display}` as memory scopes.

## Memory Workflow

At the start of a meaningful task:

1. Start or refresh the activity record using the Activity Tracking workflow below.
2. Search `{default_scope}` with 2-3 focused queries for relevant architecture, decisions, prior bugs, and current project state. If the search returns little or nothing, retry with exact topic values, exact keywords from prior records, or a known record id. When embeddings are unavailable, broad conceptual queries can miss; exact tokens and known ids are more reliable.
3. Search the authenticated/default user scope when user preferences or personal workflow may matter.
4. Use `memory_get` with a scope to list or read records; use `memory_search` to find records by query, topic, or class (there is no fetch-by-id).

Write memory only when it will help a future session:

- `decision` in `{default_scope}` for durable choices, tradeoffs, rejected options, and why they were chosen — things only a person can revise.
- `fact` in `{default_scope}` for observations someone could re-check later: implementation details, integration details, constraints, and verified behavior.
- `preference` in the authenticated/default user scope only if your key has user-scope write; otherwise treat the user scope as read-only owner context and write the preference to `{default_scope}` instead.
- `scratchpad` in the authenticated private agent scope for temporary private notes, or in `{default_scope}` only for short-lived workspace handoff notes.

{MEMORY_RECORD_GUIDANCE}

Do not write memory for routine progress, command output, facts already obvious from files, secrets, raw credentials, or noisy transient debugging notes.
{MEMORY_DISCIPLINE_GUIDANCE}

Keep memory content concise. Add a short `topic` as a label. Do not bother tuning `confidence` — ranking ignores it; what matters is the class, the anchor, and confirming a fact when you have actually checked it.

## Credentials

Use `credential_get` to retrieve `{CREDENTIAL_PREFIX}*` references. The Credential Broker resolves them at execution time.
Never ask users for raw credential values.

## Connectors

When a task may require an external service, credential, API token, repository host, chat service, browser service, or connector, check {APP_NAME} before asking the user for setup details.

1. Use `credential_list` to discover available credential references in authorized scopes.
2. Use `credential_get` only when you need a specific `{CREDENTIAL_PREFIX}*` reference for a local tool or command. Never ask the user for raw secrets and never print raw secrets.
3. Use `connectors_summary` for a compact capability overview, or `connectors_list` and `connectors_bindings_list` when you need raw connector and binding lists.
4. Use `connectors_actions_list` before running an unfamiliar connector action.
5. Use `connectors_bindings_test` when a binding may be stale or unverified.
6. Use `connectors_run` when {APP_NAME} should perform the external action server-side.

Prefer connector bindings over local secret handling when both are available, because the raw credential stays inside {APP_NAME}.
{CONNECTOR_BINDING_GUIDANCE}

## Activity Tracking

Activity records are operational task tracking, not durable memory.

At session startup, call `workspace_sync` for `{default_scope}` and reuse its `execution_id` for the session. Sync before resuming, reviewing, handing off, or completing shared work. Process each stable change ID once, including when it appears in both its resource group and `other_session_changes`. Acknowledge a page with `workspace_sync_ack` only after you incorporate or intentionally skip it.

{REPOSITORY_BOUNDARY_GUIDANCE}

{WORKSPACE_SYNC_OPERATING_RULES}

At the start of every non-trivial user task, call `activity_update` immediately with:

- `task_description`: a concise description of the current task
- `memory_scope`: `{default_scope}`
- `status`: `active`

When the task is a handoff, resume, or review of prior work, inspect the recent activity trail and any generated briefing before making changes. Use workspace memory, not agent-private scratch notes, as the durable source of truth for prior work.
Use `activity_list` and `briefing_list` when you need to inspect that trail from MCP instead of the dashboard.

While actively working, call `activity_update` again every 1-2 minutes as a heartbeat. Use `task_note` for interim progress updates and update `task_description` if the task changes materially.
Include `{default_scope}` as `memory_scope` on every heartbeat, progress note, completion, or blocked update. Core updates only the activity in that scope and never moves one across scopes.

When you are starting fresh or have gone idle, call `activity_pickup` to claim work a human assigned to you in your scopes. It returns nothing when nothing is waiting; work is pulled, never pushed, so an agent that never asks never sees it.

If the session reloads, a handoff begins, or no active activity exists yet, open a fresh activity first with `status: active` before attempting to close it. Before your final response, call `activity_update` with `status: completed` and a short `task_result` summary when the task is complete. Use `task_note` for in-flight updates. Use `status: blocked` if you cannot proceed and need user input. Do not create activity records for trivial one-shot answers that do not inspect or modify project state.
If the session has to stop early or hits a token limit, leave the activity current and write durable decisions or handoff notes to memory so another agent can continue from the saved state.
If work needs to move across users or workspaces, make that explicit in the activity scope and handoff notes rather than assuming a hidden policy layer.
If the client supports hooks or plugins, use them to automate these calls. If it does not, keep using this file as the manual operating contract.


## Claude Code Notes

- Claude Code automatically reads this `CLAUDE.md` file when present in the workspace root.
- Claude Code uses the configured MCP connection or your shell environment's `{ENV_PREFIX}API_KEY`. That key determines which {APP_NAME} user and agent are active.
- Do not add your API key to this file.
- **Tool availability:** Claude Code defers MCP tool schemas at startup to save context. Before calling any {APP_NAME} tool in a new session, load the startup schemas with `ToolSearch("select:mcp__{APP_SLUG}__workspace_sync,mcp__{APP_SLUG}__workspace_sync_ack,mcp__{APP_SLUG}__memory_search,mcp__{APP_SLUG}__activity_update,mcp__{APP_SLUG}__memory_write")`. Skipping this causes `InputValidationError`. Add other tool names to the select list as needed.
- If Claude Code can't reach {APP_NAME}, run the Verification Prompt output to verify the full end-to-end setup.
"""


def _build_agents_md(
    base_url, user_scope, workspace_scope, agent_scope, workspace_name
):
    default_scope = (
        workspace_scope
        or f"the authenticated/default user scope from your {APP_NAME} connection"
    )
    workspace_scope_label = workspace_scope or "No workspace scope selected"
    return f"""# {APP_NAME} Workspace Context

You are working on the {workspace_name} workspace.

## {APP_NAME}

Use {APP_NAME} MCP for memory, credential references, and activity tracking. If this is a handoff, resume, or review of prior work, also inspect the recent activity trail and any generated briefing before making changes. Use `activity_list` and `briefing_list` when you need that trail from MCP.

- **Base URL:** {base_url}
- **Workspace scope:** {workspace_scope_label}

The active {APP_NAME} user and agent identities are determined by the MCP/API key configured in your tool, not by this file. Do not commit credentials to this file.
If your host defers tool availability, run its tool discovery/load step first so the {APP_NAME} MCP tools are available before you try to call them.

## Memory Scope Guidance

Default memory scope for this setup is `{default_scope}`.
Read the authenticated/default user scope from your {APP_NAME} connection for stable personal preferences and owner-context details. Active agents automatically inherit read access to their principal's user scope.
Use your authenticated {APP_NAME} private scope, usually `agent:<your-agent-id>`, for private scratch notes only.
Use full prefixed scope names exactly as shown. Do not use plain workspace IDs or agent IDs as memory scopes.

## Activity Workflow

Activity records are operational task tracking, not durable memory.

At session startup, call `workspace_sync` for `{default_scope}` and reuse its `execution_id` for the session. Sync before resuming, reviewing, handing off, or completing shared work. Process each stable change ID once, including when it appears in both its resource group and `other_session_changes`. Acknowledge a page with `workspace_sync_ack` only after you incorporate or intentionally skip it.

{REPOSITORY_BOUNDARY_GUIDANCE}

{WORKSPACE_SYNC_OPERATING_RULES}

At the start of every non-trivial user task, call `activity_update` immediately with:

- `task_description`: a concise description of the current task
- `memory_scope`: `{default_scope}`
- `status`: `active`

When the task is a handoff, resume, or review of prior work, inspect the recent activity trail and any generated briefing before making changes. Use workspace memory, not agent-private scratch notes, as the durable source of truth for prior work.
Use `activity_list` and `briefing_list` when you need to inspect that trail from MCP instead of the dashboard.

While actively working, call `activity_update` again every 1-2 minutes as a heartbeat. Use `task_note` for interim progress updates and update `task_description` if the task changes materially.
Include `{default_scope}` as `memory_scope` on every heartbeat, progress note, completion, or blocked update. Core updates only the activity in that scope and never moves one across scopes.

When you are starting fresh or have gone idle, call `activity_pickup` to claim work a human assigned to you in your scopes. It returns nothing when nothing is waiting; work is pulled, never pushed, so an agent that never asks never sees it.

If the session reloads, a handoff begins, or no active activity exists yet, open a fresh activity first with `status: active` before attempting to close it. Before your final response, call `activity_update` with `status: completed` and a short `task_result` summary when the task is complete. Use `task_note` for in-flight updates. Use `status: blocked` if you cannot proceed and need user input. Do not create activity records for trivial one-shot answers that do not inspect or modify project state.
If the session has to stop early or hits a token limit, leave the activity current and write durable decisions or handoff notes to memory so another agent can continue from the saved state.
If work needs to move across users or workspaces, make that explicit in the activity scope and handoff notes rather than assuming a hidden policy layer.
If the client supports hooks or plugins, use them to automate these calls. If it does not, keep using this file as the manual operating contract.

## Memory Workflow

At the start of a meaningful task:

1. Confirm {APP_NAME} is reachable at {base_url}/mcp if this is a new setup or connectivity is uncertain.
2. Start or refresh the activity record using the Activity Workflow above.
3. Search `{default_scope}` with 2-3 focused queries for relevant architecture, decisions, prior bugs, and current project state. If the search returns little or nothing, retry with exact topic values, exact keywords from prior records, or a known record id. When embeddings are unavailable, broad conceptual queries can miss; exact tokens and known ids are more reliable.
4. If this is a handoff, resume, or review of prior work, inspect the recent activity trail and any generated briefing before making changes.
5. Search the authenticated/default user scope when user preferences or personal workflow may matter.
6. Use `memory_get` with a scope to list or read records; use `memory_search` to find records by query, topic, or class (there is no fetch-by-id).

Write memory only when it will help a future session:

- `decision` in `{default_scope}` for durable choices, tradeoffs, rejected options, and why they were chosen — things only a person can revise.
- `fact` in `{default_scope}` for observations someone could re-check later: implementation details, integration details, constraints, and verified behavior.
- `preference` in the authenticated/default user scope only if your key has user-scope write; otherwise treat the user scope as read-only owner context and write the preference to `{default_scope}` instead.
- `scratchpad` in the authenticated private agent scope for temporary private notes, or in `{default_scope}` only for short-lived workspace handoff notes.

{MEMORY_RECORD_GUIDANCE}

Do not write memory for routine progress, command output, facts already obvious from files, secrets, raw credentials, or noisy transient debugging notes.
{MEMORY_DISCIPLINE_GUIDANCE}

Keep memory content concise. Add a short `topic` as a label. Do not bother tuning `confidence` — ranking ignores it; what matters is the class, the anchor, and confirming a fact when you have actually checked it.

## Credentials And Connectors

When a task may require an external service, credential, API token, repository host, chat service, browser service, or connector, check {APP_NAME} before asking the user for setup details.

1. Use `credential_list` to discover available credential references in authorized scopes.
2. Use `credential_get` only when you need a specific `{CREDENTIAL_PREFIX}*` reference for a local tool or command. Never ask the user for raw secrets and never print raw secrets.
3. Use `connectors_summary` for a compact capability overview, or `connectors_list` and `connectors_bindings_list` when you need raw connector and binding lists.
4. Use `connectors_actions_list` before running an unfamiliar connector action.
5. Use `connectors_bindings_test` when a binding may be stale or unverified.
6. Use `connectors_run` when {APP_NAME} should perform the external action server-side.

Prefer connector bindings over local secret handling when both are available, because the raw credential stays inside {APP_NAME}.
{CONNECTOR_BINDING_GUIDANCE}
This file is the manual fallback when the client has no lifecycle hook or plugin layer.

## Codex Notes

- Codex reads `AGENTS.md` at the start of each session.
- This file is workspace-centric and can be shared by multiple agents in the same repository. The MCP/API key determines whether the active agent is Codex, OpenCode, Claude Code, or another configured agent.
- For multi-agent collaboration, select a workspace and ensure each agent has read/write access to that workspace scope.
- Use the MCP tools (`memory_search`, `memory_write`, `activity_update`, `credential_list`, `credential_get`, `connectors_*`) rather than raw API calls for better scope enforcement.
- If Codex loses connectivity, run the verification prompt to verify the full end-to-end setup.
"""


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

1. At session start, call `workspace_sync` for `{durable_scope}` and reuse its `execution_id`. Acknowledge each processed page with `workspace_sync_ack`; continue until `has_more` is false.
2. Before a meaningful task, call `activity_update` with `memory_scope` set to `{durable_scope}` and `status` set to `active`. Keep updates in that scope, use `task_note` for progress, and close with `task_result`.
3. Search relevant workspace memory before acting. For a handoff, review, or resume, inspect `activity_list` and `briefing_list` too.
4. Write durable facts and decisions to {durable_label}. A fact needs a `subject_anchor`; routine progress belongs in activity records, not memory.
5. Before requesting manual setup or a raw secret, check available credential references and connector bindings. Run connector actions through {APP_NAME} when possible.

For full operational guidance, use the generated `AGENTS.md` or `CLAUDE.md` for the relevant repository.
"""


def _connection_key_value(api_key=None):
    return api_key or "{{" + ENV_PREFIX + "API_KEY}}"


def _build_mcp_json(base_url, api_key=None):
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
    return f"""# Claude Code
# Option 1 — CLI (recommended, adds to user-level config so it works in every project):
claude mcp add --transport http --scope user {APP_SLUG} {base_url}/mcp \\
  --header "Authorization: Bearer {key}"

# Option 2 — create .mcp.json in your repo root (workspace-level, committable):
# If committing, replace the key with an env var: "Bearer {_shell_api_key}"
{generic_json}

# Codex CLI: add this to ~/.codex/config.toml
[mcp_servers.{APP_SLUG}]
url = "{base_url}/mcp"
{codex_auth}

# OpenCode: add this under ~/.config/opencode/opencode.json
{opencode_json}

# Antigravity/Other MCP clients:
# Use this JSON block. File location varies by tool — see docs/integrations.md.
{antigravity_json}
"""


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
        return f"Save this as <code>CLAUDE.md</code> in the workspace repository root. It is workspace-centric; the configured MCP/API key determines the active {APP_NAME} agent."
    elif output_type == "agents_md":
        return f"Save this as <code>AGENTS.md</code> in the workspace repository root. It is workspace-centric and can be shared by multiple agents; the configured MCP/API key determines the active {APP_NAME} agent."
    elif output_type == "assistants_md":
        return "Paste this into the assistant's own onboarding or instruction field. It is meant for assistant-style agents that manage their own MCP configuration. Use the one-time key button here when you want the prompt to include a fresh bearer token."
    elif output_type == "mcp_json":
        return f"Use the section that matches your tool. For Claude Code, run the <code>claude mcp add</code> command or save the JSON as <code>.mcp.json</code> in your repo root. Antigravity uses the same MCP shape but expects <code>serverUrl</code> instead of <code>url</code>. The bearer key determines the active {APP_NAME} agent."
    elif output_type == "env":
        return "Optional. Paste these into your shell profile (<code>~/.bashrc</code>, <code>~/.zshrc</code>) or tool environment only if you want the tool to read values from environment variables. This does not configure MCP by itself."
    elif output_type == "instructions":
        return "Read this yourself. It explains which generated output to use, where to put it, and when to generate a one-time key."
    elif output_type == "session":
        return "Paste this into the first message or workspace instructions field for a single agent session."
    elif output_type == "verification":
        return "Paste this into your connected agent after setup. It runs the end-to-end verification flow and reports memory, credential, and connector access."
    return ""


def _build_verification_prompt(user_scope, workspace_scope):
    return f"""Run the {APP_NAME} verification flow end to end:

1. Call `workspace_sync` for `{workspace_scope}` without an `execution_id`. Save the returned execution ID and report how many pinned records, assigned activities, and changes came back.
2. Call `activity_update` with `task_description` set to "{APP_NAME} verification", `memory_scope` set to `{workspace_scope}`, `status` set to `active`, and `execution_id` set to the saved value.
3. Write a memory record to `{workspace_scope}` that says this agent is connected and is safe to use for this workspace. Pass the saved `execution_id` and capture the returned record ID.
4. Call `workspace_sync` again with the saved `execution_id`. Confirm that `memory_changes` contains the new record's stable change ID. Call `workspace_sync_ack` with `next_cursor` only after checking the page.
5. Call `memory_get` with `scope` set to `{workspace_scope}`, `view` set to `full`, and a small `limit`. Confirm that the returned records include the captured record ID.
6. Call `memory_search` in `{workspace_scope}` for an exact token from the record. If the first search returns no results, retry once with the exact token and the record's `topic`.
7. Call `memory_pinned` and report how many standing-context records came back. Zero is correct on a fresh install. `workspace_sync` already returned the workspace's pinned records; this call checks standing context across the caller's other authorized scopes.
8. Call `activity_search` for a word from your task description and confirm that this session's activity comes back.
9. Call `credential_list` and report whether credential references are visible. Do not reveal or print raw secrets.
10. Call `connectors_summary` to list visible connector capability and binding health. Then call `connectors_bindings_list` with no scope to see everything visible to this agent. Call it again with `scope` set to `{user_scope}` and again with `scope` set to `{workspace_scope}`. Report user-scoped and workspace-scoped bindings separately.
11. Call `connectors_actions_list` with a real connector type ID from `connectors_list`. Report whether connector actions are visible.
12. If at least one enabled binding is visible, call `connectors_bindings_test` on a non-destructive binding and report the result. If none are visible, say so.
13. Use `task_note` for intermediate updates. Finish with `activity_update`, the same `memory_scope` and `execution_id`, `status` set to `completed`, and a short `task_result`. If the session reloads or no active activity exists, open a fresh verification activity before closing it.
14. Call `workspace_sync` one last time before reporting completion. Process and acknowledge the delivered page.
15. Report which scope you wrote to and summarize the sync, memory, credential, and connector checks.

Use the full prefixed scope name exactly as shown. Do not use a plain workspace ID as a memory scope.
"""
