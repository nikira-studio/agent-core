# Generic MCP Client — Agent Core Integration

> Static example template. Use this when your MCP client is not one of the first-class dashboard presets.

## Configuration

Agent Core exposes a standard HTTP MCP endpoint. Add it to your MCP client using the `type: "http"` transport with an Authorization header:

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

Replace `YOUR_AGENT_API_KEY` with the key shown when you created the agent in the Agent Core dashboard. The exact config key names may vary by MCP client — what matters is that the Authorization header reaches Agent Core with a valid agent API key.

---

## Calling a Tool Directly

```bash
curl -X POST http://localhost:3500/mcp \
  -H "Authorization: Bearer YOUR_AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tool": "memory_search", "params": {"query": "user preferences", "limit": 20}}'
```

---

## Tool Reference

### Memory

| Tool | Required params | Description |
|------|----------------|-------------|
| `memory_search` | `query` | Search memory records with FTS5 (+ semantic if Ollama is running) |
| `memory_write` | `content`, `memory_class` | Write a memory record. Classes: `fact`, `preference`, `decision`, `profile`, `opinion`, `belief`, `scratchpad` |
| `memory_get` | `scope` | List records by authorized scope |
| `memory_retract` | `record_id` | Soft-delete a record |

### Vault

| Tool | Required params | Description |
|------|----------------|-------------|
| `vault_list` | — | List credential references in authorized scopes |
| `vault_get` | `name` or `entry_id` | Get a specific `AC_SECRET_*` reference |

Vault tools never return raw secret values — only the `AC_SECRET_*` reference name.

### Activity

| Tool | Required params | Description |
|------|----------------|-------------|
| `activity_update` | — | Create or update the current agent task. Send every 1–2 minutes as a heartbeat. |
| `activity_get` | — | Get activity status for the current user |
| `get_briefing` | `from_agent_id` | Get a handoff briefing when taking over from another agent |

---

## Response Format

All responses use the standard envelope:

```json
{
  "ok": true,
  "data": { ... }
}
```

Errors:

```json
{
  "ok": false,
  "error": {
    "code": "SCOPE_DENIED",
    "message": "Access denied to this scope"
  }
}
```

---

## Discover Capabilities

Any authenticated agent can call `GET /spec` to get the full specification — all endpoints, MCP tools, scope model, and rate limits:

```bash
curl http://localhost:3500/spec \
  -H "Authorization: Bearer YOUR_AGENT_API_KEY"
```
