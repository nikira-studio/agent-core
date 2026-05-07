# Claude Code — Agent Core Integration

## MCP Configuration

Add Agent Core as an MCP server so Claude Code can use memory, vault, and activity tools in every session.

**Option 1 — CLI (recommended):**

```bash
claude mcp add --transport http agent-core http://localhost:3500/mcp \
  --header "Authorization: Bearer YOUR_AGENT_API_KEY"
```

**Option 2 — Edit `.claude/settings.json` directly:**

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

Place this file in:
- `.claude/settings.json` in a specific workspace/repo root
- `~/.claude/settings.json` (user-level, applies to all workspaces)

Replace `YOUR_AGENT_API_KEY` with the key shown when you created the agent in the dashboard.

---

## CLAUDE.md Snippet

Add this to the `CLAUDE.md` in each repo to give Claude Code context about Agent Core:

```markdown
## Agent Core

You are connected to Agent Core at http://localhost:3500 as agent `YOUR_AGENT_ID`.

Use Agent Core for durable memory across sessions:
- Search memory: `memory_search` with a natural language query
- Store facts, decisions, and preferences: `memory_write`
- Default memory scope: `agent:YOUR_AGENT_ID`
- For workspace-specific context: `workspace:YOUR_WORKSPACE_ID` (if configured)

For credentials: use `vault_get` or `vault_list` to get `AC_SECRET_*` references.
Never ask the user for raw API keys — retrieve the reference and let the Credential Broker resolve it.

Send an `activity_update` heartbeat every 1–2 minutes while working on a task.
```

---

## Verify the Connection

After adding the MCP config, confirm it works before starting a session:

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

If you get `401`, check that the API key in the MCP config matches the one from the dashboard.

---

## Credential Resolution

Vault entries give agents `AC_SECRET_*` references instead of raw values. To resolve those references for local tool execution, use the Credential Broker.

**If you installed Agent Core via Docker**, copy the broker script from the container first:

```bash
docker cp agent-core:/app/runner/agent_core_broker.py ./agent_core_broker.py
```

**Run a command with secrets injected:**

```bash
python agent_core_broker.py \
  --agent-id YOUR_AGENT_ID \
  --mode env \
  -- your-tool-or-script
```

Any environment variable containing an `AC_SECRET_*` reference is replaced with the real value in the child process. The parent shell is never modified.

The broker reads `data/broker.credential` by default. Override with `--token PATH` or the `AGENT_CORE_BROKER_TOKEN` environment variable.

See [Credential Broker](../docs/credential-broker.md) for full usage.
