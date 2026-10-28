# Changelog

Notable changes to Agent Core are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Credential requests: the MCP `credential_request(scope, name, purpose)` tool lets an agent ask for a secret it needs without the value passing through it. A person fills or declines the request under **Requested by agents** on the Credentials page (`GET /api/credentials/requests`, `POST /api/credentials/requests/{id}/fulfill` and `/cancel`, human sessions only), and the agent receives the AC_SECRET reference by calling the tool again. Requests are idempotent per scope and name, a declined request is not reopened by asking again, and each agent can have at most 20 open.
- Memory writes now report advisory warnings for likely active duplicates and matches to previously retracted records. Evidence-based confirmation is available through MCP, REST, and the dashboard.
- Memory consolidation can run on the configured maintenance interval, with an unconfirmed-inference rule, per-scope and installation-wide pending caps, a per-run budget, and fair rotation across rule/scope groups.
- Fresh MCP activity creation can return a bounded `since_last_active` workspace digest when its execution is not caught up. The Activity dashboard now shows execution-linked workspace activity rates for the installation lifetime and the last 30 days.
- Session context: `POST /api/session-context` and the MCP `session_context` tool return one budgeted block for session start: pinned rules, unfinished or handed-off work, recent results, changes since the agent's last session (one line per resource), and an index of key records. It replaces the startup `workspace_sync` plus searches, and opens the session's execution with its cursor at the newest change. The workspace is matched from the client's working directory against the configured workspace repo roots. Each injection is audited as `context_injected` with the record and activity ids it carried.
- Claude Code hook (`integrations/claude-code/agent_core_hook.py`, served at `/integrations/hooks/claude-code/agent_core_hook.py`). On SessionStart it injects the session context. On Stop, when the session's activity is open without a handoff, it blocks once and asks the agent to record one; it never asks twice for a task. It reads the URL and key from the existing MCP configuration and never breaks a session. The Integrations page has a **Claude Code Hook** output with install steps.
- Structured handoffs: `activity_update` (MCP) and `PUT /api/activity/{id}` accept `handoff` with `completed`, `next_steps`, `open_questions` and `needs_human`. The next session context shows next steps and open questions on recent work, and lists unresolved `needs_human` requests under "Waiting on a person" until they are resolved: by a person (`POST /api/activity/{id}/handoff/resolve`), by the agent that asked, or by a request that agent makes in a later session (see below). Closing an activity without a handoff returns a `HANDOFF_MISSING` warning. Migration 12 adds the handoff columns.
- Agents can settle their own handoff requests. A `needs_human` request recorded through `activity_update` or `PUT /api/activity/{id}` supersedes the agent's open requests in the same scope and for the same user that were made before the current activity started, so parallel sessions leave each other's requests alone. Superseding is per `metadata.task_ref`: tasks with different task_refs (such as Foundry objectives) never supersede each other, while terminal sessions without one supersede each other. The response lists them in `superseded_requests` (activity id, summary, `needs_human`, `open_questions`) so the agent can restate any that still apply, and each is audited as `handoff_superseded`. The new MCP tool `activity_handoff_resolve(activity_id, reason)` lets the agent an activity is assigned to resolve its request once dealt with. Closing a task without a request while earlier ones are open returns them in `open_requests` with an `OPEN_HANDOFF_REQUESTS` reminder and resolves nothing. A handoff for a completed or blocked task is added to its handoff rather than replacing it: new asks are appended as paragraphs of `needs_human`, list items are added, and no ask is removed except by a resolution. Over MCP, with no open task, a handoff alone goes to the agent's latest completed or blocked task in the scope, so a superseded request can be restated in the same session. `handoff_requested_at` now records when a request was made. Every resolution is stored in `handoff_json` as `resolution` (`kind` person, agent or superseded, `at`, `resolved_by`, `reason`, `superseded_by`); no migration. The Activity page keeps superseded requests visible in a collapsed group, and session context marks the calling agent's own open requests.
- `memory_get(ids=[...])` fetches records by id, for the session-context index. Fetches are audited as `memory_fetched` and count as recalls.
- `result_fetch` reads are audited as `tool_result_fetched` against the offload handle. `scripts/value_report.py` reports whether stored data reaches agents: read-through of offloaded results, empty searches per agent, never-recalled memory per scope, and sync-stream composition.

### Fixed

- `PUT /api/activity/{id}` with only a `handoff` no longer fails with `UPDATE_FAILED` (500).
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

- Usefulness review and the review model: the `POST /api/memory/proposals/review-usefulness` route, the `low_value` proposal rule, the Review Model settings card with its `/api/dashboard/review-model` routes, and the `review_model_*` and `usefulness_review_enabled` settings. The audit trail showed no use.
- Memory import and merge-mode restore: `POST /api/memory/import`, the dashboard's Import Notes dialog, and `mode=merge` on `POST /api/backup/restore`, which now accepts only `replace_all` and rejects any other mode with `INVALID_MODE`. Neither had recorded use.
- Delegated grants and delegation requests: the `/api/delegations` and `/api/delegation-requests` routes, the Delegation dashboard page, the `X-Agent-Core-Grant` header, the agent `can_delegate` setting, the `delegation_request_*` webhook and live events, and the MCP tools `delegation_request`, `delegations_list`, `delegation_requests_list`, `delegation_request_approve`, `delegation_request_deny` and `delegation_revoke`. Migration 11 drops the delegation tables. `effective_authority` remains and reports the caller's own scopes and capabilities. The audit trail showed no use.
- Handoff briefings, replaced by structured handoffs on activities: the `/api/briefings` routes (including `/api/briefings/handoff/prd`), the MCP tools `get_briefing` and `briefing_list`, the `create_handoff_briefing` recovery action and the briefing generated on reassignment, the dashboard's Briefing button and search section, the `briefing_changes` group in `workspace_sync`, and the `briefing_generated` and `handoff_created` audit actions. No briefing had been generated since 2026-06-26, yet `briefing_list` results were offloaded 252 times at about 131K characters each, because briefing rows stored full briefing bodies in `agent_activity.metadata_json` and posed as activities. Migration 13 recreates the workspace triggers without their briefing branches and deletes the briefing rows and their workspace changes.

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
