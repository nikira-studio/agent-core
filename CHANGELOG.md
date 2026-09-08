# Changelog

Notable changes to Agent Core are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Memory writes now report advisory warnings for likely active duplicates and matches to previously retracted records. Evidence-based confirmation is available through MCP, REST, and the dashboard.
- Memory consolidation can run on the configured maintenance interval, with an unconfirmed-inference rule, per-scope and installation-wide pending caps, a per-run budget, and fair rotation across rule/scope groups.
- Fresh MCP activity creation can return a bounded `since_last_active` workspace digest when its execution is not caught up. The Activity dashboard now shows execution-linked workspace activity rates for the installation lifetime and the last 30 days.

### Fixed

- Paperclip `list_issues` now accepts and forwards `limit`. If an upstream Paperclip server ignores the parameter, Agent Core trims the list locally and marks the result with `limit_applied_locally`.
- Updated built-in Paperclip adapter version to `1.4.2`, so existing installations show **Update** in the adapter library after deployment.
- Restored table-cell layout for dashboard action columns, which keeps the credentials table header aligned with its action buttons.
- Hermes memory prefetch now uses the Agent Core key's default recall scopes and caps injected context at 12,000 characters by default. Set `AGENT_CORE_MAX_CONTEXT_CHARS` to adjust the cap from 512 through 50,000 characters.

### Changed

- Generated assistant onboarding is now a concise setup guide. It includes the Hermes MCP configuration location and directs agents to repository `AGENTS.md` or `CLAUDE.md` files for the complete operating contract.
- Memory provenance fields are now authority-controlled: agents cannot claim human-authored source kinds, and `last_confirmed_at` can only be set by confirmation with evidence or verification.
- Merge restore now treats imported memory as `external_import`, clears confirmation from the source installation while retaining it in audit provenance, and leaves review proposals behind for the destination installation to regenerate.

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
