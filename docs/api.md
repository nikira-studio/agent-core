# API Reference

Base URL: `http://localhost:3500`

All responses use a standard JSON envelope:

```json
{"ok": true, "data": {}}
```

Errors follow the same shape:

```json
{
  "ok": false,
  "error": {
    "code": "SCOPE_DENIED",
    "message": "Access denied to this scope."
  }
}
```

The one exception: `GET /mcp` returns the MCP manifest directly.

---

## Authentication

Three types of callers, three types of credentials:

**Human users** (dashboard and admin operations) — use the session token returned after login:
```http
Authorization: Bearer <session-token>
```

**Agents** (memory, credentials, activity, MCP) — use the API key issued when the agent was created:
```http
Authorization: Bearer <agent-api-key>
```

**The Credential Broker** (internal credential resolution only) — use the broker credential from `data/broker.credential`:
```http
Authorization: Broker <broker-credential>
```

Agent API keys start with `ac_sk_`. Broker credentials start with `ac_broker_`. Session tokens are JWTs. Each endpoint accepts only the token type appropriate to the operation — don't mix them up.

---

## Public

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Service and database health |
| `GET` | `/spec/public` | Version, auth methods, and MCP endpoint — no auth required |
| `GET` | `/spec` | Full capability spec (agent auth required) |

---

## Auth

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| `POST` | `/api/auth/register` | None | Register a user. First user becomes admin. |
| `POST` | `/api/auth/login` | None | Login and receive a session token |
| `POST` | `/api/auth/logout` | Session | End the current session |
| `POST` | `/api/auth/password` | Session | Change password |
| `POST` | `/api/auth/otp/enroll` | Session + password | Start TOTP enrollment and receive QR data. Reset also requires current OTP. |
| `POST` | `/api/auth/otp/confirm` | Session | Confirm the first TOTP code and enable OTP |
| `POST` | `/api/auth/otp/verify` | Pending session | Verify OTP during login |
| `POST` | `/api/auth/otp/disable` | Session + password + OTP | Disable OTP on the account |
| `POST` | `/api/auth/users` | Admin session | Create a new user account |
| `PUT` | `/api/auth/users/{user_id}` | Admin session | Update user metadata or role |
| `DELETE` | `/api/auth/users/{user_id}` | Admin session | Delete a user account |

Register:
```json
{
  "email": "admin@example.com",
  "password": "long-password",
  "display_name": "Admin"
}
```

Login:
```json
{
  "email": "admin@example.com",
  "password": "long-password"
}
```

OTP verify (during login):
```json
{
  "session_id": "<pending-session-id>",
  "otp_code": "123456"
}
```

---

## Agents

Session required. Admins can manage all agents. Non-admin users can create and manage agents they own. Personal `user:<id>` scopes are limited to that owner; use `workspace:<id>` for multi-user collaboration. Agents marked `shared` are visible read-only to other authenticated users, but editing, key rotation, and delete controls stay with the owner or an admin.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/agents` | List agents |
| `POST` | `/api/agents` | Create agent |
| `GET` | `/api/agents/{agent_id}` | Get agent |
| `PUT` | `/api/agents/{agent_id}` | Update metadata and scopes |
| `DELETE` | `/api/agents/{agent_id}` | Deactivate agent |
| `POST` | `/api/agents/{agent_id}/activate` | Reactivate a deactivated agent |
| `POST` | `/api/agents/{agent_id}/purge` | Permanently delete agent record (admin only) |
| `POST` | `/api/agents/{agent_id}/rotate_key` | Rotate agent API key |

Create:
```json
{
  "id": "coding-agent",
  "display_name": "Coding Agent",
  "description": "Local coding assistant",
  "default_user_id": "admin",
  "read_scopes": ["agent:coding-agent", "user:admin", "workspace:my-workspace"],
  "write_scopes": ["agent:coding-agent"]
}
```

Agent IDs are lowercase slugs: `a-z`, `0-9`, hyphens, and underscores. Colons are reserved for scope strings and are not allowed in IDs.

---

## Workspaces

Session required. Admins can manage all workspaces. Non-admin users can create and manage workspaces they own. The scope prefix is `workspace:<id>`. A workspace is the shared collaboration scope; other users and agents can be granted access to `workspace:<id>` even if they do not own the workspace row themselves.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/workspaces` | List visible workspaces |
| `POST` | `/api/workspaces` | Create workspace |
| `GET` | `/api/workspaces/{workspace_id}` | Get workspace |
| `PUT` | `/api/workspaces/{workspace_id}` | Update owned workspace |
| `DELETE` | `/api/workspaces/{workspace_id}` | Deactivate owned workspace |
| `POST` | `/api/workspaces/{workspace_id}/activate` | Reactivate a deactivated workspace |
| `POST` | `/api/workspaces/{workspace_id}/purge` | Permanently delete workspace record (admin only) |
| `GET` | `/api/workspaces/{workspace_id}/collaborators` | List workspace collaborators |
| `PUT` | `/api/workspaces/{workspace_id}/collaborators/{user_id}` | Add or update a collaborator |
| `DELETE` | `/api/workspaces/{workspace_id}/collaborators/{user_id}` | Remove a collaborator |

Create:
```json
{
  "id": "my-project",
  "name": "My Project",
  "description": "Shared workspace for the team"
}
```

Inactive workspaces no longer authorize reads or writes to `workspace:<id>`.

Workspace sharing is user-level first: the workspace owner or an admin grants collaborator access to specific users, and then those users can scope their own agents to the shared workspace. Agents still have their own owner/default user, so sharing a workspace does not change agent ownership or attribution.

---

## Memory

Agent or session authentication accepted, with scope enforcement on every operation.

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/memory/write` | Write a memory record |
| `POST` | `/api/memory/import` | Import curated notes into memory records |
| `POST` | `/api/memory/search` | Search memory (FTS5 + optional semantic hybrid search) |
| `POST` | `/api/memory/get` | List records by scope |
| `POST` | `/api/memory/retract` | Soft-delete a record |
| `POST` | `/api/memory/move` | Atomically relocate an active record to a new scope (write access to both scopes required) |
| `POST` | `/api/memory/restore` | Restore a retracted record |
| `GET` | `/api/memory/{record_id}` | Get one record |
| `GET` | `/api/memory/{record_id}/chain` | Get the supersession chain for a record |

Write:
```json
{
  "content": "Use two-space indentation for generated examples.",
  "memory_class": "preference",
  "scope": "agent:coding-agent",
  "domain": "engineering",
  "topic": "style",
  "confidence": 0.9,
  "importance": 0.6,
  "source_kind": "operator_authored",
  "slot_key": "style",
  "valid_from": null,
  "valid_to": null,
  "last_confirmed_at": null,
  "expires_at": null,
  "supersedes_id": null
}
```

Optional memory metadata:

- `slot_key` is for preference records when you want one active value per slot. A new preference with the same `scope + slot_key` supersedes the previous active one.
- `valid_from`, `valid_to`, and `last_confirmed_at` are freshness hints. They are optional and help retrieval prefer current records when present.
- `expires_at` is an ISO datetime after which the record is excluded from search results and swept on the next maintenance run. Useful for time-bounded facts or temporary scratchpad context.
- `provenance` is server-generated on write and records who wrote the memory and from which channel; clients do not supply it directly.

Import:
```json
{
  "scope": "workspace:example",
  "memory_class": "fact",
  "domain": "import",
  "topic": null,
  "sources": [
    {
      "filename": "memory.md",
      "content": "# Project Notes\n- Workspace: example\n- Decision: use workspace:example for durable memory\n- Fact: keep imports concise and specific"
    }
  ]
}
```

Imports are explicit, manual writes into the existing memory table. The server splits markdown/text into deterministic chunks, writes them with `source_kind: "external_import"`, and stamps provenance with `/api/memory/import`, source filename, and chunk number. Import provenance is generated server-side; raw credential values are never accepted as metadata. The import path is intended for curated notes, handoffs, and memory-shaped summaries, not for raw repository instruction files unless you have already distilled the durable facts you want to keep.

For files that are mostly instructions, such as `CLAUDE.md` or `AGENTS.md`, an AI-assisted extract-and-review flow is often a better fit than literal import. That keeps the memory table focused on durable facts, decisions, and preferences instead of recreating repo guidance verbatim.

Search:
```json
{
  "query": "indentation examples",
  "scope": "agent:coding-agent",
  "memory_class": "preference",
  "limit": 20,
  "include_retracted": false,
  "include_superseded": false
}
```

Get (list by scope):
```json
{
  "scope": "agent:coding-agent",
  "memory_class": "preference",
  "limit": 50
}
```

**Valid memory classes:** `fact`, `preference`, `decision`, `scratchpad`

**Valid source kinds:** `operator_authored`, `human_direct`, `tool_output`, `agent_inference`, `episodic_inference`, `semantic_inference`, `external_import`

Writes and imports to `shared` are rejected if the content looks like PII or credentials. Very short, noisy, or credential-like search queries are also rejected.

---

## Credentials

Credential responses never include raw `value` or `value_encrypted` fields.

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| `GET` | `/api/credentials/entries?scope={scope}` | Agent/session | List credential metadata and references in an authorized scope |
| `POST` | `/api/credentials/entries` | Agent/session | Create encrypted entry |
| `GET` | `/api/credentials/entries/{entry_id}` | Agent/session | Get credential metadata and reference |
| `PUT` | `/api/credentials/entries/{entry_id}` | Agent/session | Update entry metadata or value |
| `DELETE` | `/api/credentials/entries/{entry_id}` | Agent/session | Soft-delete entry |
| `POST` | `/api/credentials/entries/{entry_id}/reference` | Agent/session | Return `AC_SECRET_*` reference |
| `POST` | `/api/credentials/entries/{entry_id}/reveal` | Authorized user session | Reveal raw value |
| `GET` | `/api/credentials/scopes` | Agent/session | List authorized credential scopes |
| `POST` | `/api/credentials/rotate` | Admin | Rotate the credential encryption key |
| `GET` | `/api/credentials/rotate/status` | Admin | Get key rotation status |
| `POST` | `/api/credentials/restore-key` | Admin | Restore a previous encryption key |

A credential stores one encrypted secret value plus metadata. The dashboard manages credentials from the `/connectors` page.

Create:
```json
{
  "scope": "agent:coding-agent",
  "name": "service-token",
  "value": "<secret-value>",
  "label": "Service token",
  "expires_at": null,
  "metadata_json": "{}"
}
```

Update metadata while keeping the current encrypted value:
```json
{
  "name": "service-token",
  "label": "Service token"
}
```

Replace the encrypted value by including `value`:
```json
{
  "value": "<new-secret-value>"
}
```

The dashboard edit form follows the same rule: leaving the replacement secret field blank keeps the current value; entering a value overwrites it.

Rotate encryption key (re-encrypts all credential entries with a new key):
```json
{}
```

Rotation response:
```json
{
  "ok": true,
  "data": {
    "message": "Credential key rotated successfully",
    "re_encrypted_count": 12,
    "keyring_size": 2
  }
}
```

Restore a previous key (use to recover from a bad rotation, with a key from a backup):
```json
{"key_base64": "<base64-encoded-fernet-key>"}
```

---

## Connector Types

Connector types are the reusable definitions Agent Core builds from imported OpenAPI specs and native MCP servers, plus the built-in `generic_http` fallback. The connector catalog is instance-wide, so imported connector types are visible to other authenticated users in the same Agent Core deployment.

Provider types:

- `builtin` for built-in fallback connector types like `generic_http`
- `openapi` for imported REST/OpenAPI connector types
- `mcp` for native MCP server registrations

The connector type row stores provider metadata such as `endpoint_url`, `transport_type`, `capabilities_json`, and `tool_snapshot_json` when applicable.

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| `GET` | `/api/connector-types` | Agent/session | List installed connector types |
| `GET` | `/api/connector-types/{connector_type_id}/tools` | Agent/session | List actions for a connector type |
| `PUT` | `/api/connector-types/{connector_type_id}/actions` | Admin | Update the disabled actions list for a connector type |
| `POST` | `/api/connector-types/preview` | Admin | Parse an OpenAPI spec and return metadata without persisting |
| `POST` | `/api/connector-types/import` | Admin | Import an OpenAPI spec from URL or pasted JSON/YAML |
| `POST` | `/api/connector-types/import-mcp` | Admin | Register a native MCP server and discover its tools |
| `POST` | `/api/connector-types/{connector_type_id}/refresh` | Admin | Refresh MCP discovery metadata and tool snapshot |
| `GET` | `/api/connector-types/directory` | Agent/session | Browse the public API directory (grouped by provider, variants included) |

Import:
```json
{
  "url": "https://example.com/openapi.json",
  "display_name": "Example REST API"
}
```

If you import a spec, the resulting connector type is what you bind to a credential. That keeps the workflow predictable: spec first, credential second, binding last.

Import an MCP server:

```json
{
  "url": "https://mcp.example.com/mcp",
  "display_name": "Firecrawl MCP",
  "transport_type": "streamable_http",
  "timeout_ms": 60000,
  "headers_json": "{\"Authorization\":\"Bearer <token>\"}"
}
```

The MCP import stores the server URL and transport alongside the discovered tool snapshot. Use `streamable_http` for HTTP-native MCP servers. If your MCP server only speaks stdio, run it behind a small HTTP bridge/proxy and point Agent Core at that bridge instead. Refreshing an MCP connector type re-discovers the tool list and updates the snapshot in place.

For OpenAPI-backed connector bindings, `config_json` can override the target base URL with `base_url` and can suppress auth injection with `auth_mode: "none"` when the imported spec expects auth but the actual deployment does not. Internal hosts are allowed by default; if you want to block local probing, set `AGENT_CORE_BLOCK_INTERNAL_HOSTS=true` and add trusted names to `AGENT_CORE_ALLOWED_INTERNAL_HOSTS` as exceptions.

---

## Connector Bindings

Connector bindings link a stored credential to an external service so Agent Core can run actions against that service on behalf of an agent. The credential never leaves Agent Core — the connector executor uses it server-side.

Credential scope controls access to the stored secret. Binding scope controls where the connector is available. A binding currently points to one credential; non-secret connector settings belong in `config_json`.

Use `config_json` for fixed, non-secret defaults that should travel with the binding. Common examples:

- `base_url` or `test_url` for a generic HTTP binding
- `default_params` for imported APIs that always need the same repo, owner, workspace, or other context
- `auth_header`, `auth_scheme`, `auth_scheme_name`, `auth_location`, or `query_param` when the connector needs a specific non-secret auth layout
- `auth_mode: "none"` to suppress spec-driven auth injection for a trusted local deployment that does not actually require auth

If the connector and credential are enough on their own, leave `config_json` empty.

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| `GET` | `/api/connector-bindings` | Agent/session | List visible connector bindings |
| `POST` | `/api/connector-bindings` | Agent/session | Create a binding |
| `GET` | `/api/connector-bindings/{binding_id}` | Agent/session | Get a binding |
| `PUT` | `/api/connector-bindings/{binding_id}` | Agent/session | Update a binding |
| `DELETE` | `/api/connector-bindings/{binding_id}` | Agent/session | Delete a binding |
| `POST` | `/api/connector-bindings/{binding_id}/test` | Agent/session | Test the binding using the stored credential |
| `POST` | `/api/connector-bindings/{binding_id}/run` | Agent/session | Run a binding action directly through the REST API |
| `POST` | `/api/connectors/{binding_id}/run` | Agent/session | Alias of the line above (intuitive path for plug-in scripts) |
| `GET` | `/api/connector-bindings/{binding_id}/tools` | Agent/session | List the actions exposed by this binding |
| `GET` | `/api/connector-bindings/{binding_id}/executions` | Agent/session | List execution history for a binding |

### Running a connector action directly over REST

Plug-in scripts (e.g. a `no_agent` cron) that don't go through MCP call the binding's `run` endpoint themselves:

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $AGENT_CORE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"action": "add_torrent", "params": {"filename": "magnet:?xt=..."}}' \
  http://core.example.com/api/connector-bindings/<binding_id>/run
```

Body shape is `{"action": "...", "params": {...}}`. The canonical path is `/api/connector-bindings/{binding_id}/run`; `/api/connectors/{binding_id}/run` is accepted as an alias so the intuitive path doesn't 404. Note the **MCP `connectors_run` tool dispatches server-side and is not the same as this direct REST path** — testing only through MCP will not surface a wrong REST URL.

Create:
```json
{
  "connector_type_id": "example-rest-api",
  "name": "Workspace API",
  "scope": "workspace:my-workspace",
  "credential_id": "<credential-entry-id>",
  "config_json": "{\"default_params\":{\"owner\":\"nikira\",\"repo\":\"agent-core\"},\"auth_scheme_name\":\"personalAccessToken\"}",
  "enabled": true
}
```

In that example, the credential provides the secret token, while `config_json` tells Agent Core which repo context and auth scheme to apply when it turns the binding into a real request. For MCP bindings, `config_json` can also carry per-binding timeout and header overrides used during tool execution.

The dashboard's `/connectors` page provides a UI for all of these operations.

---

## Internal Broker

Only the local Credential Broker should call this route.

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/internal/credentials/resolve` | Resolve an `AC_SECRET_*` reference for a configured agent |

```json
{
  "variable_name": "AC_SECRET_SERVICE_TOKEN_1A2B3C4D",
  "agent_id": "coding-agent"
}
```

The broker credential authenticates the request. The `agent_id` must come from trusted runtime configuration — not model-generated text. Agent Core checks that agent's read scopes before returning a raw value.

---

## Activity

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| `POST` | `/api/activity` | Agent/session | Create an activity record |
| `GET` | `/api/activity` | Agent/session | List visible activities |
| `POST` | `/api/activity/pickup` | Agent | Claim the next active work item assigned to this agent in authorized scopes |
| `GET` | `/api/activity/{activity_id}` | Agent/session | Get activity |
| `PUT` | `/api/activity/{activity_id}` | Agent/session | Update status, metadata, or task result |
| `POST` | `/api/activity/{activity_id}/heartbeat` | Agent/session | Refresh heartbeat timestamp |
| `DELETE` | `/api/activity/{activity_id}` | Agent/session | Cancel activity |
| `POST` | `/api/activity/{activity_id}/recovery` | Admin | Reassign stale activity and generate briefing |

Create:
```json
{
  "task_description": "Refactor memory search tests",
  "memory_scope": "agent:coding-agent",
  "metadata_json": "{}"
}
```

**Valid statuses:** `active`, `stale`, `completed`, `blocked`, `cancelled`

Only the owning agent or an admin can update, heartbeat, cancel, or reassign an activity.

### Activity Pickup

`POST /api/activity/pickup` requires agent authentication. It finds the oldest `active` activity where `assigned_agent_id` matches the calling agent and `memory_scope` is within the agent's authorized read scopes, then heartbeats it to signal the claim.

Response when work is found:
```json
{
  "ok": true,
  "data": {
    "activity": { "id": "...", "task_description": "...", "assigned_agent_id": "...", ... },
    "message": null
  }
}
```

Response when nothing is waiting:
```json
{
  "ok": true,
  "data": {
    "activity": null,
    "message": "No assigned work found for this agent in authorized scopes"
  }
}
```

The pickup is workspace-aware: an agent configured with `workspace:proj-a` in its read scopes will only claim activities whose `memory_scope` is `workspace:proj-a`. It will never see activities assigned to a different agent or scoped to a workspace it cannot read. Activity records are a mailroom-style handoff board, not orchestration: they store work, status, and ownership, but the agent runtime still has to explicitly check for and claim work.

---

## Briefings

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/briefings/handoff` | Generate briefing for an activity |
| `POST` | `/api/briefings/handoff/prd` | Generate a PRD-shaped briefing from one agent to another |
| `GET` | `/api/briefings/{briefing_id}` | Get briefing |

Standard handoff (based on an active activity):
```json
{"activity_id": "<activity-id>"}
```

PRD-shaped handoff (agent-to-agent transition):
```json
{
  "from_agent_id": "coding-agent",
  "to_agent_id": "review-agent",
  "user_id": "admin"
}
```

Briefings include authorized decision, fact, and preference memory linked to the activity scope, plus the source activity's task result when one was recorded.

---

## MCP

Agent authentication required.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/mcp` | Return MCP manifest |
| `POST` | `/mcp` | Dispatch one MCP tool call |

Dispatch:
```json
{
  "tool": "memory_search",
  "params": {
    "query": "handoff decisions",
    "scope": "agent:coding-agent"
  }
}
```

**Available tools:**

| Tool | What it does |
| --- | --- |
| `memory_search` | Search memory by query. No `scope` = your default recall scopes; pass `scope` to target one specific readable scope on demand |
| `memory_get` | List records in an authorized scope. `view='compact'` surveys a scope (metadata + content preview, no full bodies); defaults to compact for large pages, full for small. Supports `limit`/`offset` |
| `memory_write` | Write a memory record |
| `memory_retract` | Soft-delete a memory record |
| `memory_move` | Atomically relocate an active record to a new scope (preserves content/class/topic + lineage; write access to both scopes required) |
| `credential_get` | Get an `AC_SECRET_*` reference for a credential entry |
| `credential_list` | List credential metadata and references in authorized scopes |
| `activity_update` | Create or update an activity record, including progress notes and completion result |
| `activity_get` | Get an activity record |
| `activity_list` | List activities visible to the current caller |
| `activity_pickup` | Claim the next active work item assigned to this agent in authorized scopes |
| `get_briefing` | Fetch a briefing |
| `briefing_list` | List briefings visible to the current caller |
| `connectors_list` | List installed connector types as lean summaries (id, name, auth/backend type, action_count; no full specs). Supports `limit`/`offset`; use `connectors_actions_list` for a type's actions |
| `connectors_bindings_list` | List connector bindings in authorized scopes |
| `connectors_bindings_test` | Test a binding using its stored credential |
| `connectors_actions_list` | List actions supported by a connector type |
| `connectors_summary` | Summarize visible connector capability, binding, credential-presence, action, and health state |
| `connectors_run` | Run one connector action server-side using a binding |

---

## Dashboard API

Session authentication required. Global overview and audit routes require admin.

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/dashboard/search` | Search memory, activities, briefings, and connector visibility |
| `GET` | `/api/dashboard/overview` | Counts and recent activity |
| `GET` | `/api/dashboard/memory` | Dashboard memory list |
| `GET` | `/api/dashboard/credentials` | Dashboard credential list |
| `GET` | `/api/dashboard/audit` | Audit event list (admin only) |
| `GET` | `/api/dashboard/audit/export` | Export audit log as CSV (admin only) |
| `GET` | `/api/dashboard/activity` | Activity list |
| `GET` | `/api/dashboard/activity/summary` | Activity summary |
| `POST` | `/api/dashboard/prune` | Manual prune/archive of audit or activity rows (admin only) |
| `POST` | `/api/dashboard/system-settings` | Update system settings (admin only) |
| `POST` | `/api/dashboard/vector-settings` | Update vector search settings (admin only) |
| `POST` | `/api/dashboard/vector-settings/test` | Test the vector search endpoint (admin only) |
| `GET` | `/api/dashboard/vector-settings/models` | List available vector models from the configured endpoint (admin only) |
| `POST` | `/api/dashboard/broker/rotate` | Rotate broker credential (admin only) |

---

## Events

Session authentication required (cookie-based only; agent tokens are not accepted on this endpoint).

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/events` | Open a live SSE stream for dashboard events |

The events endpoint returns a persistent `text/event-stream` response. Each message is a named SSE event with a JSON payload:

```
event: activity_created
data: {"type": "activity_created", "timestamp": "2026-05-18T12:34:56.789Z", "data": {...}}
```

**Emitted event types:**

| Event | Trigger |
| --- | --- |
| `activity_created` | A new activity record is created |
| `activity_updated` | An activity status, metadata, progress note, or task result is updated |
| `activity_heartbeat` | An agent sends a heartbeat for an activity |
| `activity_cancelled` | An activity is cancelled |
| `activity_recovered` | A stale activity is reassigned or recovered |
| `connector_executed` | A connector binding action completes |

The `connector_executed` payload includes `binding_id`, `binding_name`, `scope`, `connector_type_id`, `connector_type_name`, `action`, `success`, `duration_ms`, `status`, and `error_message`.

The stream sends an SSE comment (`: heartbeat`) every 15 seconds to keep the connection alive through proxies. The browser's built-in `EventSource` API reconnects automatically on disconnect; Agent Core's dashboard client uses exponential backoff (1 s → 2 s → … → 30 s max).

This endpoint is used internally by the dashboard to push live updates to the overview stat cards, activity table, and connector execution indicator without polling.

---

## Integrations Setup

Session authentication required. These routes power the setup page that generates instructions, environment files, and verification prompts for a chosen agent/workspace.

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/integrations/preview` | Generate setup instructions and recommended scopes |
| `POST` | `/api/integrations/generate-connection` | Generate connection files for the selected agent/workspace |
| `POST` | `/api/integrations/apply-access` | Apply the selected access scopes to an agent |
| `POST` | `/api/integrations/apply-recommended-access` | Apply the recommended access scopes to an agent |

---

## Backup and Maintenance

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| `POST` | `/api/backup/export` | Admin | Download encrypted full backup archive and one-time backup key header |
| `POST` | `/api/backup/restore` | Admin | Restore an encrypted backup ZIP (`mode=replace_all` or `mode=merge`) |
| `GET` | `/api/backup/export/memory?fmt=jsonl` | Session | Export authorized memory as JSONL or CSV |
| `GET` | `/api/backup/export/credentials` | Session | Export credential metadata only (no raw values) |
| `GET` | `/api/backup/export/audit?fmt=csv` | Admin | Export audit log as CSV |
| `GET` | `/api/backup/startup-checks` | Admin | Run operational health checks |
| `POST` | `/api/backup/maintenance` | Admin | Run maintenance (prune stale activity and scratchpad memory) |

Restore uses multipart form data:

```bash
curl -X POST http://localhost:3500/api/backup/restore \
  -H "Authorization: Bearer <admin-session>" \
  -F "backup=@backup.zip.enc" \
  -F "backup_key=<one-time-backup-key>" \
  -F "mode=replace_all"
```

Use `mode=merge` to add records from the backup without overwriting existing records. Existing records (by primary key) are preserved.

---

## Webhooks

The Webhooks section covers two independent features:

- **Inbound receiver** — external systems push work commands into Agent Core
- **Outbound notifications** — Agent Core pushes event notifications to external endpoints

---

## Inbound Webhooks

Allows external systems (n8n, Zapier, custom scripts) to create and manage activities in Agent Core by sending authenticated HTTP POST commands.

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| `GET` | `/api/webhooks/inbound/key/status` | Admin | Check whether an inbound key exists |
| `POST` | `/api/webhooks/inbound/key` | Admin | Generate the first inbound key |
| `POST` | `/api/webhooks/inbound/key/rotate` | Admin | Rotate the inbound key |
| `POST` | `/api/webhooks/inbound` | Inbound key | Submit an inbound command |

### Authentication

Every inbound request must include the inbound key in a dedicated header:

```http
X-Agent-Core-Inbound-Key: ac_inbound_<token>
```

The key is installation-wide (one key at a time). Generate it once via the dashboard or API; it is shown once and then hashed. Rotation immediately invalidates the previous key.

### Generate key

```http
POST /api/webhooks/inbound/key
Authorization: Bearer <admin-session>
```

Response (201):

```json
{
  "ok": true,
  "data": {
    "key": "ac_inbound_<token>",
    "note": "Store this key — it will not be shown again."
  }
}
```

Returns 409 if a key already exists. Use the rotate endpoint to replace it.

### Rotate key

```http
POST /api/webhooks/inbound/key/rotate
Authorization: Bearer <admin-session>
```

Response (200): same shape as generate. The previous key is immediately deactivated.

### Submit a command

```http
POST /api/webhooks/inbound
X-Agent-Core-Inbound-Key: ac_inbound_<token>
Content-Type: application/json

{
  "event_type": "activity.create",
  "assigned_agent_id": "codex",
  "task_description": "Review webhook implementation",
  "memory_scope": "workspace:agent-core",
  "workspace": "workspace:agent-core"
}
```

### Supported event types

Commands use dot notation and imperative form to distinguish them from outbound notifications.

| Event type | Required fields | Optional fields |
| --- | --- | --- |
| `activity.create` | `assigned_agent_id` | `task_description`, `memory_scope`, `workspace` |
| `activity.assign` | `activity_id`, `assigned_agent_id` | `memory_scope` |
| `activity.update` | `activity_id` + at least one of: `status`, `task_description`, `task_note`, `task_result`, `memory_scope` | |
| `activity.cancel` | `activity_id` | `reason` |
| `activity.note` | `activity_id`, `note` | |

#### activity.create

Creates a new activity record. `assigned_agent_id` is used as both `agent_id` and `assigned_agent_id`. If `workspace` is supplied, Agent Core stores it as optional metadata on the activity record.

```json
{
  "event_type": "activity.create",
  "assigned_agent_id": "codex",
  "task_description": "Review webhook implementation",
  "memory_scope": "workspace:agent-core"
}
```

Response:

```json
{"ok": true, "data": {"activity_id": "abc123", "status": "active"}}
```

#### activity.assign

Reassigns an existing activity to a different agent.

```json
{
  "event_type": "activity.assign",
  "activity_id": "abc123",
  "assigned_agent_id": "opus"
}
```

#### activity.update

Updates status, description, result, or scope metadata of an existing activity.

```json
{
  "event_type": "activity.update",
  "activity_id": "abc123",
  "status": "completed",
  "task_description": "Reviewed and approved",
  "task_note": "Checked the diff and verified the schema change",
  "task_result": "Reviewed the patch and approved the change"
}
```

#### activity.cancel

Cancels an existing activity.

```json
{
  "event_type": "activity.cancel",
  "activity_id": "abc123",
  "reason": "superseded by new ticket"
}
```

#### activity.note

Appends a note to the activity's audit trail. Does not modify the activity record itself.

```json
{
  "event_type": "activity.note",
  "activity_id": "abc123",
  "note": "Handoff complete. Reviewed PR #42 and left inline comments."
}
```

### Error responses

| Status | Code | Meaning |
| --- | --- | --- |
| 401 | `UNAUTHORIZED` | Missing or invalid inbound key |
| 400 | `INVALID_PAYLOAD` | Unknown event type or missing required field |

---

## Outbound Webhooks

Admin-only. Manage outbound webhook registrations and view delivery history.

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| `POST` | `/api/webhooks` | Admin | Register a new webhook |
| `GET` | `/api/webhooks` | Admin | List all webhook registrations |
| `GET` | `/api/webhooks/{id}` | Admin | Get a single webhook registration |
| `PUT` | `/api/webhooks/{id}` | Admin | Update a webhook (name, url, secret, events, enabled) |
| `DELETE` | `/api/webhooks/{id}` | Admin | Delete a webhook and its delivery log |
| `POST` | `/api/webhooks/{id}/test` | Admin | Send a synthetic test delivery |
| `GET` | `/api/webhooks/{id}/deliveries` | Admin | List recent delivery attempts |

### Create webhook

```http
POST /api/webhooks
Authorization: Bearer <admin-session>
Content-Type: application/json

{
  "name": "n8n Activity Alerts",
  "url": "https://n8n.example.com/webhook/agent-core",
  "secret": "your-hmac-secret",
  "event_types": ["activity_cancelled", "activity_updated"]
}
```

Response:

```json
{
  "ok": true,
  "data": {
    "webhook": {
      "id": "abc123",
      "name": "n8n Activity Alerts",
      "url": "https://n8n.example.com/webhook/agent-core",
      "event_types": ["activity_cancelled", "activity_updated"],
      "enabled": true,
      "created_by": "admin",
      "created_at": "2026-05-18T10:00:00+00:00",
      "updated_at": "2026-05-18T10:00:00+00:00"
    }
  }
}
```

The secret is write-only. It is never returned in list or get responses.

### Supported event types

`activity_created`, `activity_updated`, `activity_heartbeat`, `activity_cancelled`, `activity_recovered`, `connector_executed`

### Delivery payload

Every delivery is a signed HTTP POST. The envelope is the same for all event types:

```json
{
  "event_type": "activity_cancelled",
  "timestamp": "2026-05-18T10:30:00.000000+00:00",
  "data": { ... }
}
```

**Activity event `data` fields** (`activity_created`, `activity_updated`, `activity_heartbeat`, `activity_cancelled`, `activity_recovered`):

```json
{
  "activity_id": "abc123",
  "task_description": "Refactor auth middleware",
  "task_note": "Applied the middleware change and started the test pass",
  "task_result": "Completed auth middleware refactor and added tests",
  "agent_id": "my-agent",
  "assigned_agent_id": "my-agent",
  "user_id": "admin",
  "memory_scope": "workspace:my-project",
  "status": "cancelled",
  "started_at": "2026-05-18T09:00:00+00:00",
  "updated_at": "2026-05-18T10:30:00+00:00",
  "heartbeat_at": "2026-05-18T10:25:00+00:00",
  "ended_at": "2026-05-18T10:30:00+00:00",
  "previous_status": "active"
}
```

`task_note` is optional and is typically populated for in-flight updates. `task_result` is optional and is typically populated when a task is completed. `previous_status` is present on `activity_updated` and `activity_cancelled`. `recovery_action` and `result` are present on `activity_recovered`.

**Connector event `data` fields** (`connector_executed`):

```json
{
  "binding_id": "bind123",
  "binding_name": "GitHub Actions",
  "scope": "workspace:my-project",
  "connector_type_id": "github",
  "connector_type_name": "GitHub",
  "action": "list_repos",
  "success": true,
  "duration_ms": 312,
  "status": "success",
  "error_message": null
}
```

`error_message` is non-null on failure.

Signature header: `X-Agent-Core-Signature: sha256=<hex>` (HMAC-SHA256 of raw body with webhook secret).

### Test delivery

```http
POST /api/webhooks/{id}/test
Authorization: Bearer <admin-session>
```

Sends a synthetic payload to the registered URL with `event_type: "test"`. The payload is not a replay of any prior delivery.

Response:

```json
{"ok": true, "data": {"ok": true, "http_status": 200}}
```

### Delivery log

```http
GET /api/webhooks/{id}/deliveries?limit=50
Authorization: Bearer <admin-session>
```

Returns an array of delivery attempts, ordered newest first. Each entry includes `status` (`success` or `failure`), `http_status`, `event_type`, and `error_message` if applicable.

---

## Rate Limits

Rate-limited endpoints include these response headers:

| Header | Value |
| --- | --- |
| `X-RateLimit-Limit` | The limit for this operation |
| `X-RateLimit-Remaining` | Requests remaining in the current window |
| `X-RateLimit-Reset` | Unix timestamp when the window resets |
