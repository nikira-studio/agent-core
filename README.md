# Agent Core

**Persistent memory and a secure credential vault for all your AI agents — running locally on your own machine.**

If you use multiple AI coding agents (Claude Code, Cursor, Codex, Hermes, or anything else), you've hit these problems:

- You switch agents or start a new session and have to re-explain everything from scratch
- You copy-paste the same API keys, workspace decisions, and preferences over and over
- You can't have two agents working on the same workspace without manually keeping them in sync

Agent Core is a single local service all your agents connect to. They share memory, access the same credentials, and pick up where the last session left off — without you manually bridging the gap.

---

## What It Gives Your Agents

### Persistent Memory

Agents write facts, decisions, and preferences to Agent Core. The next session — same agent or different one — can search for them and pick up in context. No more starting cold.

```
claude-code writes: "We decided to use PostgreSQL, not SQLite, for the prod database."
                            ↓
codex reads:        memory_search("database decision") → gets that record back
```

Memory is scoped so agents only access what they're allowed to: their own private memory, shared workspace memory, or their owner's personal preferences.

### Encrypted Credential Vault

Store API keys and tokens once. Every agent that needs one gets a stable reference (`AC_SECRET_GITHUB_TOKEN`) instead of the raw value. The actual secret is never in a prompt, never in memory, never in a log.

```
You store:      GitHub token → vault
Agent gets:     AC_SECRET_GITHUB_TOKEN_1A2B3C4D   (not the real token)
At run time:    Credential Broker injects the real value into the process environment
```

### Shared Workspace Memory

If multiple users or agents work in the same repository or product area, grant each user-owned agent access to the same workspace scope. When one agent makes a decision, the others can find it.

```
workspace:my-app  ← both claude-code and codex have read/write access
                   decisions, context, and preferences written here are visible to both
```

---

## How It Works

Agent Core is a local HTTP server with a REST API and an MCP endpoint. Agents authenticate with an API key and call tools like `memory_search`, `memory_write`, and `vault_get`. Nothing is sent to any external service.

```
┌─────────────┐     MCP or REST      ┌──────────────────┐
│  Claude Code │ ──────────────────► │                  │
│  Cursor      │ ──────────────────► │   Agent Core     │
│  Codex       │ ──────────────────► │   localhost:3500 │
│  Hermes      │ ──────────────────► │                  │
│  any agent   │ ──────────────────► └──────────────────┘
└─────────────┘                            │
                                           │
                              ┌────────────┴────────────┐
                              │  SQLite + encrypted      │
                              │  vault on your disk      │
                              └──────────────────────────┘
```

---

## Quickstart

### Docker (recommended)

```bash
git clone https://github.com/nikira-studio/agent-core agent-core
cd agent-core
cp .env.example .env
cp docker-compose.example.yml docker-compose.yml
docker compose up -d
```

`docker-compose.yml` is gitignored so your local config (data paths, ports, custom networks) stays private. Edit it before starting if you need to customize anything.

Open `http://localhost:3500`. The setup screen will prompt you to create an admin account.

### Local Python

```bash
git clone https://github.com/nikira-studio/agent-core agent-core
cd agent-core
python3.11 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 3500
```

---

## Connect an Agent

Create an agent in the dashboard (Agents → New Agent) and copy the API key — it's shown once. Then use the **Integrations** page to get a ready-to-paste config for your tool.

For MCP-compatible clients (Claude Code, Cursor, Claude Desktop):

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

For REST clients or custom agents, every feature is also available through the HTTP API.

---

## Documentation

| Doc | What's in it |
| --- | --- |
| [Quickstart](docs/quickstart.md) | Step-by-step install, first agent, first memory write |
| [Integrations](docs/integrations.md) | MCP and REST setup for Claude Code, Cursor, and others |
| [Credential Broker](docs/credential-broker.md) | How `AC_SECRET_*` references work and how to resolve them |
| [Configuration](docs/configuration.md) | Environment variables and data directory layout |
| [Security](docs/security.md) | Scope model, secret handling, rate limits, deployment checklist |
| [API Reference](docs/api.md) | Full REST and MCP endpoint reference |
| [Backup & Restore](docs/backup-restore.md) | Export, restore, and maintenance |
| [Troubleshooting](docs/troubleshooting.md) | Common issues and fixes |

---

## Local Data

Everything stays on your machine:

```
data/
  agent-core.db       ← SQLite database (all memory, agents, vault metadata, activity)
  vault.key           ← Current Fernet encryption key for vault values
  vault.keyring       ← Historical keys (for post-rotation decryption)
  broker.credential   ← Credential for the local broker (auto-generated)
  backups/
```

`data/` is gitignored. Back up `vault.key`, `vault.keyring`, and `agent-core.db` together — the full backup export from the dashboard includes all three.

---

## Requirements

- Docker with Compose, **or** Python 3.11 for local development
- SQLite with FTS5 (standard in the Docker image and most Python 3.11 builds)
- Optional: [Ollama](https://ollama.com) for semantic memory search (falls back to full-text search if unavailable)

The Docker image is the supported runtime and uses Python 3.11. Code changes must remain compatible with Python 3.11 syntax and dependencies unless the Docker base image is intentionally changed in the same update.

Before reporting runtime-affecting work as complete, run:

```bash
python3.11 -m compileall app tests
pytest -q
docker compose build
docker compose up -d
curl http://localhost:3500/health
```

## License

[MIT](LICENSE)
