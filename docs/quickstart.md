# Quickstart

This guide takes you from nothing to a running Agent Core with one agent that can save and find shared memory. Later steps add a stored credential and a capability, for when a task needs them.

Agent Core is a program that sits beside your AI agents. It gives them memory they share, a safe place for credentials, capabilities (things an agent can do in other services) and a record of what they did. It does not run your agents or schedule their work.

## What you will do

1. Run Agent Core and sign in.
2. Create a workspace and an agent, and connect your tool to it.
3. Check that memory works.
4. Later, when a task needs it, store a credential and set up a capability.

Webhooks, handoffs and backups can wait. The first memory workflow does not need them.

---

## Before you start

You need one of:

- **Docker with Compose**, the recommended path. It handles everything automatically.
- **Python 3.11 or 3.12**, for local development or if you prefer not to use Docker. (The Docker image runs 3.11; the test suite also passes on 3.12.)

You'll also want a browser for the dashboard.

Optional: [Ollama](https://ollama.com) running locally. Agent Core uses it to search memory by meaning as well as by words. Without it, search still works and matches the words you type.

---

## Step 1: Get Agent Core running

### Option A: Docker (recommended)

Docker is the supported runtime. It takes care of the Python version, SQLite dependencies, and data persistence without any manual setup.

```bash
git clone https://github.com/nikira-studio/agent-core agent-core
cd agent-core
cp docker-compose.example.yml docker-compose.yml
docker compose up -d
```

`docker-compose.yml` is gitignored, so your local copy (with your data path, port, and network settings) won't accidentally get committed. Edit it before starting if you need to customize anything.

Open `http://localhost:3500`. You'll see a sign-in screen that offers to create your admin account. The example compose file listens on this machine only (`127.0.0.1`) because **the first person to open the dashboard becomes its admin**; register before you expose the port to a network.

### Option B: Local Python

```bash
git clone https://github.com/nikira-studio/agent-core agent-core
cd agent-core
python3 -m venv .venv      # python3 must be 3.11 or 3.12; use python3.11 or python3.12 if your default is different
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 3500   # run it from the project folder, the one that contains app/
```

Open `http://localhost:3500` and create the first admin account. To change settings such as the port or data folder, copy `.env.example` to `.env` first; [Configuration](configuration.md) lists them.

> **Stay on 3.11 or 3.12 for local work.** Newer Python versions accept syntax that the Docker image (which runs Python 3.11) will reject, so run `python3.11 -m compileall app` before you build an image. If you hit FTS5 database errors, switch to the Docker path, which bundles a compatible SQLite build.

---

## Step 2: Create your admin account

The first time you open the dashboard, you'll be prompted to register. This account becomes the admin: it can manage all agents, view the audit log, and access backup and restore. Keep that password somewhere safe.

After registering, you are in. **Needs you** is the home page. It lists what is waiting on you, and an empty list means there is nothing to do. A short **Getting started** list helps on a new install.

Below the list, a health strip shows whether your capabilities, webhooks and the local credential helper are working. If it says a capability is failing, [Troubleshooting](troubleshooting.md#the-health-strip-says-a-connection-is-failing) explains what that means.

---

## Step 3 (optional): Set up two-step sign-in

You can skip this and come back later. To enable login MFA, enroll a TOTP authenticator. It's a one-time setup.

Go to **Settings → Account**, find **Sign-in**, and scan the QR code with any authenticator app (Google Authenticator, Authy, 1Password, or anything else that supports TOTP). Confirm the first code to activate it. You can reset or disable OTP later from the same page.

You won't be prompted for it during normal use, only at login.

---

## Step 4: Create a workspace and your first agent

A **workspace** is a shared place where agents keep memory and tasks for one project. An **agent** is the identity a tool uses to sign in to Agent Core. Each agent has its own key and its own limits on what it can read and write.

1. Open **Agents**, then the **Workspaces** tab, and create a workspace such as `my-project`. In the same dialog you can add your project's folder (one per machine where the project lives, written as that machine names it) and tick the agents that can use it. A session started in that folder then gets the workspace's context automatically.
2. On the **Agents** tab, choose **+ Create Agent**. Give it an ID and a name that say what it is for, such as `coding-agent`. With one workspace, a new agent can read and write it by default.

Creating an agent does not show a key. You make the key in the next step.

### What an agent's settings mean

An agent's on and off switches are its **permissions**: Use memory, Coordinate tasks, Reference credentials, See capabilities, Run capabilities, Fetch secrets and Propose admin changes. The **Access** button on an agent's row opens one dialog about that agent: the workspaces it can read and write, the capabilities it can use, the credentials it can reference, its keys and its permissions. Adding or removing a workspace shows what it would open or close before you save. Agent Core never switches a permission on for an agent. If one is missing, the dialog offers a **Turn on** button, and a person presses it.

Not every entry on the Agents page is an agent. A **service** is a program or container that only fetches the secrets it is given. It has one permission, Fetch secrets, and no workspaces. Services are listed under **Credentials > Services**.

### Advanced: create an agent with the API

You act as a person with an **operator token**: a short-lived key you create in **Settings → Account → Operator tokens** while signed in (5 to 60 minutes, shown once). Scripts that create agents, grants or backups use it instead of your password. Replace `<operator-token>` below with it:

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

`read_scopes` and `write_scopes` say what the agent can see and write. A scope is a named area of memory: `agent:<id>` is the agent's own private area, `workspace:<id>` is a workspace, `user:<id>` is one person's. By default an agent can write only to its own area. The **Shared memory** option adds the `shared` scope, which every user and workspace can see. Give it only to an agent that should read or write for everyone. Writing to `shared` also needs the agent to be in `AGENT_CORE_SHARED_SCOPE_AGENTS` or to have `shared` in its write scopes.

---

## Step 5: Connect your tool

The **Connect** page (`http://localhost:3500/integrations`, also a tab on **Agents**) walks you through it. Choose your user, workspace, agent and tool. The page then shows, in order:

1. The setting to add to your tool. Choose **Generate One-Time Key** and confirm, and the key is filled in for you. The key is shown once, so copy it into your tool right away. If you lose it, generate another: an agent can hold several keys (for example one per machine), and a new key does not stop the others. Revoke a key you no longer use under **Agents → More → Keys**. Through the API, `POST /api/agents/coding-agent/keys` with `{"label": "laptop"}` returns a key once.
2. A `CLAUDE.md` or `AGENTS.md` file to save in your project. It tells the agent to use Agent Core. You can verify the connection before you save it.
3. For Claude Code and Codex, an optional hook that loads the workspace context when a session starts.

Do the setting and the hook yourself in a terminal. An agent usually cannot, because it has no network access and cannot write to its own settings folder.

Most tools take the setting as MCP (Model Context Protocol) settings. MCP is the standard way AI tools call outside programs, and Claude Code, Cursor, Claude Desktop and Codex support it:

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

To check from a terminal that the key works:

```bash
curl -X POST http://localhost:3500/mcp \
  -H "Authorization: Bearer <agent-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"tool": "memory_search", "params": {"query": "test", "limit": 5}}'
```

You should get an answer that starts like this (the other fields vary):

```json
{"ok": true, "data": {"records": [], "total": 0, ...}}
```

A `401` means the key is wrong. Use the agent's key, not your dashboard password. A connection error means Agent Core is not running on port 3500.

Then paste the **Verification Prompt** from the bottom of the Connect page into your tool. It asks the agent to write a note, find it again, check which credentials and capabilities it can see, and log a task.

---

## Step 6: Write and search memory

Once connected, an agent writes a memory with three things: a class (what kind of record it is), a scope (who can see it) and the content.

The class matters. A **fact** is something that can be checked against the code, a server or another system. A **decision** is something a person or team chose, and nothing can check it. Agent Core re-checks facts on a schedule and leaves decisions alone. What an agent just did belongs in the activity record, not in memory.

For a preference, add a `slot_key` to keep one active value per slot: a new preference with the same scope and slot replaces the old one. `valid_from` and `valid_to` say when the content was true in the world, which can differ from when it was written. Set them when a record covers a period other than "from now on".

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

You manage credentials on the **Credentials** page. A credential is the encrypted secret itself: an API key, token, password, URL or other value. A connection is separate. It tells Agent Core how to use a stored credential with a capability. A capability can come from an imported OpenAPI spec, a registered MCP server, the built-in Generic HTTP option or an installed adapter.

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

Each credential has one list of who can use it, in three groups: the capabilities that sign in with it, the agents that can reach it, and the services that receive its value. It opens from the credential's own page, under **Access**. An agent never receives the value; it receives a reference, and Agent Core fills that reference in when a tool runs.

You can show a stored value again from **Credentials > Edit > Show value**. It appears in the dialog with Hide and Copy, is hidden again after 30 seconds or when the dialog closes, and never appears in the table or in the page source. Each time it is shown, the person and the credential's name are recorded in the audit log, never the value.

See [Local credential helper](credential-broker.md) for how to use that reference with local tools so the real token gets injected without appearing in your prompt or config files.

## Step 8: Create a connection (optional)

If you want Agent Core to take actions on your behalf, like creating an issue, reading a repo, or calling an API, rather than just supplying a secret to a local tool, use the **Capabilities** page in the dashboard.

This is the server-side capability path. The agent asks for a capability, Agent Core executes it, and the result comes back through the same scoped channel.

The flow:

1. Import an OpenAPI spec for the capability you want, register a native MCP server, or use **Generic HTTP** for a one-off endpoint.
2. Go to **Capabilities** and pick that capability.
3. Select a stored credential, or create one inline while creating the connection.
4. Bind it to a scope like `workspace:<id>`.
5. Test the connection from the dashboard.
6. Call `connectors_run` from MCP to trigger actions. Agent Core uses the credential server-side and returns the result, whether the backing provider is OpenAPI or MCP.

A credential entered while adding a connection belongs to that connection: no agent can reference it, and only Agent Core uses it (a service can still be given it explicitly). Tick **Also let agents reference this credential** to store it under the connection's owner instead, as before.

Who may use a connection is a list of its own, under **Who can use it**: named agents, everyone in a workspace, or all of one person's agents, with an exclusion for a single agent. Every change shows who gains it and who loses it before it is saved. Being on that list lets an agent use the connection; it gives no memory access and no credential access.

The **Access** page under Credentials shows all of this in one list: who can reach what, when each was last used, and anything waiting on a person. Its **Unused** card lists credentials, capabilities and services that nothing uses, so you can tidy up. The [dashboard map](dashboard-map.md) describes each page.

Credential scope and connection scope are related but not identical. Credential scope controls who may access the stored secret. Connection scope controls where that capability is available. In the common case, use the same workspace scope for both. For advanced setups, one personal credential can power multiple workspace connections.

The key difference from the Local credential helper: with capabilities, Agent Core runs the action and the raw secret never leaves the server. With the helper, your local tool gets the real value injected at runtime. Use whichever fits your situation.

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
