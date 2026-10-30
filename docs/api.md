# API reference

Every REST endpoint and MCP tool. For what the concepts mean (scopes, memory classes, anchors, the review queue), see [How it works](how-it-works.md).

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

**`503 DATABASE_UNAVAILABLE`** can come back from any endpoint that touches the database. It means a full restore is holding the database still while it swaps files; the response carries a `Retry-After` and the condition clears in seconds. It is deliberately a refusal rather than a wait, because waiting would stall every other request in the process. See [Backup & Restore](backup-restore.md#restore).

---

## Authentication

Three types of callers, three types of credentials:

**Human users** (dashboard and admin operations) use the session token returned after login:
```http
Authorization: Bearer <session-token>
```

**Agents** (memory, credentials, activity, MCP) use the API key issued when the agent was created:
```http
Authorization: Bearer <agent-api-key>
```

**The Credential Broker** (internal credential resolution only) uses the broker credential from `data/broker.credential`:
```http
Authorization: Broker <broker-credential>
```

Agent API keys start with `ac_sk_`. Broker credentials start with `ac_broker_`. Session tokens are JWTs. Each endpoint accepts only the token type appropriate to the operation; don't mix them up.

---

## Public

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Service and database health |
| `GET` | `/spec/public` | Version, auth methods, and MCP endpoint; no auth required |
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
| `GET` | `/api/auth/effective-authority` | Session or agent | The caller's current effective authority: identity, read and write scopes, workspaces, and capabilities |
| `POST` | `/api/auth/users` | Admin session | Create a new user account |
| `PUT` | `/api/auth/users/{user_id}` | Admin session | Update user metadata, role, or `is_active` status |
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

Session required. Admins can manage all agents. Non-admin users can create and manage agents they own. At request time, every active agent inherits read access to `user:<principal_user_id>`; this does not grant user-scope write access. Personal `user:<id>` scopes are limited to that principal; use `workspace:<id>` for multi-user collaboration. Agents marked `shared` are visible read-only to other authenticated users, but editing, key rotation, and delete controls stay with the owner or an admin.

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

The two workspace read routes accept either a dashboard session or an agent API key. An agent sees only workspaces in its effective read scopes. All workspace creation, management, activation, deletion, and collaborator routes require a dashboard session. Admins can manage all workspaces. Non-admin users can create and manage workspaces they own.

The scope prefix is `workspace:<id>`. A workspace is the shared collaboration scope. Other users and agents can receive access to `workspace:<id>` without owning the workspace row.

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

Workspace sharing is user-level first: the workspace owner or an admin grants collaborator access to specific users, and then those users can scope their own agents to the shared workspace. At execution time, Agent Core intersects each stored workspace scope with the agent principal's current `can_read` or `can_write` collaborator bit. An agent's `default_user_id` is that principal; `owner_user_id` manages the agent record and is only its fallback. Sharing a workspace therefore does not change agent ownership or attribution, and a permission downgrade takes effect without editing the agent.

---

## Memory

Agent or session authentication accepted, with scope enforcement on every operation.

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/memory/write` | Write a memory record |
| `POST` | `/api/memory/search` | Search memory (FTS5 + optional semantic hybrid search) |
| `POST` | `/api/memory/get` | List records by scope |
| `POST` | `/api/memory/confirm` | Confirm a record with evidence |
| `POST` | `/api/memory/retract` | Soft-delete a record |
| `POST` | `/api/memory/move` | Atomically relocate an active record to a new scope (write access to both scopes required) |
| `POST` | `/api/memory/restore` | Restore a retracted record (retraction is reversible) |
| `GET` | `/api/memory/{record_id}` | Get one record |
| `GET` | `/api/memory/{record_id}/chain` | Get the supersession chain for a record |
| `DELETE` | `/api/memory/{record_id}` | Permanently delete a record and its embedding (unlike retract, this is not recoverable) |

Write:
```json
{
  "content": "Use two-space indentation for generated examples.",
  "memory_class": "preference",
  "scope": "agent:coding-agent",
  "topic": "style",
  "confidence": 0.9,
  "importance": 0.6,
  "source_kind": "agent_inference",
  "slot_key": "style",
  "valid_from": null,
  "valid_to": null,
  "subject_anchor": "repo:docs/style-guide.md",
  "expires_at": null,
  "supersedes_id": null
}
```

Choosing `memory_class`: a **fact** is settled by checking (someone could verify it against the code, a host, or a service), a **decision** is settled by someone deciding and nothing can verify it. Reporting what you just did is neither; that belongs in `activity_update`.

Optional memory metadata:

- `subject_anchor` names what would confirm a fact: `repo:<path>`, `host:<name-or-ip>`, or `service:<binding_id>`. Repo paths are **relative to the workspace root**, never absolute: the same directory has a different absolute path in every agent's container. Omit it when nothing could verify the record, which is normal for a decision.
- `pinned` marks a record as standing context: returned by `memory_pinned` at session start rather than having to win a search, included in every session context, and skipped by the clean-up rules. **Agents request pinning rather than performing it.** `memory_pin` queues the request for operator review, because standing context reaches every session in the scope including other agents', which makes it the most influential thing an agent could write. Capped per scope (10 by default, `max_pinned_per_scope`) so the list stays short enough to read in full. Only `fact`, `decision` and `preference` records can be pinned.
- `slot_key` is for preference records when you want one active value per slot. A new preference with the same `scope + slot_key` supersedes the previous active one.
- `last_confirmed_at` means the record was checked **against the world**. It is set by `memory_confirm` (which requires evidence naming what was checked) or by the verification pass, not by writing the record. Search results report `days_since_confirmed` so a caller can tell a fact verified today from one nobody has checked in months.
- `valid_from` and `valid_to` bound **when the content was true in the world**, which is not the same question as when the system learned it (`created_at`, `status_changed_at`). Keeping the two apart is what lets the corpus answer "what was true in March" rather than only "what do we believe now". Left unset, a record is taken to start when it was written, so nothing silently claims to have always been true. Supersession closes the old record's `valid_to` automatically, at the successor's `valid_from` when one is given, otherwise at the moment of supersession, and never overwrites an end date the writer set deliberately.
- `expires_at` is an ISO datetime after which the record is excluded from search results and swept on the next maintenance run. Useful for time-bounded facts or temporary scratchpad context.
- `confidence` and `importance` are caller-assigned. Ranking uses observed evidence: how often a record is recalled, whether callers said it helped, and how long since it was confirmed. So there is no need to tune them.
- `provenance` is server-generated on write and records who wrote the memory, from which channel, and which activity was open at the time; clients do not supply it directly.

Search:
```json
{
  "query": "indentation examples",
  "scope": "agent:coding-agent",
  "memory_class": "preference",
  "subject_anchor": "repo:docs",
  "limit": 20,
  "include_retracted": false,
  "include_superseded": false
}
```

`subject_anchor` filters by prefix, so `repo:app/services` matches everything anchored under it: useful for "what have we recorded about this part of the code".

`as_of` asks the corpus what it held to be true at a given moment instead of now. A record is returned when the instant falls inside its validity window, so a fact that has since been replaced comes back for dates before the replacement and its successor comes back for dates after: useful when reconstructing why a past decision looked right at the time. Superseded records are eligible under `as_of` (that is the point); retracted records never are, because retraction says the record should not have been written rather than that it stopped being true. A value that is not a parseable timestamp is rejected with `INVALID_INPUT`.

`activity_id` filters to records written during one piece of work. Every write already cites the activity that was open at the time, stamped server-side; this is the traversal back the other way. `activity_get` returns the same set as `produced_records`: picking up someone else's work shows what it concluded, not only what it was attempting.

**Search results are a lean projection**: `id`, `content`, `memory_class`, `scope`, `topic`, `subject_anchor`, `created_at`, `last_confirmed_at`, `record_status`, and a derived `days_since_confirmed`. Lifecycle columns and caller-assigned scores are omitted because a result is for deciding what is relevant, and on a full page that bookkeeping cost more context than the content it surrounded. Pass `"view": "full"` for every column, or use `memory_get` / `GET /api/memory/{id}` to inspect one record.

Keyword candidates use FTS5 BM25 relevance over content and topic. A bounded lexical contribution survives the final merge with optional semantic results and the existing confirmation/usefulness signals. Matching still requires every sanitized query token. Current records retain priority, and age penalties apply to facts rather than decisions. `limit` defaults to 20 and positive limits are capped at 100. `offset` selects a later page after ranking eligible records. Ranking reads only metadata; complete records are loaded for the selected page. Semantic search ignores stored vectors from a different model or with incompatible dimensions. Switching models preserves keyword recall of existing records but does not regenerate their embeddings automatically.

Both `memory_search` and `POST /api/memory/search` accept optional `max_response_chars`. On REST, omitted or null preserves the existing response shape. On MCP, omitted or null defaults the budget to `AGENT_CORE_TOOL_RESULT_SPILL_THRESHOLD`, so an oversized search returns its best whole records inline instead of being offloaded to a `result_fetch` handle, and recall is counted only for records actually delivered. Otherwise, pass a positive integer to bound the logical JSON `data` object after projection and pagination. The exact count is `len(json.dumps(data, indent=2, default=str))`, with Python's default ASCII escaping. This matches JSON-RPC tool text; REST uses the same budget convention even though its HTTP JSON is compact. Headers and transport wrappers are outside the budget. This is a character bound, not a model-specific token count.

Budgeted responses add `response_limited`. When true, the response contains the longest whole-record prefix that fits. Records are never clipped or skipped to pack in lower-ranked results. `total` remains the number of returned records. To continue, advance `offset` by that number; when the page is empty because its first record is too large, raise the budget instead.

If the first matching record does not fit, the response contains an empty list and a `warnings` entry with code `RESPONSE_BUDGET_TOO_SMALL`. That warning also consumes the budget. If the required metadata and warning cannot fit, the request fails with HTTP 400 and that code. Invalid budget types or values fail REST model validation with HTTP 422; MCP returns HTTP 400 with `INVALID_RESPONSE_BUDGET`. Error responses are outside the success-response budget.

Recall counts and audit result counts cover only selected records. For a spilled MCP response, selected records receive recall credit once when the logical response is produced; fetching its chunks does not increment counts again. Budgets below the configured spill threshold remain inline. Larger allowed results retain the existing spill behavior.

Example MCP arguments:

```json
{"query": "retry policy", "limit": 20, "offset": 0, "max_response_chars": 6000}
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

`last_confirmed_at` is read-only on ordinary writes. Confirm after writing by calling `memory_confirm` or `POST /api/memory/confirm` with `{"record_id": "...", "evidence": "what was checked"}`. Agent-authenticated callers cannot claim `operator_authored` or `human_direct`, and `external_import` cannot be written; it only labels records brought in by import paths that have since been removed.

A successful write can include advisory `warnings`. `POSSIBLE_DUPLICATE` identifies similar active records, while `PREVIOUSLY_RETRACTED` identifies similar retracted records so the caller can decide whether the new assertion is intentional. These warnings never block the write. Semantic candidates are evaluated independently for active and retracted records; when embeddings are unavailable, normalized prefix matching still supplies retraction awareness.

Writes to `shared` are rejected if the content looks like PII or credentials. Very short, noisy, or credential-like search queries are also rejected.

---

## Memory review queue

Admin only. These endpoints suggest and record decisions about what to retract or pin. **Rules propose; they never apply.** Agents write memory; they do not prune or elevate each other's, which is why nothing here is reachable with an agent key.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/memory/proposals` | List proposals (`status`, `scope`, `rule`, `limit`, `offset`; defaults to `status=pending`) |
| `POST` | `/api/memory/proposals/generate` | Run the mechanical rules and queue what they find |
| `POST` | `/api/memory/proposals/{id}/decide` | Record a verdict, applying the action on accept |
| `POST` | `/api/memory/proposals/{id}/reanchor` | Repoint a record at what actually describes it |
| `GET` | `/api/memory/proposals/stats` | Per-rule verdict history: how often each rule has been right |

Proposals come from two places. **Generation rules** are mechanical. They run during the configured maintenance interval when `consolidation_scan_enabled` is on, and on demand from the dashboard's review page or `POST /api/memory/proposals/generate`:

| Rule | Finds |
| --- | --- |
| `episodic_log` | A task log written as durable memory |
| `ticket_closeout` | A record about work that has since closed |
| `duplicate_cluster` | Near-identical records |
| `stale_volatile` | Facts nobody has confirmed in a long time (facts only, a decision does not go stale because time passed) |
| `unconfirmed_inference` | Non-human facts that have never been confirmed and are older than `unconfirmed_inference_days` |

**Event-driven proposals** are queued as things happen: `anchor_missing` when verification finds the subject gone and `pin_request` when an agent calls `memory_pin`.

Generate:
```json
{ "scope": "workspace:example", "rules": ["duplicate_cluster"] }
```
`rules` is optional; omitting it runs all of them. An unknown rule name is rejected with `UNKNOWN_RULE`. Proposals already pending or decided for the same rule and targets are skipped rather than duplicated, so the queue converges instead of re-asking. Generation respects a per-rule-and-scope pending cap, an installation-wide pending cap, and a per-run creation budget. Groups are rotated fairly so a busy scope cannot consume every run.

Decide:
```json
{ "verdict": "accepted", "outcome": "no_longer_current" }
```
`verdict` is `accepted` or `rejected`. `outcome` applies to confirm-style proposals, where both answers are accepts because the rule was right to ask either way: `still_current` resolves the question without claiming the record was independently verified, while `no_longer_current` retracts it. Use **Confirm with evidence** in the dashboard, `memory_confirm`, or `POST /api/memory/confirm` to set `last_confirmed_at`. Confirming from a proposal card confirms the record first and then resolves that proposal as `still_current`.

Accepting a retraction is reversible with `POST /api/memory/restore`. That is deliberate, a review that cannot be wrong cheaply is a review nobody will run.

Every decision is written to the audit log with the rule, verdict, outcome, and the records affected.

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

`expires_at` must be an ISO 8601 instant; anything else is rejected with `422` rather than stored. Sending `null` **clears** the expiry, while omitting the field leaves it alone, the two mean different things. Past its expiry a credential stops resolving: `reveal` answers `410 CREDENTIAL_EXPIRED`, and the broker refuses it. An expiry that cannot be parsed at all (a row written before this was validated) is treated as expired rather than raising. The dashboard shows expired and expiring-soon badges on the credentials list.

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

Restoration is rejected if any stored encrypted entry cannot be decrypted with the supplied key. For data encrypted under multiple keys, restore a full backup with its matching keyring. See [key restoration](security.md#rotating-the-credential-encryption-key).

---

### Credential requests

An agent calls the MCP `credential_request` tool. People see open requests at `GET /api/credentials/requests` and fill one with `POST /api/credentials/requests/{id}/fulfill` (`value`, optional `label` and `expires_at`) or decline it with `POST /api/credentials/requests/{id}/cancel`. Both need a human session with write access to the scope.

## Connector types

Connector types are the reusable definitions Agent Core builds from imported OpenAPI specs, native MCP servers, the built-in Generic HTTP fallback, and installed adapters. The connector catalog is instance-wide, so imported connector types and installed adapters are visible to other authenticated users in the same Agent Core deployment.

Provider types:

- `builtin` for adapter-backed connector types and other built-in handlers
- `generic_http` for the built-in Generic HTTP fallback and legacy Generic HTTP rows
- `openapi` for imported REST/OpenAPI connector types
- `mcp` for native MCP server registrations

The connector type row stores provider metadata such as `endpoint_url`, `transport_type`, `capabilities_json`, `tool_snapshot_json`, `operations_json`, `backend_type`, and `backend_json` when applicable.

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| `GET` | `/api/connector-types` | Agent/session | List installed connector types |
| `GET` | `/api/connector-types/summary` | Agent/session | Summarize connector types, visible bindings, and action health |
| `POST` | `/api/connector-types/health-check` | Agent/session | Check enabled binding health for a connector type and/or scope |
| `GET` | `/api/connector-types/{connector_type_id}/tools` | Agent/session | List actions for a connector type |
| `PUT` | `/api/connector-types/{connector_type_id}/actions` | Admin | Update the disabled actions list for a connector type |
| `GET` | `/api/connector-types/adapters` | Agent/session | List available system and user adapters |
| `POST` | `/api/connector-types/adapters/{adapter_id}/install` | Admin | Install an adapter into the connector catalog |
| `POST` | `/api/connector-types/adapters/{adapter_id}/update` | Admin | Refresh an installed adapter from the newer available template |
| `DELETE` | `/api/connector-types/adapters/{adapter_id}/install` | Admin | Uninstall an adapter from the connector catalog |
| `POST` | `/api/connector-types/preview` | Admin | Parse an OpenAPI spec and return metadata without persisting |
| `POST` | `/api/connector-types/import` | Admin | Import an OpenAPI spec from URL or pasted JSON/YAML |
| `POST` | `/api/connector-types/create-http` | Admin | Create a Generic HTTP connector type |
| `POST` | `/api/connector-types/import-mcp` | Admin | Register a native MCP server and discover its tools |
| `POST` | `/api/connector-types/{connector_type_id}/refresh` | Admin | Refresh MCP discovery metadata and tool snapshot |
| `GET` | `/api/connector-types/directory` | Agent/session | Browse the public API directory (grouped by provider, variants included) |
| `DELETE` | `/api/connector-types/{connector_type_id}` | Admin | Delete a connector type; adapter-backed connector types follow uninstall behavior |

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

## Connector bindings

Connector bindings link a stored credential to an external service so Agent Core can run actions against that service on behalf of an agent. The credential never leaves Agent Core, the connector executor uses it server-side.

Credential scope controls access to the stored secret. Binding scope controls where the connector is available. A binding currently points to one credential; non-secret connector settings belong in `config_json`.

Use `config_json` for fixed, non-secret defaults that should travel with the binding. Common examples:

- `base_url` or `test_url` for a generic HTTP binding
- `default_params` for imported APIs or adapters that always need the same repo, owner, workspace, tenant, company, or other context
- `auth_header`, `auth_scheme`, `auth_scheme_name`, `auth_location`, or `query_param` when the connector needs a specific non-secret auth layout
- `auth_mode: "none"` to suppress spec-driven auth injection for a trusted local deployment that does not actually require auth

When running a connector action, Agent Core prepares params before validation and execution:

1. Start with the caller's `params` object.
2. Apply any action-level `param_aliases` from the connector metadata, and infer simple camelCase-to-snake_case aliases when the action schema declares the snake_case property.
3. Merge `config_json.default_params` underneath the caller params.
4. Validate the merged params against the action schema.

Explicit caller params always override defaults. This lets a binding provide stable context like `{"default_params":{"company_id":"..."}}` while still allowing a caller to override `company_id` for a specific action. Keep `default_params` non-secret; use credentials for secrets.

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
| `POST` | `/api/connector-bindings/{binding_id}/oauth/start` | Agent/session (write scope) | Begin the browser OAuth authorization flow for an oauth2 adapter binding; returns the provider authorization URL |
| `GET` | `/api/connector-bindings/oauth/callback` | Session | OAuth redirect target; exchanges the authorization code and stores tokens on the binding's credential |

### Running a connector action directly over REST

Plug-in scripts (e.g. a `no_agent` cron) that don't go through MCP call the binding's `run` endpoint themselves:

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $AGENT_CORE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"action": "add_torrent", "params": {"filename": "magnet:?xt=..."}}' \
  http://core.example.com/api/connector-bindings/<binding_id>/run
```

Body shape is `{"action": "...", "params": {...}}`. The canonical path is `/api/connector-bindings/{binding_id}/run`; `/api/connectors/{binding_id}/run` is accepted as an alias so the intuitive path doesn't 404. Note the **MCP `connectors_run` tool dispatches server-side and is not the same as this direct REST path.** Testing only through MCP will not surface a wrong REST URL.

Create:
```json
{
  "connector_type_id": "example-rest-api",
  "name": "Workspace API",
  "scope": "workspace:my-workspace",
  "credential_id": "<credential-entry-id>",
  "config_json": "{\"default_params\":{\"owner\":\"your-org\",\"repo\":\"your-repo\"},\"auth_scheme_name\":\"personalAccessToken\"}",
  "enabled": true
}
```

In that example, the credential provides the secret token, while `config_json` tells Agent Core which repo context and auth scheme to apply when it turns the binding into a real request. For MCP bindings, `config_json` can also carry per-binding timeout and header overrides used during tool execution.

The dashboard's `/connectors` page provides a UI for all of these operations.

---

## Internal broker

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

The broker credential authenticates the request. The `agent_id` must come from trusted runtime configuration, not model-generated text. Agent Core checks that agent's read scopes before returning a raw value.

---

## Session context

`POST /api/session-context` (agent key) returns `{memory_scope, resolved_by, execution_id, context, chars, reductions, memory_ids, activity_ids}`. Body: optional `memory_scope`, `cwd`, `host_session_ref`, `source`, `max_chars` (1,000 to 50,000, default 9,000). Without `memory_scope`, `cwd` is matched against the `workspace_repo_roots` setting, deepest root first. `context` is fitted to `max_chars` by dropping whole items, cheapest first: the record index, then changes, then result detail. An execution is opened (or reused for the same `host_session_ref`) with its cursor at the newest change. Each call is audited as `context_injected`.

`POST /api/session-context/handoff-check` with `{host_session_ref}` returns `{nudge: false}` or `{nudge: true, activity_id, reason}`. It is used by the Claude Code Stop hook, and asks at most once per activity.

## Workspace synchronization

These routes require agent authentication and workspace memory read access.

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/workspace-sync` | Start or continue an execution and return workspace changes after its cursor |
| `POST` | `/api/workspace-sync/ack` | Advance the acknowledged cursor after processing a delivered page |
| `POST` | `/api/workspace-sync/end` | Mark an execution as ended |

The first sync omits `execution_id`. The response returns a new execution ID, pinned records, assigned activities, and changes grouped into `memory_changes` and `activity_changes`. `other_session_changes` lists the IDs of changes whose `source_execution_id` belongs to another execution. The changes themselves appear once, in their resource groups.

Keep the execution ID for one host session. Pass it to later sync calls and supported write operations for source attribution. Acknowledge `next_cursor` only after processing or intentionally skipping the page. An acknowledgement cannot move past the highest cursor that the server delivered to that execution.

If retention removes changes before an execution reads them, the next response sets `cursor_reset` to `true` and `cursor_reset_reason` to `cursor_expired`.

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
| `POST` | `/api/activity/{activity_id}/recovery` | Admin | Recover a stale activity: complete, cancel, resume, or reassign it |

Create:
```json
{
  "task_description": "Refactor memory search tests",
  "memory_scope": "agent:coding-agent",
  "metadata_json": "{}"
}
```

**Valid statuses:** `active`, `stale`, `completed`, `blocked`, `cancelled`, `reassigned` (set by recovery when work moves to another agent)

Only the owning agent or an admin can update, heartbeat, cancel, or reassign an activity. Reading is wider: `GET /api/activity/{id}` and MCP `activity_get` open any activity in a scope the caller reads, the same activities `activity_list` and `activity_search` show it.

For the MCP `activity_update` tool, creating a fresh activity in a readable `workspace:*` scope can add a best-effort `since_last_active` object beside `activity`. It summarizes changes since that agent's prior activity using workspace change-sequence boundaries. A first activity uses `baseline: "retained_tail"`; the response also reports `total_available`, `truncated`, and `oldest_available_at` so clients do not infer completeness beyond retained history. The key is omitted when the feature is disabled, the caller cannot read the scope, digest computation fails, or the supplied `execution_id` is already caught up. REST activity creation is unchanged.

### Handoffs

`PUT /api/activity/{id}` and the MCP `activity_update` tool accept `handoff` with `completed` (string), `next_steps` and `open_questions` (lists of strings) and `needs_human` (string), up to 4,000 characters. Any other field, including `resolution`, is rejected with `INVALID_HANDOFF`. The next session context shows them.

A `needs_human` request waits on a person: it is listed under "Waiting on a person" in session context and under **Waiting on you** on the Activity page until one of these resolves it:

- **A person** calls `POST /api/activity/{id}/handoff/resolve` (the **Mark resolved** button). The route is for people only; an agent key gets `403`. It returns `{activity, resolution}`, and `409 NOT_PENDING` when the activity has no open request.
- **The agent that asked** calls the MCP `activity_handoff_resolve` tool with `activity_id` and a `reason` (required, up to 500 characters). Only the agent the activity is assigned to can call it, and only in a scope it can write; any other agent gets `403`.
- **A request from a later session of the same agent** supersedes it. When `activity_update` or `PUT /api/activity/{id}` records a handoff with a non-empty `needs_human`, every other open request of activities with the same assigned agent, the same user and the same `memory_scope` that was made before this activity started is resolved as superseded. A request a parallel session made after this activity started is left open, as are requests in other scopes, of other users or of other agents. Superseding is per task: only activities with the same `metadata.task_ref` (for example `jira:PROJ-123`) supersede each other, activities without a `task_ref` (ordinary terminal sessions) supersede each other, and a tagged and an untagged activity never do; `open_requests` follows the same rule. Superseded requests leave the queue and session context but stay listed, collapsed, under **Superseded by a later handoff** on the Activity page.

For example, one agent working in `workspace:app` without a `task_ref`:

1. Monday's session closes with `needs_human: "Approve the schema change"`. It waits on a person.
2. Tuesday's session closes with `needs_human: "Pick a release date"`. Monday's request was made before Tuesday's session started, so it is superseded, and Tuesday's response lists it in `superseded_requests`. If the schema change still needs approval, the agent restates it, and both asks wait.
3. A second session that started on Monday afternoon, while Monday's was still open, would not have superseded Monday's request: superseding only reaches requests made before the current session started.
4. With `task_ref` set, only requests with the same `task_ref` are superseded. An orchestrator that runs several jobs as one agent keeps one open request per job.

A request's time is `handoff_requested_at`, set whenever a `needs_human` request is recorded or its text changes (before a handoff exists, the column is the Stop hook's "already asked" stamp). For rows recorded before this was stamped, the activity's `ended_at` stands in, then `updated_at`.

`needs_human` is one string; separate asks are paragraphs separated by a blank line. A handoff sent for a task that is already `completed` or `blocked` amends its handoff rather than replacing it: new asks are appended unless the same paragraph is already there, `next_steps` and `open_questions` gain the items they lack, and `completed` is replaced when given. No ask is removed this way; asks leave only through a resolution. If the request was already resolved, the new asks start a fresh request. An amendment that would exceed 4,000 characters is rejected with `INVALID_HANDOFF` and nothing changes. A handoff sent for an open task (`active` or `stale`) replaces it, as before.

Each resolution is stored in the activity's `handoff_json` under `resolution`, beside `handoff_resolved_at`:

```json
{
  "kind": "person | agent | superseded",
  "at": "2026-10-27T12:00:00+00:00",
  "resolved_by": "user id (person) or agent id (agent, superseded)",
  "reason": "agent resolutions only",
  "superseded_by": "the activity whose handoff replaced it (superseded only)"
}
```

A resolved request stays resolved when a later handoff on the same activity repeats the same `needs_human` text, and keeps its `resolution`; a different `needs_human` reopens it. Resolutions are audited as `handoff_resolved` (details include `kind`, `actor_type` and, for agents, `reason`) and `handoff_superseded` (details include `superseded_by` and the request text), and announced as `activity_handoff_resolved` events whose `handoff_resolution` is `person`, `agent` or `superseded`.

The MCP `activity_update` response and the `PUT /api/activity/{id}` response can carry these fields beside `activity`, and both write the same audit events:

| Field | When |
| --- | --- |
| `superseded_requests` | The handoff's `needs_human` superseded requests from earlier sessions. One entry per request: `activity_id`, `summary` (the task description, up to 200 characters), `status`, `needs_human`, `open_questions` (when the old handoff had any) and `requested_at`. A `HANDOFF_REQUESTS_SUPERSEDED` warning asks the agent to restate any that still apply. |
| `open_requests` | The call closed the activity (`completed` or `blocked`) without a `needs_human` request while the assigned agent has other open requests in the scope, made before this activity started. Same entry shape. An `OPEN_HANDOFF_REQUESTS` warning reminds the agent to resolve them with `activity_handoff_resolve` or restate them. Nothing is resolved. |
| `warnings` | `HANDOFF_MISSING` (closed without a handoff), `HANDOFF_REQUESTS_SUPERSEDED`, `OPEN_HANDOFF_REQUESTS`. |
| `handoff_amended` | MCP only. `true` when the call carried only a `handoff`, the agent had no open activity in the scope, and the handoff was added to the handoff of the agent's latest activity in the scope because it is `completed` or `blocked`. This is how an agent restates a superseded request after closing: it sends only the restated ask. Without such an activity the call still needs `task_description`. REST has no lookup of this kind because it names the activity; a `PUT` with a `handoff` (alone or with other fields) on a `completed` or `blocked` activity is merged the same way. |

Recording or resolving a handoff in a `workspace:*` scope adds a `handoff_recorded` or `handoff_resolved` change to that workspace's change feed (`workspace_sync`), with the task, status, `needs_human` and `task_ref` in its summary. Activity webhook payloads carry the `handoff`, `handoff_resolved_at` and `task_ref`, and resolving sends `activity_handoff_resolved`.

`GET /api/activity/attention` lists unresolved `needs_human` requests, newest first, in the scopes the caller reads: `id`, `agent_id`, `user_id`, `memory_scope`, `status`, `task_description`, `needs_human`, `task_ref`, `updated_at`. It is for a coordinator, such as an attention reader or a chief-of-staff agent, that tells a person what is waiting on them; it needs the `coordination` capability, not admin. Repeat `scope` to narrow it (each must be readable, or the call returns `403`), pass `without_task_ref=true` to skip requests that point at another system's task, and `limit` (default 50, at most 200). The MCP equivalent is `attention_list`. Listing never resolves anything; that stays with a person.

### Compact activity lists

Over MCP, `activity_list` and `activity_search` return compact rows unless `view=full` is passed: `id`, `agent_id`, `memory_scope`, `status` and timestamps, previews of `task_description`, `task_note` and `task_result` (cut at 200, 160 and 240 characters and marked with `…`), `task_ref`, `has_handoff`, and `needs_human` while a request to a person is unresolved. They keep as many whole rows as fit under the tool-result offload threshold; when `response_limited` is true, raise `offset` by `count` for the next page. `activity_get` returns one whole record. REST `GET /api/activity` and `/api/activity/search` are unchanged and return full rows.

### Linking an activity to another system's task

When work belongs to a task that lives in another system, record it as `metadata.task_ref = "<system>:<id>"`, for example `jira:PROJ-123` or `github:owner/repo#42`. MCP `activity_update` and REST `POST /api/activity` and `PUT /api/activity/{id}` take it as `task_ref` (REST also accepts it inside `metadata_json`); it must be a lowercase system name, a colon, and an id without spaces. Core stores it and does not interpret the id. A reader that already lists that system's queue uses it to skip the handoff (`without_task_ref`), so one item is not shown twice. Work started from another system's task should always set it; a terminal session started by a person leaves it empty.

### Activity pickup

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
| `memory_confirm` | Mark a record verified against the world as of now. Requires `evidence` naming what was checked; reading a record is not checking it |
| `memory_feedback` | Say whether a recalled record actually helped; feeds observed usefulness into ranking |
| `memory_verify` | Check anchored facts against the repo path, host or connector binding they describe. Records evidence on passes; queues review for anchors that have vanished |
| `memory_reanchor` | Repoint a record at what actually describes it, when the file moved or the anchor was wrong |
| `memory_pinned` | The standing context for the caller's scopes; loaded at session start rather than searched for |
| `memory_pin` | Request that a record become (or stop being) standing context; queues the request for operator review |
| `credential_get` | Get an `AC_SECRET_*` reference for a credential entry |
| `credential_list` | List credential metadata and references in authorized scopes |
| `credential_request` | Ask a person for a missing secret; returns the reference once they fill it on the Credentials page |
| `session_context` | One budgeted session-start block (rules, waiting and unfinished work, recent results with next steps, changes, key-record index) plus the session's `execution_id` |
| `workspace_sync` | Start or continue an execution and return current context plus workspace changes after its cursor |
| `workspace_sync_ack` | Advance an execution's acknowledged cursor after processing a delivered page |
| `activity_update` | Create or update an activity record in the supplied `memory_scope`, including progress notes, completion result and a structured `handoff` (`completed`, `next_steps`, `open_questions`, `needs_human`); it never moves an activity across scopes. A `needs_human` request supersedes the agent's open requests in the scope from earlier sessions and lists them in `superseded_requests` (see [Handoffs](#handoffs)) |
| `activity_handoff_resolve` | Resolve one of the caller's own `needs_human` requests once it has been dealt with. `activity_id` and `reason` (up to 500 characters) are required; only the agent the activity is assigned to may call it |
| `activity_get` | Get an activity record in a readable scope, with the memory records written during it |
| `activity_list` | List activities visible to the current caller. Compact by default (see below); `view=full` returns every field |
| `attention_list` | Unresolved `needs_human` handoffs across readable scopes, newest first, with each one's `task_ref`; read-only, for coordinators |
| `activity_pickup` | Claim the next active work item assigned to this agent in authorized scopes |
| `activity_search` | Full-text search the activity trail: what agents worked on, per task, with results. Use for "what did we do on X"; use `memory_search` for durable facts and decisions. Compact by default, like `activity_list` |
| `effective_authority` | Report the caller's current effective authority: identity, read and write scopes, workspaces, and capabilities |
| `connectors_list` | List installed connector types as lean summaries (id, name, auth/backend type, action_count; no full specs). Supports `limit`/`offset`; use `connectors_actions_list` for a type's actions |
| `connectors_bindings_list` | List connector bindings in authorized scopes |
| `connectors_bindings_test` | Test a binding using its stored credential |
| `connectors_actions_list` | List actions supported by a connector type |
| `connectors_summary` | Summarize visible connector capability, binding, credential-presence, action, and health state |
| `connectors_run` | Run one connector action server-side using a binding |
| `connectors_resolve` | Deterministically resolve a connector type (plus optional alias, scope, and action) to a single authorized binding |
| `result_fetch` | Retrieve a slice of a previously offloaded large tool result by handle |

### Large tool results (offloading)

When any MCP tool's output exceeds `AGENT_CORE_TOOL_RESULT_SPILL_THRESHOLD` characters (default 8000), the full payload is stored server-side (encrypted at rest) and the tool returns a compact envelope instead:

```json
{
  "offloaded": true,
  "handle": "abc123...",
  "total_chars": 32732,
  "expires_at": "2026-07-06T21:40:30Z",
  "summary": {"preview": "...", "structure": "object", "top_level_keys": ["..."]},
  "retrieve_with": {"tool": "result_fetch", "params": {"handle": "abc123...", "offset": 0, "limit": 4000}}
}
```

Call `result_fetch` with the handle to read the full payload in chunks, following `next_offset` until `has_more` is false. Each fetch is audited as `tool_result_fetched` against the handle, so `scripts/value_report.py` can report how many offloaded results agents actually read. Handles are scoped to the agent that made the original call and expire after `AGENT_CORE_TOOL_RESULT_SPILL_TTL_HOURS` (default 24h).

### Image data-URL artifacts (connector output)

Connector responses whose body contains a `data:image/<subtype>;base64,...` payload (for example, image generation through OpenRouter, which returns 1–2 MB inline data URLs) are detected and exported to disk before the response reaches the agent. The detected bytes are decoded and written to `<DATA_PATH>/artifacts/connector/<handle>.<ext>`, and the data URL in the response is replaced with a small reference object:

```json
{
  "exported": true,
  "artifact": {
    "path": "/var/lib/agent-core/data/artifacts/connector/AbC...Ef.png",
    "mime_type": "image/png",
    "size_bytes": 1234567,
    "handle": "AbC...Ef",
    "format": "data_url"
  }
}
```

The connector result dict also carries `artifacts_exported` (the count of images exported) so callers can branch on it. Supported subtypes: `png`, `jpeg`, `jpg`, `webp`, `gif`, `svg+xml`, `bmp`, `tiff`. Non-image data URLs and malformed base64 payloads are left in place and fall through to the existing spill path. Each export writes a `connector_artifact_exported` audit row with the binding id and the count exported.

---

## Dashboard API

Session authentication required. Global overview and audit routes require admin.

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/dashboard/search` | Search memory, activities, and connector visibility |
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
| `GET` | `/api/dashboard/vector-reembed` | Re-embed progress and how many records have a vector for the current model (admin only) |
| `POST` | `/api/dashboard/vector-reembed` | Start embedding every record without a vector for the current model, replacing its vector from any earlier model; optional body `{"pause_ms": N}` (0-30000) pauses after each record; `409` if a run is already going (admin only) |
| `POST` | `/api/dashboard/vector-models` | List the models an embedding endpoint reports; body `{"provider": "ollama"|"openai", "url", "auth_type"?, "api_key"?}`; the saved key is used only for the saved URL (admin only) |
| `POST` | `/api/dashboard/vector-reembed/estimate` | Time a synthetic sample as long as an average record (no memory content is sent) on a model and return seconds per record, estimated re-embed time, the wait each memory write would add, and a warning level; optional body `{"model", "url"}` measures a model before it is saved (admin only) |
| `POST` | `/api/dashboard/vector-reembed/cancel` | Stop the running re-embed after its current batch (admin only) |
| `GET` | `/api/dashboard/ollama-models?url=` | List the models an Ollama-compatible endpoint has installed; used by the embedding settings card (admin only) |
| `POST` | `/api/dashboard/broker/rotate` | Rotate broker credential (admin only) |

---

## Events

Session authentication required (cookie-based only; agent tokens are not accepted on this endpoint).

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/events` | Open a live SSE stream for dashboard events |

Non-admin users receive activity events for their own activities, and connector events from currently readable scopes. Admins can receive all of these events. Visibility is checked before each delivery; deleting or expiring the session stops further event delivery.

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

## Integrations setup

Session authentication required. These routes power the setup page that generates instructions, environment files, and verification prompts for a chosen agent/workspace.

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/integrations/preview` | Generate setup instructions and recommended scopes |
| `POST` | `/api/integrations/generate-connection` | Generate connection files for the selected agent/workspace |
| `POST` | `/api/integrations/apply-access` | Apply the selected access scopes to an agent |
| `POST` | `/api/integrations/apply-recommended-access` | Apply the recommended access scopes to an agent |

---

## Backup and maintenance

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| `POST` | `/api/backup/export` | Admin | Download encrypted full backup archive and one-time backup key header |
| `POST` | `/api/backup/restore` | Admin | Restore an encrypted backup ZIP, replacing all current data (`mode=replace_all`) |
| `GET` | `/api/backup/export/memory?fmt=jsonl` | Session | Export authorized memory as JSONL or CSV |
| `GET` | `/api/backup/export/credentials` | Session | Export credential metadata only (no raw values) |
| `GET` | `/api/backup/export/audit?fmt=csv` | Admin | Export audit log as CSV |
| `GET` | `/api/backup/startup-checks` | Admin | Run operational health checks |
| `POST` | `/api/backup/maintenance` | Admin | Run maintenance now (mark stale activities, prune expired scratchpad memories, sweep `expires_at` TTL'd records, purge retracted/superseded records past retention) |
| `GET` | `/api/backup/maintenance/status` | Admin | Last maintenance run time, trigger (scheduled/manual), result counts, and the automatic schedule |

Maintenance also runs automatically on an interval (default every 60 minutes; see `AGENT_CORE_MAINTENANCE_INTERVAL_MINUTES` in [configuration](configuration.md)), so calling it manually is optional.

Restore uses multipart form data:

```bash
curl -X POST http://localhost:3500/api/backup/restore \
  -H "Authorization: Bearer <admin-session>" \
  -F "backup=@backup.zip.enc" \
  -F "backup_key=<one-time-backup-key>" \
  -F "mode=replace_all"
```

`replace_all` is the only mode, and the default. Any other value is rejected with `400 INVALID_MODE`.

---

## Webhooks

The Webhooks section covers two independent features:

- **Inbound receiver**, external systems push work commands into Agent Core
- **Outbound notifications**, Agent Core pushes event notifications to external endpoints

---

## Inbound webhooks

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
    "note": "Store this key; it will not be shown again."
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

## Outbound webhooks

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

## Rate limits

Rate-limited endpoints include these response headers:

| Header | Value |
| --- | --- |
| `X-RateLimit-Limit` | The limit for this operation |
| `X-RateLimit-Remaining` | Requests remaining in the current window |
| `X-RateLimit-Reset` | Unix timestamp when the window resets |
