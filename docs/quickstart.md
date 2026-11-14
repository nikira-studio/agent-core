# Quickstart

This guide takes you from zero to a running Agent Core with an agent that can write and recall shared memory. The final steps show how to add credentials and services when a task needs them.

Agent Core is a local capability layer for agents: memory, credentials, services, and activity tracking. It is not a scheduler or orchestration OS. Your agents connect to it when they need services.

## What you set up

1. Run Agent Core and create an agent identity.
2. Connect that agent over MCP and verify shared memory.
3. Store a credential only when a local tool or external service needs one.
4. Add a service connection only when Agent Core should call that service itself.

Activities, workspaces, handoffs, webhooks, and backup are available later. They do not block the first memory workflow.

---

## Before you start

You need one of:

- **Docker with Compose**, the recommended path. It handles everything automatically.
- **Python 3.11 or 3.12**, for local development or if you prefer not to use Docker. (The Docker image runs 3.11; the test suite also passes on 3.12.)

You'll also want a browser for the dashboard.

Optional but recommended: [Ollama](https://ollama.com) running locally. Agent Core uses it for semantic (AI-powered) memory search. Without it, memory search still works. It falls back to fast full-text search automatically.

---

## Step 1: Get Agent Core running

### Option A: Docker (recommended)

Docker is the supported runtime. It takes care of the Python version, SQLite dependencies, and data persistence without any manual setup.

```bash
git clone https://github.com/nikira-studio/agent-core agent-core
cd agent-core
cp .env.example .env
cp docker-compose.example.yml docker-compose.yml
docker compose up -d
```

`docker-compose.yml` is gitignored, so your local copy (with your data path, port, and network settings) won't accidentally get committed. Edit it before starting if you need to customize anything.

Open `http://localhost:3500`. You'll see the setup screen. The example compose file listens on this machine only (`127.0.0.1`) because **the first person to open the dashboard becomes its admin**; register before you expose the port to a network.

### Option B: Local Python

```bash
git clone https://github.com/nikira-studio/agent-core agent-core
cd agent-core
python3 -m venv .venv      # python3 must be 3.11 or 3.12; use python3.11 or python3.12 if your default is different
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 3500   # run it from the project folder, the one that contains app/
```

Open `http://localhost:3500` and register the first admin user.

> **Stay on 3.11 or 3.12 for local work.** Newer Python versions accept syntax that the Docker image (which runs Python 3.11) will reject, so run `python3.11 -m compileall app` before you build an image. If you hit FTS5 database errors, switch to the Docker path, which bundles a compatible SQLite build.

---

## Step 2: Create your admin account

The first time you open the dashboard, you'll be prompted to register. This account becomes the admin: it can manage all agents, view the audit log, and access backup and restore. Keep that password somewhere safe.

After registering, you're in. **Needs you** is the home page: it lists what is waiting on you (an empty list means nothing to do), whether everything is healthy, and a short **Getting started** list for a new install.

---

## Step 3 (optional): Set up two-step sign-in

You can skip this and come back later. To enable login MFA, enroll a TOTP authenticator. It's a one-time setup.

Go to **Settings → Account**, find **Sign-in**, and scan the QR code with any authenticator app (Google Authenticator, Authy, 1Password, or anything else that supports TOTP). Confirm the first code to activate it. You can reset or disable OTP later from the same page.

You won't be prompted for it during normal use, only at login.

---

## Step 4: Create a workspace and your first agent

First open **Agents → Workspaces** and create a workspace, for example `my-project`: the shared place where agents keep memory and tasks. With one workspace, a new agent is set up to read and write it by default.

Agents are the identities that connect to Agent Core. Each one gets its own API key and its own scope of what memory, credentials, and service capabilities it can access. Service capabilities can come from imported OpenAPI specs, native MCP server registrations, the built-in Generic HTTP fallback, or installed **adapters**. Install adapters from the **Adapter Library** on the Services page; user-installed adapter files live in `data/adapters/` and built-in templates ship in `app/adapter_templates/` (see [Adapters](integrations.md#adapters)).

Open **Agents** in the sidebar and create a new agent. Give it a descriptive name like `claude-coding-agent` or `cursor-agent`. Creating an agent does not show a key. Open the **Connect** page (the dialog after you save links to it) to generate the agent's key and its tool settings. The key is shown **once**; copy it into the tool right away. There is no way to read it later, but you can generate a new one (which stops the old one working). Through the API, `POST /api/agents/coding-agent/rotate_key` (same header as below) returns the key once.

If you'd rather use the API, you act as a person with an **operator token**: a short-lived key you create in **Settings → Account → Operator tokens** while signed in (5 to 60 minutes, shown once; scripts that create agents, grants or backups use it instead of your password). Replace `<operator-token>` below with it:

```bash
curl -X POST http://localhost:3500/api/agents \
  -H "Authorization: Bearer <operator-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "coding-agent",
    "display_name": "Coding Agent",
    "read_scopes": ["agent:coding-agent", "user:admin", "workspace:my-project"],
    "write_scopes": ["agent:coding-agent", "workspace:my-project"]
  }'
```

The `read_scopes` and `write_scopes` control what this agent can see and write. By default, an agent can only write to its own private scope (`agent:<id>`). You can also give it access to shared context or a specific workspace, which is useful if you have multiple agents collaborating on the same project.

The **Shared memory** option adds the `shared` scope: memory every user and workspace can see. Give it only to an agent that should read or write context for everyone; write access to `shared` also needs the agent to be in `AGENT_CORE_SHARED_SCOPE_AGENTS` or have `shared` in its write scopes.

---

## Step 5: Connect your tool

The fastest way to connect an agent to your tool is the **Connect** page at `http://localhost:3500/integrations`. Select your user, workspace, agent, and target tool, and it generates the exact config or `CLAUDE.md` content you need to paste in.

You do **not** need to create a `CLAUDE.md` or `AGENTS.md` file before the verification step. The verification prompt works as soon as the agent has MCP access and the right scopes.

For a quick manual connection using MCP (works with Claude Code, Cursor, Claude Desktop, and others):

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

To verify the connection is working:

```bash
curl -X POST http://localhost:3500/mcp \
  -H "Authorization: Bearer <agent-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"tool": "memory_search", "params": {"query": "test", "limit": 5}}'
```

You should see:

```json
{"ok": true, "data": {"records": [], "retrieval_mode": "no_candidates", "total": 0, ...}}
```

If you get a `401`, you're using the wrong key. Make sure it's the agent API key, not your dashboard password. A connection error means Agent Core isn't running on port 3500.

After the MCP connection is working, run the generated **Verification Prompt** in the connected agent. The prompt starts a workspace execution, verifies change delivery and acknowledgement, writes and reads memory, checks credential and service visibility, and updates activity.

---

## Step 6: Write and search memory

Once connected, agents write memory records with three required fields: a class (what kind of thing it is), a scope (who can see it), and the content itself.

The class matters more than it looks. A **fact** is settled by checking: someone or something could verify it against the code, a host, or a service. A **decision** is settled by someone deciding, and nothing can verify it. That split is what lets Agent Core re-check facts on a schedule while leaving your decisions alone. Reporting what you just did is neither, and belongs in the activity trail instead.

If you are writing a preference, add a `slot_key` to keep one active value per slot: a new preference with the same scope and slot supersedes the previous one. `valid_from` and `valid_to` describe when the content was true in the world, which is a separate timeline from when it was written; set them when a record covers a period other than "from now on".

```bash
curl -X POST http://localhost:3500/api/memory/write \
  -H "Authorization: Bearer <agent-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "Prefer concise status updates, no bullet walls.",
    "memory_class": "preference",
    "scope": "agent:coding-agent"
  }'
```

Search retrieves it later:

```bash
curl -X POST http://localhost:3500/api/memory/search \
  -H "Authorization: Bearer <agent-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"query": "status updates"}'
```

A search covers the agent's default recall scopes: everything it can read, unless the agent was given a narrower default set (Agents → Edit), in which case pass `scope` to reach another one. `retrieval_mode` says how it ran: `hybrid` (meaning and keywords), `fts_only` (keywords only, because the embedding backend was off or failed) or `no_candidates` (no record in the scopes searched, so nothing to find).

---

## Step 7: Store a credential

Credentials are managed from the **Credentials** page. A credential is the encrypted secret itself: an API key, token, password, URL, or config value. A service connection is separate. It tells Agent Core how to use a stored credential with a service such as an imported OpenAPI spec, a registered native MCP server, the built-in Generic HTTP fallback, or an installed adapter manifest.

From the dashboard:

1. Go to **Credentials**.
2. Click **New Credential**.
3. Choose a scope such as `user:<id>`, `workspace:<id>`, or `agent:<id>`.
4. Enter the secret value and save.

The raw value is encrypted immediately. Normal API and dashboard responses show metadata and an `AC_SECRET_*` reference, not the raw value.

You can edit a credential later. Name, label, and type can be changed normally. The replacement secret field is optional: leave it blank to keep the current encrypted value, or enter a new value to overwrite it.

You can also create a credential through the API:

```bash
curl -X POST http://localhost:3500/api/credentials/entries \
  -H "Authorization: Bearer <agent-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "scope": "agent:coding-agent",
    "name": "github-token",
    "value": "<your-secret>",
    "label": "Service Token"
  }'
```

The response includes a stable `AC_SECRET_*` reference name, something like `AC_SECRET_SERVICE_TOKEN_1A2B3C4D`. That's what you pass to tools and scripts. The raw value never comes back through regular list/get APIs.

See [Local credential helper](credential-broker.md) for how to use that reference with local tools so the real token gets injected without appearing in your prompt or config files.

## Step 8: Create a service connection (optional)

If you want Agent Core to take actions on your behalf, like creating an issue, reading a repo, or calling an API, rather than just supplying a secret to a local tool, use the **Services** page in the dashboard.

This is the server-side capability path. The agent asks for a capability, Agent Core executes it, and the result comes back through the same scoped channel.

The flow:

1. Import an OpenAPI spec for the service you want, register a native MCP server, or use **Generic HTTP** for a one-off endpoint.
2. Go to **Services** and pick that service.
3. Select a stored credential, or create one inline while creating the connection.
4. Bind it to a scope like `workspace:<id>`.
5. Test the connection from the dashboard.
6. Call `connectors_run` from MCP to trigger actions. Agent Core uses the credential server-side and returns the result, whether the backing provider is OpenAPI or MCP.

Credential scope and connection scope are related but not identical. Credential scope controls who may access the stored secret. Connection scope controls where that service is available. In the common case, use the same workspace scope for both. For advanced setups, one personal credential can power multiple workspace connections.

The key difference from the Local credential helper: with services, Agent Core runs the action and the raw secret never leaves the server. With the helper, your local tool gets the real value injected at runtime. Use whichever fits your situation.

## Workspace collaboration

Workspaces are shared collaboration scopes. The workspace owner or an admin can add collaborators from the **Workspaces** page, and each collaborator can then grant their own agents access to that workspace.

Use this flow when two or more users are working on the same project:

1. Create the workspace once.
2. Add the other users as collaborators.
3. Have each user grant their own agent `workspace:<id>` access.

This keeps ownership and attribution separate from collaboration. The shared workspace is the source of truth, while agents remain owned by one user.

---

## What's next

- [How it works](how-it-works.md): the whole system end to end, and the reasoning behind it
- [Integrations](integrations.md): tool-specific setup for Claude Code, Cursor, Codex, and others
- [Configuration](configuration.md): customize port, data path, session timeouts, and more
- [Local credential helper](credential-broker.md): inject real credential values at runtime without exposing them to models
- [Security](security.md): understand the scope model and deployment checklist
- [API reference](api.md): full endpoint documentation
