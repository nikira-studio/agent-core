# OpenClaw — Agent Core Integration

> Static example template. Use this when your client speaks standard MCP or REST and does not need a first-class generator preset.

## MCP Configuration

Add Agent Core as an MCP server in your OpenClaw MCP configuration:

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

Replace `YOUR_AGENT_API_KEY` with the key shown when you created the agent in the Agent Core dashboard.

---

## REST Integration

If you prefer REST over MCP, set these environment variables:

```bash
export AGENT_CORE_URL="http://localhost:3500"
export AGENT_CORE_API_KEY="YOUR_AGENT_API_KEY"
export AGENT_CORE_AGENT_ID="YOUR_AGENT_ID"
```

Then use the REST API directly:

```bash
# Search memory
curl -X POST $AGENT_CORE_URL/api/memory/search \
  -H "Authorization: Bearer $AGENT_CORE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "context", "limit": 10}'

# Write memory
curl -X POST $AGENT_CORE_URL/api/memory/write \
  -H "Authorization: Bearer $AGENT_CORE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"content": "Decision: use 4-space indent", "memory_class": "decision", "scope": "agent:YOUR_AGENT_ID"}'
```

---

## Available Tools

| Tool | What it does |
| --- | --- |
| `memory_search` | Search your scoped memory |
| `memory_write` | Write facts, decisions, preferences, opinions, beliefs |
| `memory_retract` | Soft-delete a memory record |
| `vault_get` | Retrieve a credential reference (`AC_SECRET_*`) |
| `vault_list` | List credential references in scope |
| `activity_update` | Update or create task activity |
| `activity_get` | Get a specific activity |
| `get_briefing` | Get handoff briefing for task reassignment |

---

## Verify the Connection

```bash
curl -X POST http://localhost:3500/mcp \
  -H "Authorization: Bearer YOUR_AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tool": "memory_search", "params": {"query": "test", "limit": 5}}'
```

Expected: `{"ok": true, "data": {"records": [], "count": 0, "retrieval_mode": "fts_only"}}`

---

## Security Model

- Each agent has scoped read/write access to memory and vault.
- Raw credentials are never exposed — only `AC_SECRET_*` references.
- The Agent Core Credential Broker resolves references at execution time.
- Agents cannot read other agents' private scoped data.
