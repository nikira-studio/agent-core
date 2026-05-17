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
| `POST` | `/api/memory/search` | Search memory (FTS5 + optional semantic hybrid search) |
| `POST` | `/api/memory/get` | List records by scope |
| `POST` | `/api/memory/retract` | Soft-delete a record |
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
  "supersedes_id": null
}
```

Optional memory metadata:

- `slot_key` is for preference records when you want one active value per slot. A new preference with the same `scope + slot_key` supersedes the previous active one.
- `valid_from`, `valid_to`, and `last_confirmed_at` are freshness hints. They are optional and help retrieval prefer current records when present.
- `provenance` is server-generated on write and records who wrote the memory and from which channel; clients do not supply it directly.

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

Writes to `shared` are rejected if the content looks like PII or credentials. Very short, noisy, or credential-like search queries are also rejected.

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
| `POST` | `/api/connector-types/import` | Admin | Import an OpenAPI spec from URL or pasted JSON/YAML |
| `POST` | `/api/connector-types/import-mcp` | Admin | Register a native MCP server and discover its tools |
| `POST` | `/api/connector-types/{connector_type_id}/refresh` | Admin | Refresh MCP discovery metadata and tool snapshot |
| `GET` | `/api/connector-types/directory` | Agent/session | Browse the public API directory |

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

For OpenAPI-backed connector bindings, `config_json` can override the target base URL with `base_url` and can suppress auth injection with `auth_mode: "none"` when the imported spec expects auth but the actual deployment does not. That is the supported way to bind a published spec to a trusted internal deployment without disabling SSRF protection for arbitrary URLs. If the target hostname is private or internal, add it to `AGENT_CORE_ALLOWED_INTERNAL_HOSTS` in the Agent Core deployment environment so the validator can recognize it as operator-trusted.

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
| `GET` | `/api/connector-bindings/{binding_id}/tools` | Agent/session | List the actions exposed by this binding |
| `GET` | `/api/connector-bindings/{binding_id}/executions` | Agent/session | List execution history for a binding |

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

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/activity` | Create an activity record |
| `GET` | `/api/activity` | List visible activities |
| `GET` | `/api/activity/{activity_id}` | Get activity |
| `PUT` | `/api/activity/{activity_id}` | Update status or metadata |
| `POST` | `/api/activity/{activity_id}/heartbeat` | Refresh heartbeat timestamp |
| `DELETE` | `/api/activity/{activity_id}` | Cancel activity |
| `POST` | `/api/activity/{activity_id}/recovery` | Reassign stale activity and generate briefing |

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

---

## Briefings

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/briefings/handoff` | Generate handoff briefing for an activity |
| `POST` | `/api/briefings/handoff/prd` | Generate a PRD-shaped handoff from one agent to another |
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

Briefings include authorized decision, fact, and preference memory linked to the activity scope.

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
| `memory_search` | Search authorized memory by query |
| `memory_get` | List records in an authorized scope |
| `memory_write` | Write a memory record |
| `memory_retract` | Soft-delete a memory record |
| `credential_get` | Get an `AC_SECRET_*` reference for a credential entry |
| `credential_list` | List credential metadata and references in authorized scopes |
| `activity_update` | Create or update an activity record |
| `activity_get` | Get an activity record |
| `activity_list` | List activities visible to the current caller |
| `get_briefing` | Fetch a handoff briefing |
| `briefing_list` | List briefings visible to the current caller |
| `connectors_list` | List available connector types |
| `connectors_bindings_list` | List connector bindings in authorized scopes |
| `connectors_bindings_test` | Test a binding using its stored credential |
| `connectors_actions_list` | List actions supported by a connector type |
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
| `POST` | `/api/dashboard/system-settings` | Update system settings (admin only) |
| `POST` | `/api/dashboard/vector-settings` | Update vector search settings (admin only) |
| `POST` | `/api/dashboard/vector-settings/test` | Test the vector search endpoint (admin only) |
| `GET` | `/api/dashboard/vector-settings/models` | List available vector models from the configured endpoint (admin only) |
| `POST` | `/api/dashboard/broker/rotate` | Rotate broker credential (admin only) |

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

## Rate Limits

Rate-limited endpoints include these response headers:

| Header | Value |
| --- | --- |
| `X-RateLimit-Limit` | The limit for this operation |
| `X-RateLimit-Remaining` | Requests remaining in the current window |
| `X-RateLimit-Reset` | Unix timestamp when the window resets |
