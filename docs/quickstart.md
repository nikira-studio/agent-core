# Quickstart

Get Agent Core running, create your first admin account, and connect an agent.

---

## Prerequisites

- **Docker with Compose** (recommended), or Python 3.11 for local development
- A browser for the dashboard
- Optional: [Ollama](https://ollama.com) running locally for semantic memory search (falls back to FTS5 without it)

---

## Option 1: Docker (Recommended)

Docker is the supported runtime. It handles Python 3.11, SQLite dependencies, and data persistence without any additional setup.

```bash
git clone https://github.com/nikira-studio/agent-core agent-core
cd agent-core
cp .env.example .env
cp docker-compose.example.yml docker-compose.yml
docker compose up -d
```

`docker-compose.yml` is gitignored — your local copy won't be committed. Edit it to set your data directory, ports, or any custom network config before starting.

Open `http://localhost:3500`. The first time you visit, you'll be prompted to create an admin account.

---

## Option 2: Local Python

```bash
git clone https://github.com/nikira-studio/agent-core agent-core
cd agent-core
python3.11 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 3500
```

Open `http://localhost:3500` and register the first admin user.

> **Note:** Use Python 3.11 locally unless you are intentionally updating the Docker runtime. Newer local Python versions can accept syntax that the Docker image rejects. Your Python build must also include SQLite with FTS5. If you hit FTS5 errors, use the Docker path instead.

---

## Create Your First Agent

Once logged in, open **Agents** in the dashboard sidebar and create a new agent. Give it a name like `coding-agent` and copy the API key that appears — **it's shown exactly once and cannot be retrieved later.**

If you prefer the API:

```bash
curl -X POST http://localhost:3500/api/agents \
  -H "Authorization: Bearer <admin-session>" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "coding-agent",
    "display_name": "Coding Agent",
    "read_scopes": ["agent:coding-agent", "user:admin", "shared"],
    "write_scopes": ["agent:coding-agent"]
  }'
```

The `read_scopes` and `write_scopes` control what memory and vault entries this agent can access. User scopes are personal to the agent owner/default user. For collaboration across people or tools, create a workspace and grant agents access to its `workspace:<id>` scope.

---

## Connect an MCP Client

The fastest way to connect a tool is the **Integrations** page in the dashboard — it generates ready-to-paste config for Claude Code, Codex, Cursor, Windsurf, and generic MCP clients. Open `http://localhost:3500/agent-setup`, select your user, workspace, agent, and target tool, then copy the generated output.

For a quick manual test to confirm the server is reachable:

```bash
curl -X POST http://localhost:3500/mcp \
  -H "Authorization: Bearer <agent-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"tool": "memory_search", "params": {"query": "test", "limit": 5}}'
```

Expected response:

```json
{"ok": true, "data": {"records": [], "count": 0, "retrieval_mode": "fts_only"}}
```

If you get a `401`, double-check that you're using the agent API key (not your dashboard password). If you get a connection error, confirm Agent Core is running on port 3500.

---

## Write and Search Memory

Agents write structured memory records with a class, scope, and content:

```bash
curl -X POST http://localhost:3500/api/memory/write \
  -H "Authorization: Bearer <agent-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "Prefer concise status updates.",
    "memory_class": "preference",
    "scope": "agent:coding-agent"
  }'
```

Search for it later:

```bash
curl -X POST http://localhost:3500/api/memory/search \
  -H "Authorization: Bearer <agent-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "status updates"
  }'
```

Memory searches are scoped to whatever the agent has read access to. You don't need to specify a scope on every search — the agent's permissions determine what it can see.

---

## Store a Credential

> **Before you can reveal a credential value in the dashboard or export a backup, you must enroll a TOTP authenticator.** Go to **Settings → Security** and scan the QR code with any authenticator app (Google Authenticator, Authy, 1Password, etc.). This is a one-time setup step.

Add a secret to the Vault:

```bash
curl -X POST http://localhost:3500/api/vault/entries \
  -H "Authorization: Bearer <agent-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "scope": "agent:coding-agent",
    "name": "github-token",
    "value": "<your-secret>",
    "label": "GitHub Token",
    "value_type": "api"
  }'
```

The response includes a stable `AC_SECRET_*` reference name. The raw value is never returned through the normal API — only through the Credential Broker at execution time.

See [Credential Broker](credential-broker.md) for how to use that reference with local tools.

---

## Run Tests

```bash
python3.11 -m compileall app tests
pytest -q
```

The test suite uses isolated temporary databases. It won't touch your local `data/` directory.

For changes that affect startup, routing, dependencies, or generated dashboard code, also verify the Docker runtime:

```bash
docker compose build
docker compose up -d
curl http://localhost:3500/health
```

---

## What's Next

- [Configuration](configuration.md) — customize port, data path, session timeouts, and more
- [Security](security.md) — understand the scope model and deployment checklist
- [Credential Broker](credential-broker.md) — resolve `AC_SECRET_*` references for local tools
- [API Reference](api.md) — full endpoint documentation
- [Integrations](integrations.md) — connect Claude Code, Cursor, and other tools
