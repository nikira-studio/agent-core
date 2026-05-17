# Integrations

Agent Core exposes memory, credentials, connector actions, and activity tracking over **MCP** (Model Context Protocol) and **REST**. It doesn't schedule or orchestrate — it gives agents services to call when they need them.

The dashboard **Integrations** page at `/integrations` generates ready-to-paste configs for specific tools and is usually the fastest path. This doc explains what's happening under the hood and covers cases the generator doesn't handle. The current presets include Claude Code, Codex, Cursor, Windsurf, Antigravity, and a generic MCP/REST path.

The dashboard **Connectors** page at `/connectors` is where you register external capabilities for Agent Core itself. It supports:

- importing OpenAPI specs as connector types
- registering native MCP servers as connector types
- binding those capabilities to credentials and scopes

OpenAPI imports and MCP server registrations both become first-class connector types in the same catalog. The difference is only how the connector type was discovered and how Agent Core executes it later.

For operator-managed internal services on your own network, keep the default SSRF guard in place and opt in only through deployment config. Set `AGENT_CORE_ALLOWED_INTERNAL_HOSTS` for trusted hostnames like `firecrawl` or `searxng`, then use binding `config_json` overrides such as `{"base_url":"http://firecrawl:3002/v1","auth_mode":"none"}` when you need an imported OpenAPI spec to talk to that internal deployment.

---

## MCP

MCP is the native protocol for Claude Code, Cursor, Claude Desktop, and a growing list of other tools. When you connect via MCP, your agent can call tools like `memory_search` and `credential_get` directly from within a session, without writing any code.

**The MCP endpoint is at `/mcp`** and requires your agent API key.

Add it to any MCP-compatible client with this config:

```json
{
  "mcpServers": {
    "agent-core": {
      "type": "http",
      "url": "http://localhost:3500/mcp",
      "headers": {
        "Authorization": "Bearer <agent-api-key>"
      }
    }
  }
}
```

### Antigravity

Antigravity uses the same MCP JSON shape, but the endpoint field should be `serverUrl` instead of `url`:

```json
{
  "mcpServers": {
    "agent-core": {
      "type": "http",
      "serverUrl": "http://localhost:3500/mcp",
      "headers": {
        "Authorization": "Bearer <agent-api-key>"
      }
    }
  }
}
```

### What MCP Gives Your Agents

Once connected, these tools are available in any session:

| Tool | What it does |
| --- | --- |
| `memory_search` | Search all memory the agent has access to, by natural language query |
| `memory_get` | List records in a specific scope |
| `memory_write` | Save a memory record (automatically checks for PII on shared scopes) |
| `memory_retract` | Soft-delete a memory record |
| `credential_get` | Get an `AC_SECRET_*` reference for a stored credential |
| `credential_list` | List credential entries the agent can access (metadata and references only — no raw values) |
| `activity_update` | Create or update an activity record (for tracking active work) |
| `activity_get` | Get the status of an activity |
| `activity_list` | List activities visible to the current agent or user |
| `get_briefing` | Pull a handoff briefing when taking over from another agent |
| `briefing_list` | List generated briefings visible to the current agent or user |
| `connectors_list` | List available connector types |
| `connectors_bindings_list` | List connector bindings in authorized scopes |
| `connectors_bindings_test` | Test a binding using the stored credential |
| `connectors_actions_list` | List actions supported by a connector type |
| `connectors_run` | Run one connector action server-side using a binding |

This is the pattern throughout Agent Core: agents connect, discover what they’re allowed to use, and call what they need. Agent Core provides the capabilities and logs the results — it doesn’t act as a workflow engine.

For memory writes, `slot_key` can make a preference deterministic by keeping one active value per slot, and `valid_from`, `valid_to`, and `last_confirmed_at` are optional freshness hints.

If one agent needs to hand work to another, write the durable state into the shared workspace scope and generate or link a briefing. If you are reviewing prior work, use `memory_search`, `activity_list`, and `briefing_list` together before changing anything. The private `agent:<id>` scope is only for scratch notes for that specific agent and should not be treated as the handoff channel.

## Connector Setup in Agent Core

Use **OpenAPI import** when the service already publishes a REST spec. Use **Import MCP Server** when the service is a native MCP server and you want Agent Core to keep the capability catalog, credentials, scopes, execution history, and audit trail in one place.

### OpenAPI import

- Go to `/connectors`
- Click `+ Import API Spec`
- Paste or upload the OpenAPI document
- Create a binding to a stored credential

For internal deployments, the binding can override the target server with `config_json.base_url`. If the imported spec declares auth but the local service does not use it, set `config_json.auth_mode` to `none` so Agent Core does not require or inject a credential for that binding.

Example: for a local Firecrawl deployment, import the public spec from GitHub, then create a binding with:

```json
{
  "base_url": "http://firecrawl:3002/v1",
  "auth_mode": "none"
}
```

And set `AGENT_CORE_ALLOWED_INTERNAL_HOSTS=firecrawl` in the Agent Core environment. That keeps the imported spec public while routing execution to your trusted internal Firecrawl instance.

### MCP server import

- Go to `/connectors`
- Click `+ Import MCP Server`
- Enter the MCP endpoint URL, transport, and optional discovery headers
- Refresh later if the server's tool set changes
- Create a binding to a stored credential if the server needs one

The MCP import is server-side only: it discovers and stores the tool list in Agent Core, then agents call those tools through the same connector/binding execution path as OpenAPI-backed connectors.

For the same reason, MCP bindings can carry per-binding `timeout_ms` and `headers_json` for discovery or execution, but the endpoint itself must still be an operator-trusted URL. If you need to point a binding at an internal hostname, register it in `AGENT_CORE_ALLOWED_INTERNAL_HOSTS` rather than weakening the default URL guard.

Transport guidance:

- `streamable_http` is the preferred native MCP transport for HTTP-accessible servers
- `http` is accepted as an alias for HTTP-native MCP servers
- stdio-only MCP servers are not launched directly by Agent Core; put them behind a small HTTP bridge/proxy and register the bridge URL instead
- Agent Core rejects unsupported transports at import time so you do not end up with a connector that cannot execute later

Test that MCP is reachable:

```bash
curl -X POST http://localhost:3500/mcp \
  -H "Authorization: Bearer <agent-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "tool": "memory_search",
    "params": {
      "query": "handoff decisions",
      "scope": "agent:coding-agent"
    }
  }'
```

To discover what Agent Core supports at runtime, any authenticated agent can call `GET /spec`:

```bash
curl http://localhost:3500/spec \
  -H "Authorization: Bearer <agent-api-key>"
```

Unauthenticated callers can use `/spec/public` for a minimal discovery response.

---

## Tool-Specific Setup

### Claude Code

**Option 1 — CLI (recommended):**

```bash
claude mcp add --transport http --scope user agent-core http://localhost:3500/mcp \
  --header "Authorization: Bearer YOUR_AGENT_API_KEY"
```

This adds Agent Core to your user-level config so it's available in every project.

**Option 2 — Project config file:**

Create `.mcp.json` in your repo root (this file can be committed and shared with your team):

```json
{
  "mcpServers": {
    "agent-core": {
      "type": "http",
      "url": "http://localhost:3500/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_AGENT_API_KEY"
      }
    }
  }
}
```

> If you're committing `.mcp.json`, consider using an environment variable instead of a hardcoded key: `"Authorization": "Bearer ${AGENT_CORE_API_KEY}"`. Set the variable in your shell or `.env` before starting Claude Code.

**Verify the connection:**

```bash
curl -X POST http://localhost:3500/mcp \
  -H "Authorization: Bearer YOUR_AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tool": "memory_search", "params": {"query": "test", "limit": 5}}'
```

Expected: `{"ok": true, "data": {"records": [], "count": 0, "retrieval_mode": "fts_only"}}`

This curl test only confirms MCP reachability. For the full end-to-end verification flow, use the generated **Verification Prompt** and run it in the connected agent. That prompt writes a workspace memory record, reads it back, checks credential and connector visibility, and reports workspace-scoped and user-scoped bindings separately when both exist.

You can run that verification prompt immediately after wiring MCP. You do not need a `CLAUDE.md` or `AGENTS.md` file first; those files are only for persistent repository-level instructions when you want them.

**CLAUDE.md snippet:**

The **Integrations** page generates a full `CLAUDE.md` snippet tailored to your agent and workspace. Paste it into your repo's `CLAUDE.md` to give Claude Code context about what's available — which scopes to use, when to search memory, and how to handle credentials. Here's a minimal version to get started:

```markdown
## Agent Core

You are connected to Agent Core at http://localhost:3500.

- Search memory at the start of each session: `memory_search` with a natural language query. If a broad query returns little or nothing, retry with exact topic values, exact words from prior records, or a known record id. When embeddings are unavailable, exact tokens and known ids are more reliable than conceptual searches.
- Store decisions, preferences, and facts: `memory_write`
- For credentials: use `credential_get` to retrieve an AC_SECRET_* reference — never ask the user for raw API keys
- Send `activity_update` heartbeats every 1–2 minutes while working on a task
```

---

### Claude Desktop

Edit (or create) the config file:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "agent-core": {
      "type": "http",
      "url": "http://localhost:3500/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_AGENT_API_KEY"
      }
    }
  }
}
```

Restart Claude Desktop after saving.

---

### Cursor

Create or edit the MCP config file:

- **Global (all projects):** `~/.cursor/mcp.json`
- **Workspace-level (this repo only):** `.cursor/mcp.json` in your project root

```json
{
  "mcpServers": {
    "agent-core": {
      "type": "http",
      "url": "http://localhost:3500/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_AGENT_API_KEY"
      }
    }
  }
}
```

Restart Cursor after saving.

---

### Other MCP Hosts

Every MCP-compatible tool needs the same two things: the endpoint URL and the `Authorization` header. The JSON structure above works for any client that supports the `type: http` transport. Key names may vary slightly — check that tool's docs for where to put it.

```json
{
  "mcpServers": {
    "agent-core": {
      "type": "http",
      "url": "http://localhost:3500/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_AGENT_API_KEY"
      }
    }
  }
}
```

---

## Generating Integration Files from the Dashboard

The **Integrations** page (`/integrations`) generates everything you need for a specific tool in one place:

- Ready-to-paste MCP JSON
- `CLAUDE.md` or `AGENTS.md` content with instructions for the agent
- Environment variable snippets
- Verification prompts to paste into the agent to confirm it's connected

Select your user, workspace, agent, and target tool. The generator has first-class presets for Claude Code, Codex, Cursor, Windsurf, Antigravity, and generic MCP/REST flows.

The Integrations page generates the canonical setup text and downloadable files. Each output type is available for every tool preset:

| Output type | What it contains |
| --- | --- |
| Instructions | Human-readable setup steps and scope guidance for the selected tool |
| MCP Config | Ready-to-paste JSON for the agent's MCP config file (`agent-core-mcp-config.txt`) |
| Environment Variables | `.env`-style variable snippet (`agent-core.env`) |
| CLAUDE.md | Repo-level instructions for Claude Code to paste into `CLAUDE.md` |
| AGENTS.md | Equivalent instructions for Codex or other `AGENTS.md`-aware tools |
| Session Prompt | A startup prompt the agent can run at the beginning of each session |
| Verification Prompt | A one-time prompt that confirms the full end-to-end connection is working |

---

## Two Ways to Use Secrets

Agent Core gives you two distinct paths for using stored credentials. Pick the one that fits how the action happens:

**Credential Broker — your local tool needs the secret.** The agent gets an `AC_SECRET_*` reference, your local tool gets the real value injected at runtime, and the broker is what does the injection. The secret travels from Agent Core to your local process only, never through the model.

**Connectors — Agent Core runs the action for you.** The agent tells Agent Core which binding to use and what action to run. Agent Core uses the stored credential server-side, calls the external service, and returns the result. The raw secret never leaves Agent Core at all.

### Credential Broker Flow

1. The agent calls `credential_get` (MCP) or `POST /api/credentials/entries/{id}/reference` (REST).
2. Agent Core returns an `AC_SECRET_*` reference — not the actual token.
3. The agent includes that reference in the tool configuration.
4. When the tool runs, the local Credential Broker intercepts the reference and injects the real value into the tool's environment.

See [Credential Broker](credential-broker.md) for setup instructions.

### Connectors: Agent Core Runs Actions Directly

If you want Agent Core itself to call an imported OpenAPI spec or another service on your behalf, use the **Connectors** page at `/connectors`.

The current flow is:

1. Import an OpenAPI spec for the service you want, or use the built-in `generic_http` connector for a quick one-off endpoint.
2. Create or pick a stored credential. You can create credentials directly on the Connectors page or inline while creating a binding.
3. Create a connector binding for the imported connector type.
4. Bind that connector to a scope like `workspace:<id>`, `user:<id>`, or `shared`.
5. Test the binding from the dashboard.
6. Run actions through MCP with `connectors_run`.

Connector types are instance-wide catalog entries. The built-in `generic_http` type is always available, and any imported spec becomes visible to other authenticated users in the same Agent Core instance.

This is the clearest example of the capability-layer model. The connector catalog is a service directory, and each action is a server-side capability that the agent can call when it needs that external system.

The agent sends a binding ID, an action name, and parameters. Agent Core resolves the credential server-side, calls the external service, logs the execution, and returns the result. The raw secret never reaches the agent.

Credential scope and binding scope are both intentional:

- **Credential scope** controls who can access the stored secret.
- **Binding scope** controls where the connector is available to agents.

For normal workspace use, set both to the same workspace. For advanced use, a credential in a user scope can power multiple workspace bindings if the acting agent has access to both.

When you want to distinguish personal bindings from workspace bindings, call `connectors_bindings_list` once with no scope filter and again with an explicit `scope` such as `user:<id>` or `workspace:<id>`.

Each binding currently links to one credential. Connector-specific non-secret settings, such as a default repo, base URL, auth header name, or query parameter name, belong in the binding config JSON.

### Workspace Collaboration

If a workspace is shared with multiple users, each user can still keep their own agents. The workspace owner or an admin grants the users collaborator access on the workspace record, and those users can then scope their own agents to `workspace:<id>`.

This is the recommended setup for team use:

1. Share the workspace with the users who need it.
2. Pick or create each user's own agent.
3. Give that agent `workspace:<id>` access only after the user can see the workspace.

That keeps agent ownership, workspace access, and connector binding scope aligned without making users share one agent identity.

If you're importing a spec and using a PAT or other bearer token, the flow is simple: import the spec, create a credential, bind the credential, then call `connectors_run` with the action you want. The agent never needs the raw token, and you never need to paste it into a prompt.

### The Right Mental Model

Think of Agent Core less like an orchestrator and more like a building of services:

- **Memory** is the shared reference room.
- **Credentials** are the secured service keys.
- **Connectors** are the service counters agents can walk up to.
- **Activity** is the live status board.

Agents decide when to use a service. Agent Core makes the service available, enforces scope, and logs what happened.

---

## Activity Tracking

Agents can report what they're working on so you can see live status in the dashboard. This is especially useful when multiple agents are working in parallel.

```bash
# Create an activity when you start a task
curl -X POST http://localhost:3500/api/activity \
  -H "Authorization: Bearer <agent-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "task_description": "Refactoring memory search tests",
    "memory_scope": "agent:coding-agent"
  }'

# Send a heartbeat periodically (every 60–120 seconds is fine)
curl -X POST http://localhost:3500/api/activity/<activity-id>/heartbeat \
  -H "Authorization: Bearer <agent-api-key>"
```

If an agent misses heartbeats for more than `AGENT_CORE_STALE_THRESHOLD_MINUTES` (default: 5 minutes), its activity is automatically marked `stale`. The dashboard surfaces these with options to resume, reassign to another agent, generate a briefing, or cancel.

## Takeover Workflow

If one agent runs out of tokens, hits a weekly limit, or otherwise needs to stop before finishing, the next agent can continue from Agent Core state instead of starting blind.

The practical flow is:

1. The current agent keeps its activity record up to date with `activity_update` heartbeats.
2. It writes durable decisions, facts, and handoff notes to memory when something should survive beyond the current session.
3. When work stops, the next agent reads the latest activity, relevant memory, and any generated briefing.
4. If an activity is stale or being handed off intentionally, generate a briefing with `/api/briefings/handoff` or `get_briefing`. Briefings are handoff artifacts created on demand, not something the system scheduler produces automatically.

This isn't automatic orchestration — it's a durable handoff trail. A different agent picks up where the last one stopped, with full context, instead of starting blind.

If work needs to cross users or workspaces, make that explicit in the activity scope and briefing trail. If a broad memory search returns nothing, retry with exact topic values, specific words from prior records, or a known record ID — conceptual queries can miss when embeddings aren't available.

---

## Handing Work to Another Agent

When one agent needs to pass work to another — switching tools, handing off a task, or escalating — you can generate a briefing that gives the incoming agent immediate context:

```bash
curl -X POST http://localhost:3500/api/briefings/handoff \
  -H "Authorization: Bearer <agent-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"activity_id": "<activity-id>"}'
```

The briefing includes the current task description, recent decisions, key facts, and relevant memory pulled from the activity's scope. The incoming agent can call `get_briefing` via MCP to pull this as part of its startup.

---

## REST Integration

If your tool doesn't support MCP, every feature is also available through REST using the same agent API key.

A quick Python example:

```python
import httpx

BASE_URL = "http://localhost:3500"
API_KEY = "your-agent-api-key"
headers = {"Authorization": f"Bearer {API_KEY}"}

# Write a memory record
httpx.post(f"{BASE_URL}/api/memory/write", headers=headers, json={
    "content": "User prefers two-space indentation",
    "memory_class": "preference",
    "scope": "agent:coding-agent",
    "domain": "engineering",
    "topic": "style"
})

# Search memory
results = httpx.post(f"{BASE_URL}/api/memory/search", headers=headers, json={
    "query": "indentation preference"
}).json()

# Get a credential reference
entries = httpx.get(f"{BASE_URL}/api/credentials/entries?scope=user:admin", headers=headers).json()
entry_id = entries["data"]["entries"][0]["id"]
ref = httpx.post(f"{BASE_URL}/api/credentials/entries/{entry_id}/reference", headers=headers).json()
# → {"ok": true, "data": {"variable_name": "AC_SECRET_SERVICE_TOKEN_1A2B3C4D"}}
```

See [API Reference](api.md) for the full endpoint documentation.
