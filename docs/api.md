# API Reference

Base URL: `http://localhost:3500`.

All API responses use a standard JSON envelope:

```json
{
  "ok": true,
  "data": {}
}
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

The only exception is `GET /mcp`, which returns the MCP manifest directly.

## Authentication

Agent Core has three types of callers, each with their own credential:

**Human users** (dashboard and admin operations) use the session token returned after login:

```http
Authorization: Bearer <session-token>
```

**Agents** (memory, vault, activity, MCP) use the API key issued when the agent was created:

```http
Authorization: Bearer <agent-api-key>
```

**The Credential Broker** (internal vault resolution only) uses the broker credential from `data/broker.credential`:

```http
Authorization: Broker <broker-credential>
```

Agent API keys start with `ac_sk_`. Broker credentials start with `ac_broker_`. Session tokens are JWTs. Don't mix them up — each endpoint accepts only the token type appropriate to the operation.

## Public

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Service and database health |
| `GET` | `/spec/public` | Version, auth methods, and MCP endpoint |

## Auth

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| `POST` | `/api/auth/register` | None | Register a user. The first user becomes admin. |
| `POST` | `/api/auth/login` | None | Login and receive a session token. |
| `POST` | `/api/auth/logout` | Session | End the current session. |
| `POST` | `/api/auth/password` | Session | Change password. |
| `POST` | `/api/auth/otp/enroll` | Session + password | Start TOTP enrollment and receive QR data. Reset also requires current OTP. |
| `POST` | `/api/auth/otp/confirm` | Session | Confirm the first TOTP code, enable OTP, and receive backup codes once. |
| `POST` | `/api/auth/otp/verify` | Pending session | Verify OTP during login. |
| `POST` | `/api/auth/otp/backup-codes` | Session | Regenerate backup codes and return them once. |

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

OTP verify:

```json
{
  "session_id": "<pending-session-id>",
  "otp_code": "123456"
}
```

## Agents

Session required. Admins can manage all agents. Agents belong to one owner/default user. Personal user scopes are limited to that owner; use `workspace:<id>` workspace scopes for multi-user collaboration. Non-admin users can create and manage agents they own; non-admin scope grants are limited to their own user scope, owned workspace scopes, owned agent scopes, and shared read access.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/agents` | List agents |
| `POST` | `/api/agents` | Create agent. Generate the one-time connection key from Integrations when ready. |
| `GET` | `/api/agents/{agent_id}` | Get agent |
| `PUT` | `/api/agents/{agent_id}` | Update metadata and scopes |
| `DELETE` | `/api/agents/{agent_id}` | Deactivate agent |
| `POST` | `/api/agents/{agent_id}/purge` | Permanently delete agent record (Admin only) |
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

IDs are lowercase slugs: `a-z`, `0-9`, hyphen, and underscore. Colons are reserved for scope strings and are not allowed inside ID components.

## Workspaces

Session required. Admins can manage all workspaces. Non-admin users can create and manage workspaces they own. The API uses `/api/workspaces` and the scope prefix is `workspace:<id>`.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/workspaces` | List visible workspaces |
| `POST` | `/api/workspaces` | Create workspace |
| `GET` | `/api/workspaces/{workspace_id}` | Get workspace |
| `PUT` | `/api/workspaces/{workspace_id}` | Update owned workspace |
| `DELETE` | `/api/workspaces/{workspace_id}` | Deactivate owned workspace |

Create:

```json
{
  "id": "agent-core",
  "name": "Agent Core",
  "description": "Local agent memory and credential hub"
}
```

Inactive workspaces no longer authorize `workspace:<id>` reads or writes.

## Memory

Agent or session authentication is accepted, with scope enforcement on every operation.

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/memory/write` | Write memory |
| `POST` | `/api/memory/search` | Search memory through FTS5 with semantic hybrid retrieval when available |
| `POST` | `/api/memory/get` | List records by authorized scope |
| `POST` | `/api/memory/retract` | Retract a record; accepts `{"record_id":"..."}` or `?record_id=...` |
| `GET` | `/api/memory/{record_id}` | Get one memory record |
| `GET` | `/api/memory/{record_id}/chain` | Get supersession chain for a record |

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
  "supersedes_id": null
}
```

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

Get:

```json
{
  "scope": "agent:coding-agent",
  "memory_class": "preference",
  "limit": 50
}
```

Valid memory classes: `fact`, `preference`, `decision`, `scratchpad`.

Valid source kinds: `operator_authored`, `human_direct`, `tool_output`, `agent_inference`, `episodic_inference`, `semantic_inference`, `external_import`.

Writes to `shared` are rejected if the content appears to contain PII or credentials. Searches reject very short, noisy, or credential-like queries.

## Vault

Vault responses never include raw `value` or `value_encrypted` fields.

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| `GET` | `/api/vault/entries?scope={scope}` | Agent/session | List masked entries in an authorized scope |
| `POST` | `/api/vault/entries` | Agent/session | Create encrypted entry |
| `GET` | `/api/vault/entries/{entry_id}` | Agent/session | Get masked entry |
| `PUT` | `/api/vault/entries/{entry_id}` | Agent/session | Update entry metadata or value |
| `DELETE` | `/api/vault/entries/{entry_id}` | Agent/session | Soft-delete entry |
| `POST` | `/api/vault/entries/{entry_id}/reference` | Agent/session | Return `AC_SECRET_*` reference |
| `POST` | `/api/vault/entries/{entry_id}/reveal` | Authorized user session + OTP | Reveal raw value |
| `GET` | `/api/vault/scopes` | Agent/session | List authorized vault scopes |
| `POST` | `/api/vault/rotate` | Admin + OTP | Rotate the vault encryption key |
| `GET` | `/api/vault/rotate/status` | Admin | Get vault key rotation status |
| `POST` | `/api/vault/restore-key` | Admin + OTP | Restore a previous vault key |

Create:

```json
{
  "scope": "agent:coding-agent",
  "name": "github-token",
  "value": "<secret-value>",
  "label": "GitHub token",
  "value_type": "api",
  "expires_at": null,
  "metadata_json": "{}"
}
```

Valid `value_type` category values: `api`, `password`, `url`, `config`, `other`.

Reveal:

```json
{
  "otp_code": "123456"
}
```

**Vault key rotation** (`POST /api/vault/rotate`) replaces the primary encryption key. All vault entries are re-encrypted with the new key. The previous key is retained in the keyring for decryption of older entries. Requires admin session and valid OTP code.

Rotate:

```json
{
  "otp_code": "123456"
}
```

Response:

```json
{
  "ok": true,
  "data": {
    "message": "Vault key rotated successfully",
    "re_encrypted_count": 12,
    "keyring_size": 2
  }
}
```

**Restore key** (`POST /api/vault/restore-key`) installs a specific key as the new primary. Use this to restore from a pre-rotation backup. Requires admin session and valid OTP code.

Restore:

```json
{
  "key_base64": "<base64-encoded-fernet-key>",
  "otp_code": "123456"
}
```

## Internal Broker

Only the local credential broker should call this route.

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/internal/vault/resolve` | Resolve an `AC_SECRET_*` reference for a configured agent |

```json
{
  "variable_name": "AC_SECRET_GITHUB_TOKEN_1A2B3C4D",
  "agent_id": "coding-agent"
}
```

The broker credential authenticates the broker. The `agent_id` must come from trusted runtime configuration, not model-generated text. Agent Core then checks that agent's read scopes before returning a raw value.

## Activity

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/activity` | Create active activity |
| `GET` | `/api/activity` | List visible activities |
| `GET` | `/api/activity/{activity_id}` | Get activity |
| `PUT` | `/api/activity/{activity_id}` | Update status/metadata |
| `POST` | `/api/activity/{activity_id}/heartbeat` | Refresh heartbeat |
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

Valid statuses: `active`, `stale`, `completed`, `blocked`, `cancelled`.

Only the owning agent or an admin can update, heartbeat, cancel, or reassign an activity.

## Briefings

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/briefings/handoff` | Generate handoff briefing for an activity |
| `POST` | `/api/briefings/handoff/prd` | Generate PRD-shaped handoff from one agent to another |
| `GET` | `/api/briefings/{briefing_id}` | Get briefing |

```json
{
  "activity_id": "<activity-id>"
}
```

Briefings include authorized decision, fact, and preference memory linked to the activity scope.

PRD-shaped handoff:

```json
{
  "from_agent_id": "coding-agent",
  "to_agent_id": "review-agent",
  "user_id": "admin"
}
```

## MCP

Agent authentication required.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/mcp` | Return MCP manifest directly |
| `POST` | `/mcp` | Dispatch one MCP tool |

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

Tools: `memory_search`, `memory_get`, `memory_write`, `memory_retract`, `vault_get`, `vault_list`, `activity_update`, `activity_get`, `get_briefing`.

## Dashboard API

Session authentication required. Global overview and audit routes require admin.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/dashboard/overview` | Counts and recent activity |
| `GET` | `/api/dashboard/memory` | Dashboard memory list |
| `GET` | `/api/dashboard/vault` | Dashboard vault list |
| `GET` | `/api/dashboard/audit` | Audit event list, admin only |
| `GET` | `/api/dashboard/activity` | Activity list |
| `GET` | `/api/dashboard/activity/summary` | Activity summary |
| `POST` | `/api/dashboard/broker/rotate` | Rotate broker credential, admin only |

## Backup and Maintenance

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| `POST` | `/api/backup/export` | Admin + OTP | Download full backup ZIP |
| `POST` | `/api/backup/restore` | Admin + OTP | Restore backup ZIP with `mode=replace_all` or `mode=merge` |
| `GET` | `/api/backup/export/memory?fmt=jsonl` | Session | Export authorized memory as JSONL or CSV |
| `GET` | `/api/backup/export/vault` | Session | Export vault metadata only |
| `GET` | `/api/backup/export/audit?fmt=csv` | Admin | Export audit CSV |
| `GET` | `/api/backup/startup-checks` | Admin | Run operational checks |
| `POST` | `/api/backup/maintenance` | Admin | Run maintenance hooks |

Restore uses multipart form data:

```bash
curl -X POST http://localhost:3500/api/backup/restore \
  -H "Authorization: Bearer <admin-session>" \
  -F "backup=@backup.zip" \
  -F "otp_code=123456" \
  -F "mode=replace_all"
```

Use `mode=merge` to insert records that do not already exist while preserving current records on primary-key conflicts.

Rate-limited endpoints include `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset` headers. `X-RateLimit-Reset` is a Unix timestamp.
