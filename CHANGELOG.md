# Changelog

Notable changes to Agent Core are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Re-embedding after an embedding model change. Stored vectors are tagged with the model that made them and search only uses the current model's, so switching models used to leave every existing record findable by text only. **Settings → Vector Search** now shows how many records have a vector for the current model and has a **Re-embed missing records** button that embeds the rest in the background, active records first, with progress and a Stop button. Each record keeps one vector, so re-embedding replaces the previous model's vectors. An optional pause after each record lets a run share a slow backend. A run skips records already done, so a stopped or interrupted run can simply be started again, and only one runs at a time across workers. Also available as `GET`/`POST /api/dashboard/vector-reembed` and `POST /api/dashboard/vector-reembed/cancel` (admin only), and from a shell in the container as `python3 -m app.services.reembed_service`.

### Changed

- MCP `activity_list` and `activity_search` return compact rows by default (previews of the task, note and result, `task_ref`, and any unresolved request to a person) and keep as many as fit inline, with `response_limited` and offset paging; `view=full` returns every field. Large lists were offloaded to a handle that agents rarely fetched. REST responses are unchanged.
- Session context lists at most 10 key records, with shorter previews, down from up to 40. Listed records were rarely opened; `memory_search` finds the rest.
- `GET /api/connector-bindings/{id}/executions` returns the real `total` and caps `limit` at 200.
- The example `docker-compose.example.yml` uses a named volume and adds a health check; a bind-mounted data directory the container cannot `chmod` no longer stops startup.

### Security

- The Generic HTTP connector only sends the binding's credential to the configured `base_url` host; a caller-supplied `url` on another host is refused. Adapter path parameters are percent-encoded as single segments, and only a manifest path that is literally absolute can replace `base_url`.
- Upstream response bodies are read with a 10 MB cap, so a runaway upstream cannot exhaust the process; a body over the cap is reported as `RESPONSE_TOO_LARGE` rather than parsed cut off.
- TOTP codes are single-use: a code that already signed in is refused for the rest of its window. Migration 15 adds `otp_secrets.last_used_step`.
- MCP `memory_verify` without a `scope` verifies only the caller's writable scopes instead of the whole installation.
- `credential_request` needs read access to the scope as well as write access, so a write-only key cannot learn which entries exist.
- The audit page escapes every cell, including a forwarded client address, and the audit CSV export neutralises spreadsheet formulas.

### Fixed

- HTTP-engine adapters return a non-2xx upstream reply (a 404, a session challenge) as `success: false` with its status and body again. Since the response-size cap, the error-response wrapper crashed with a `TypeError` instead.
- MCP `activity_update` with an unknown `status` returns `400 INVALID_STATUS` instead of a 500; `?page=` values that are not numbers open page one instead of a 500; a malformed credential scope is a 400.
- Amending a handoff with a `needs_human` after a person resolved the previous request opens a new request even when the text is the same, and a `task_ref`-only update is reported as `activity_updated`, not a heartbeat. `superseded_requests`/`open_requests` report the time the request was made.
- CLI adapters run their tool with the server's `PATH` and `HOME` (and nothing else from the server environment), so a tool installed outside `/usr/bin` is found; the GitHub CLI adapter's `create_issue` no longer passes a `--json` flag `gh issue create` does not have and returns the issue URL and number.
- A second consumer write conflict in the same scope no longer fails on a unique name.
- Purging a workspace also removes its change feed, executions and proposals, so a workspace re-created with the same id does not inherit them.
- A webhook test delivery is sent once, not also by the background worker; a vector API key that no longer decrypts is reported as missing instead of being sent as-is; hybrid search is reported only when vectors were actually scored; `memory_move` no longer blocks the event loop on the embedding call.
- The broker runner treats a read timeout like any other resolution failure, and the Claude Code hook accepts a lower-case `bearer` header.
- Removed install-specific figures and ticket ids from comments, docstrings and test fixtures, and pointers to planning documents that are not in the repository.

## [1.1.0] - 2026-10-28

### Added

- Credential requests: the MCP `credential_request(scope, name, purpose)` tool lets an agent ask for a secret it needs without the value passing through it. A person fills or declines the request under **Requested by agents** on the Credentials page (`GET /api/credentials/requests`, `POST /api/credentials/requests/{id}/fulfill` and `/cancel`, human sessions only), and the agent receives the AC_SECRET reference by calling the tool again. Requests are idempotent per scope and name, a declined request is not reopened by asking again, and each agent can have at most 20 open.
- Memory writes now report advisory warnings for likely active duplicates and matches to previously retracted records. Evidence-based confirmation is available through MCP, REST, and the dashboard.
- Memory consolidation can run on the configured maintenance interval, with an unconfirmed-inference rule, per-scope and installation-wide pending caps, a per-run budget, and fair rotation across rule/scope groups.
- Fresh MCP activity creation can return a bounded `since_last_active` workspace digest when its execution is not caught up. The Activity dashboard now shows execution-linked workspace activity rates for the installation lifetime and the last 30 days.
- Session context: `POST /api/session-context` and the MCP `session_context` tool return one budgeted block for session start: pinned rules, unfinished or handed-off work, recent results, changes since the agent's last session (one line per resource), and an index of key records. It replaces the startup `workspace_sync` plus searches, and opens the session's execution with its cursor at the newest change. The workspace is matched from the client's working directory against the configured workspace repo roots. Each injection is audited as `context_injected` with the record and activity ids it carried.
- Claude Code hook (`integrations/claude-code/agent_core_hook.py`, served at `/integrations/hooks/claude-code/agent_core_hook.py`). On SessionStart it injects the session context. On Stop, when the session's activity is open without a handoff, it blocks once and asks the agent to record one; it never asks twice for a task. It reads the URL and key from the existing MCP configuration and never breaks a session. The Integrations page has a **Claude Code Hook** output with install steps.
- Structured handoffs: `activity_update` (MCP) and `PUT /api/activity/{id}` accept `handoff` with `completed`, `next_steps`, `open_questions` and `needs_human`. The next session context shows next steps and open questions on recent work, and lists unresolved `needs_human` requests under "Waiting on a person" until they are resolved: by a person (`POST /api/activity/{id}/handoff/resolve`), by the agent that asked, or by a request that agent makes in a later session (see below). Closing an activity without a handoff returns a `HANDOFF_MISSING` warning. Migration 12 adds the handoff columns.
- Agents can settle their own handoff requests. A `needs_human` request recorded through `activity_update` or `PUT /api/activity/{id}` supersedes the agent's open requests in the same scope and for the same user that were made before the current activity started, so parallel sessions leave each other's requests alone. Superseding is per `metadata.task_ref`: tasks with different task_refs (such as separate jobs an orchestrator runs as one agent) never supersede each other, while terminal sessions without one supersede each other. The response lists them in `superseded_requests` (activity id, summary, `needs_human`, `open_questions`) so the agent can restate any that still apply, and each is audited as `handoff_superseded`. The new MCP tool `activity_handoff_resolve(activity_id, reason)` lets the agent an activity is assigned to resolve its request once dealt with. Closing a task without a request while earlier ones are open returns them in `open_requests` with an `OPEN_HANDOFF_REQUESTS` reminder and resolves nothing. A handoff for a completed or blocked task is added to its handoff rather than replacing it: new asks are appended as paragraphs of `needs_human`, list items are added, and no ask is removed except by a resolution. Over MCP, with no open task, a handoff alone goes to the agent's latest completed or blocked task in the scope, so a superseded request can be restated in the same session. `handoff_requested_at` now records when a request was made. Every resolution is stored in `handoff_json` as `resolution` (`kind` person, agent or superseded, `at`, `resolved_by`, `reason`, `superseded_by`); no migration. The Activity page keeps superseded requests visible in a collapsed group, and session context marks the calling agent's own open requests.
- Attention list for coordinators: the MCP `attention_list` tool and `GET /api/activity/attention` list unresolved `needs_human` requests across the scopes the caller reads, for an agent or dashboard that reports what is waiting on a person. It needs the `coordination` capability, not admin, and is read-only.
- `task_ref`: `activity_update`, `POST /api/activity` and `PUT /api/activity/{id}` accept `task_ref` (`<system>:<id>`, for example `jira:PROJ-123`), stored as `metadata.task_ref`, to link an activity to a task in another system. `attention_list` can skip requests that carry one (`without_task_ref`) when that system's own queue already lists them.
- Handoff events: activity webhook payloads include `handoff`, `handoff_resolved_at` and `task_ref`; every resolution sends `activity_handoff_resolved` with `handoff_resolution` (`person`, `agent` or `superseded`); and recording or resolving a handoff adds `handoff_recorded` or `handoff_resolved` to the workspace change feed.
- Workspace repository folder: admins link a workspace to a repository folder on the Edit Workspace dialog. Session context and the anchor verifier use it to pick the workspace from a session's working directory.
- Credential consumers: services and containers get their own resolve-only identity (`credentials_resolve`) and exact-entry grants, with optional versioned write-back for refreshed OAuth state. See [docs/credential-consumers.md](docs/credential-consumers.md). Nightly maintenance rolls successful resolution audits older than 30 days into daily counts.
- `memory_get(ids=[...])` fetches records by id, for the session-context index. Fetches are audited as `memory_fetched` and count as recalls.
- `result_fetch` reads are audited as `tool_result_fetched` against the offload handle. `scripts/value_report.py` reports whether stored data reaches agents: read-through of offloaded results, empty searches per agent, never-recalled memory per scope, and sync-stream composition.

### Fixed

- `PUT /api/activity/{id}` with only a `handoff` no longer fails with `UPDATE_FAILED` (500).
- MCP `memory_write` and `memory_search` now apply the same per-agent rate limits and concurrent-search limit as REST, as `docs/security.md` describes. Both transports share one budget.
- `activity_get` (MCP and REST) opens any activity in a scope the caller can read, matching `activity_list` and `activity_search`.
- A credential request closes as soon as its credential exists in the requested scope, however it was added; an agent asking again for a credential that was filled and then moved elsewhere is told where it went instead of opening a new request.
- The Hermes memory provider expands `${env:NAME}` and `${NAME}` in the Agent Core MCP block of its config, and logs one warning (naming the variable, never its value) and stays unavailable when one is unset, instead of sending the placeholder as a token. Its system prompt now asks the agent to record work it hands to others; set `AGENT_CORE_HANDOFF_PROMPT=0` to leave that out.
- Removed the git adapter installer (`install_from_git`): no route called it, while the docs described it as an install path. Docs now describe the two real paths.
- Corrected `SECURITY.md` environment variable names (`AGENT_CORE_` prefix) and the README's outbound-traffic statement, which omitted maintenance binding checks and webhooks.
- Removed unused functions: `get_accessible_workspace_ids`, `vector_service.get_embedding`, `get_current_user`, `get_current_user_optional`, `_memory_audit_details` and `SpecResponse`.
- Paperclip `list_issues` now accepts and forwards `limit`. If an upstream Paperclip server ignores the parameter, Agent Core trims the list locally and marks the result with `limit_applied_locally`.
- Updated built-in Paperclip adapter version to `1.4.2`, so existing installations show **Update** in the adapter library after deployment.
- Restored table-cell layout for dashboard action columns, which keeps the credentials table header aligned with its action buttons.
- Hermes memory prefetch now uses the Agent Core key's default recall scopes and caps injected context at 12,000 characters by default. Set `AGENT_CORE_MAX_CONTEXT_CHARS` to adjust the cap from 512 through 50,000 characters.

### Changed

- The verification pass records a present anchor as `anchor_checked_at` and no longer sets `last_confirmed_at`, which only evidence-based confirmations set. Migration 10 moves earlier verifier stamps to `anchor_checked_at` and restores each record's last real confirmation from the audit log. Search results include `anchor_checked_at`.
- The connector execution log stores the response size instead of the response body. Migration 9 replaces bodies already stored.
- `connectors_actions_list` returns a compact menu by default (action, one-line description, side effect, required parameters, capability policy); pass `action` for one action's full input schema, or `view=full`. `connectors_summary` returns one line of state per binding by default; pass `view=full` (or a `connector_type_id`) for credential and health detail. Both were offloaded on most calls.
- `workspace_sync` lists only change IDs in `other_session_changes`; the changes themselves appear once, in their resource groups.
- Maintenance no longer hard-deletes superseded memory records. They are the history behind `as_of` searches and supersession chains. Retracted records are still purged after their grace period.
- Generated agent instructions start sessions from the injected session context (or one `session_context` call) instead of `workspace_sync`.
- MCP `memory_search` without `max_response_chars` now trims to whole records under the tool-result offload threshold instead of being offloaded to a handle. Recall counts now include only records the agent received.
- Unattended verifier re-confirmations no longer emit `memory_confirmed` workspace changes (migration 8). They were about a third of all sync events. Confirmations by an agent or a person still sync.
- Generated assistant onboarding is now a concise setup guide. It includes the Hermes MCP configuration location and directs agents to repository `AGENTS.md` or `CLAUDE.md` files for the complete operating contract.
- Memory provenance fields are now authority-controlled: agents cannot claim human-authored source kinds, and `last_confirmed_at` can only be set by confirmation with evidence or verification.

### Removed

Upgrading: each removal below has a migration that drops its tables or settings, so take a backup first. An agent or script that called a removed route or MCP tool gets `404` or an unknown-tool error. Instead of delegated grants, give the agent the scopes it needs directly or a shared workspace; instead of briefings, record a structured handoff on the activity. The `held` memory status, which nothing set, is no longer accepted.

- Usefulness review and the review model: the `POST /api/memory/proposals/review-usefulness` route, the `low_value` proposal rule, the Review Model settings card with its `/api/dashboard/review-model` routes, and the `review_model_*` and `usefulness_review_enabled` settings. It had little use, and the usefulness rule duplicated what reviewers decide on confirm proposals.
- Memory import and merge-mode restore: `POST /api/memory/import`, the dashboard's Import Notes dialog, and `mode=merge` on `POST /api/backup/restore`, which now accepts only `replace_all` and rejects any other mode with `INVALID_MODE`. Both were rarely used, and merge restore could combine two installations' records in ways that were hard to undo.
- Delegated grants and delegation requests: the `/api/delegations` and `/api/delegation-requests` routes, the Delegation dashboard page, the `X-Agent-Core-Grant` header, the agent `can_delegate` setting, the `delegation_request_*` webhook and live events, and the MCP tools `delegation_request`, `delegations_list`, `delegation_requests_list`, `delegation_request_approve`, `delegation_request_deny` and `delegation_revoke`. Migration 11 drops the delegation tables. `effective_authority` remains and reports the caller's own scopes and capabilities. Delegation added a second authority path to every scope check for a rarely used feature; a shared workspace grants the same access more simply.
- Handoff briefings, replaced by structured handoffs on activities: the `/api/briefings` routes (including `/api/briefings/handoff/prd`), the MCP tools `get_briefing` and `briefing_list`, the `create_handoff_briefing` recovery action and the briefing generated on reassignment, the dashboard's Briefing button and search section, the `briefing_changes` group in `workspace_sync`, and the `briefing_generated` and `handoff_created` audit actions. Briefings were rarely generated, but their full bodies were stored in `agent_activity.metadata_json` as rows posing as activities, so listing them returned very large results that were usually offloaded rather than read. Migration 13 recreates the workspace triggers without their briefing branches and deletes the briefing rows and their workspace changes.

## [1.0.0] - 2026-08-16

First stable release. Agent Core is a local capability layer for AI agents: shared memory, credentials, connectors, scoped access, activity tracking, and delegated authority in one SQLite-backed service.

### Memory

- Durable records in four classes (fact, decision, preference, scratchpad) with per-agent scoped access
- Facts carry subject anchors and are re-verified on a schedule; decisions are left alone by design
- Two independent timelines per record (transaction time and valid time), with `as_of` search for point-in-time questions
- Full-text search always; hybrid semantic search when an embedding backend such as Ollama is configured
- Ranking driven by observed recalls, caller feedback, and confirmation freshness rather than author self-assessment
- Pinned standing context, loaded at session start and capped per scope; agents request pins, operators grant them
- Clean-up rules that propose and never act; retraction is reversible

### Activity and handoffs

- Self-reported task trail with heartbeats, staleness detection, and a searchable history of what agents worked on
- Work assignment from the dashboard with explicit agent pickup, and generated briefings for handoffs
- Every memory write cites the activity that was open at the time, so a task links to what it concluded
- Per-execution `workspace_sync` cursors deliver pinned context, assignments, briefings, and workspace changes to each active session
- Durable change acknowledgements let two sessions with the same agent identity track their progress independently

### Credentials and connectors

- Fernet-encrypted credential store with key rotation and a keyring for old entries
- Agents receive `AC_SECRET_*` references, never raw values; a local broker resolves them at runtime
- Connector catalog built from OpenAPI imports, native MCP servers, shareable adapters, or generic HTTP
- Scoped bindings with connector-controlled actions, requirement gating, and deterministic resolution

### Delegated authorization

- Short-lived, narrowed grants: subset-only issuance, one-hour TTL cap, one-time REST-only claim, and replacement (never union) of the recipient's permanent authority
- Request and approval workflow with dashboard review; approval can keep or narrow permissions, never add them
- Lifecycle events on the live dashboard stream and outbound webhooks
- End-to-end attribution in the audit log; backups force live grants to revoked

### Operations

- Web dashboard with live SSE updates, audit log, and generated per-tool integration files
- Outbound signed webhook notifications and an inbound webhook receiver for external automation
- Backup and restore bundling the database with its key material; hourly maintenance sweep
- One REST API and 37 MCP tools over a shared service layer, in a single Docker container with all data on local disk
