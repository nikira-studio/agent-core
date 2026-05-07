# Cursor — Agent Core Integration

## MCP Configuration

Add Agent Core as an MCP server in Cursor's config file.

**Config file location:**
- **Global (all workspaces):** `~/.cursor/mcp.json`
- **Workspace-level:** `.cursor/mcp.json` in your repo root

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

Restart Cursor after saving the file.

---

## Available Tools

Once connected, Cursor can use these Agent Core tools:

| Tool | What it does |
| --- | --- |
| `memory_search` | Search scoped memory records by query |
| `memory_write` | Store facts, decisions, preferences, and notes |
| `memory_retract` | Soft-delete a memory record |
| `vault_get` | Get a credential reference (`AC_SECRET_*`) — never the raw secret |
| `vault_list` | List available credentials in scope |
| `activity_update` | Report task progress and send heartbeats |
| `activity_get` | Get task status |
| `get_briefing` | Retrieve handoff context when switching agents |

---

## Verify the Connection

Test from the terminal before starting a session:

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

---

## Credential Resolution

Vault entries give agents `AC_SECRET_*` references instead of raw values. To resolve those references when running terminal commands, use the Credential Broker.

**If you installed Agent Core via Docker**, copy the broker script from the container first:

```bash
docker cp agent-core:/app/runner/agent_core_broker.py ./agent_core_broker.py
```

**Run a command with secrets injected:**

```bash
python agent_core_broker.py \
  --agent-id YOUR_AGENT_ID \
  --mode env \
  -- your-command arg1 arg2
```

Any environment variable containing an `AC_SECRET_*` reference is replaced with the real value in the child process. The parent shell is never modified.

See [Credential Broker](../docs/credential-broker.md) for full usage.
