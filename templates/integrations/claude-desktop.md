# Claude Desktop — Agent Core Integration

> Static example template. Use this as a reference when configuring Claude Desktop manually.

## MCP Configuration

Edit your Claude Desktop config file to add Agent Core as an MCP server.

**Config file location:**
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

Add Agent Core to the `mcpServers` section. If the file doesn't exist yet, create it:

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

**Restart Claude Desktop** after saving the file for the change to take effect.

---

## Available Tools

Once connected, Claude Desktop can use these Agent Core tools in every conversation:

| Tool | What it does |
| --- | --- |
| `memory_search` | Search your scoped memory by query |
| `memory_write` | Store facts, decisions, preferences, and notes |
| `memory_retract` | Soft-delete a memory record |
| `vault_get` | Get a credential reference (`AC_SECRET_*`) — never the raw secret |
| `vault_list` | List available credential references in scope |
| `activity_update` | Report task progress and send heartbeats |
| `activity_get` | Get task status across agents |
| `get_briefing` | Retrieve handoff context when switching agents |

---

## Verify the Connection

Test from the terminal before starting a conversation:

```bash
curl -X POST http://localhost:3500/mcp \
  -H "Authorization: Bearer YOUR_AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tool": "memory_search", "params": {"query": "test", "limit": 5}}'
```

Expected response:

```json
{"ok": true, "data": {"records": [], "count": 0, "retrieval_mode": "fts_only"}}
```

If you see `401 Unauthorized`, the API key in the config file doesn't match. Rotate the key from the dashboard (**Agents → [agent] → Rotate Key**) and update the config file.

---

## Notes

- Your agent identity is derived from the API key — you don't need to tell Claude which agent it is.
- Memory and vault operations are automatically scoped to your agent's allowed scopes.
- Raw credential values are never returned through MCP — only `AC_SECRET_*` references.
- If Agent Core is on a different machine on your LAN, replace `localhost:3500` with that machine's IP and port.
