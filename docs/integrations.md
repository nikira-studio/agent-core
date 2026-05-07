# Integrations

Agent Core supports REST, MCP, and a local Credential Broker wrapper. Any tool or runtime that can make HTTP requests can integrate with it.

---

## MCP

The MCP endpoint is at `/mcp` and requires agent API key authentication. Connect any MCP-compatible client (Claude Desktop, Cursor, OpenClaw, generic MCP hosts) by pointing it at your Agent Core instance.

**Example MCP client config:**

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

**Available MCP tools:**

| Tool | What it does |
| --- | --- |
| `memory_search` | Search authorized memory records by query |
| `memory_get` | List records in an authorized scope |
| `memory_write` | Write a memory record (with scope and PII checks) |
| `memory_retract` | Soft-delete a memory record |
| `vault_get` | Get an `AC_SECRET_*` reference for a vault entry |
| `vault_list` | List masked vault entries in authorized scopes |
| `activity_update` | Create or update an agent activity record |
| `activity_get` | Get an authorized activity record |
| `get_briefing` | Fetch a handoff briefing for an activity |

**Calling a tool directly:**

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

**Discovering capabilities:**

Any agent can call `GET /spec` with its agent API key to get the version, auth methods, MCP endpoint location, and available tool contracts. Unauthenticated callers can use `/spec/public` for the minimal public discovery response.

```bash
curl http://localhost:3500/spec \
  -H "Authorization: Bearer <agent-api-key>"
```

---

## Dashboard Setup

The dashboard **Integrations** page is `/agent-setup`. It generates tool-specific agent setup instructions, `CLAUDE.md`, `AGENTS.md`, MCP JSON, environment variables, and verification prompts for a selected user, workspace, agent, and target tool.

Use the **Agents** page to create or edit agent identities, API keys, and scopes. Use **Integrations** after the agent exists to generate the files and prompts that tell Claude Code, Codex, Cursor, Windsurf, or a generic MCP client how to connect and which Agent Core scopes to use.

The old `/integrations` URL redirects to `/agent-setup`.

The dashboard generator has first-class presets for Claude Code, Codex, Cursor, Windsurf, and the generic MCP/REST flows.

Static example templates are also available in `templates/integrations/` for clients that speak standard MCP or REST but do not need special generator behavior in the app:

| Template file | Notes |
| --- | --- |
| `templates/integrations/claude-code.md` | Example repo-level guidance for Claude Code |
| `templates/integrations/claude-desktop.md` | Static example for Claude Desktop |
| `templates/integrations/cursor.md` | Example repo-level guidance for Cursor |
| `templates/integrations/openclaw.md` | Static example for OpenClaw |
| `templates/integrations/generic-mcp.md` | Static generic MCP example |
| `templates/integrations/generic-rest.md` | Static generic REST example |

### Where to put the MCP config

The JSON snippet above goes in different files depending on the tool:

**Claude Code** — run the CLI command (preferred):

```bash
claude mcp add --transport http agent-core http://localhost:3500/mcp \
  --header "Authorization: Bearer YOUR_AGENT_API_KEY"
```

Or add manually to `.claude/settings.json` (workspace-level) or `~/.claude/settings.json` (global).

**Claude Desktop**

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Create the file if it doesn't exist. Restart Claude Desktop after saving.

**Cursor**

- Global (all workspaces): `~/.cursor/mcp.json`
- Workspace-level: `.cursor/mcp.json` in your repo root

Restart Cursor after saving.

**OpenClaw / other MCP hosts** — consult that tool's MCP server configuration docs. The config key names may vary, but every tool needs the `Authorization` header with your agent API key.

These are guidance documents, not stored integration records. They use the same REST and MCP APIs documented in [API Reference](api.md).

---

## Credential Injection

When a tool needs a credential, the flow looks like this:

1. The agent calls `vault_get` (MCP) or `POST /api/vault/entries/{id}/reference` (REST).
2. Agent Core returns an `AC_SECRET_*` reference name — not the raw value.
3. The agent includes that reference in its tool configuration.
4. At execution time, the local Credential Broker resolves the reference and injects the raw value.

See [Credential Broker](credential-broker.md) for setup and usage.

---

## Activity Heartbeats

Agents should heartbeat their active work so Agent Core can surface live activity and detect when an agent goes offline:

```bash
# Start an activity
curl -X POST http://localhost:3500/api/activity \
  -H "Authorization: Bearer <agent-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "task_description": "Refactoring memory search tests",
    "memory_scope": "agent:coding-agent"
  }'

# Send a heartbeat (every 60–120 seconds recommended)
curl -X POST http://localhost:3500/api/activity/<activity-id>/heartbeat \
  -H "Authorization: Bearer <agent-api-key>"
```

Activities that miss heartbeats beyond `AGENT_CORE_STALE_THRESHOLD_MINUTES` (default: 5 minutes) are automatically marked `stale`. The dashboard surfaces these with recovery options: resume, reassign, generate briefing, or cancel.

---

## Handoff Briefings

When handing work to another agent, generate a briefing from the current activity:

```bash
curl -X POST http://localhost:3500/api/briefings/handoff \
  -H "Authorization: Bearer <agent-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"activity_id": "<activity-id>"}'
```

The briefing includes the active task, recent completed tasks, key decisions, and relevant memory — giving the incoming agent immediate context without a raw memory dump.

---

## REST Integration

For clients that don't support MCP, every feature is available through the REST API. The same agent API key works for both.

A minimal Python client:

```python
import httpx

BASE_URL = "http://localhost:3500"
API_KEY = "your-agent-api-key"
headers = {"Authorization": f"Bearer {API_KEY}"}

# Write memory
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
entry = httpx.get(f"{BASE_URL}/api/vault/entries?scope=user:admin", headers=headers).json()
ref = httpx.post(f"{BASE_URL}/api/vault/entries/{entry['data']['entries'][0]['id']}/reference", headers=headers).json()
# → {"ok": true, "data": {"variable_name": "AC_SECRET_GITHUB_TOKEN_1A2B3C4D"}}
```

See [API Reference](api.md) for the full endpoint documentation.
