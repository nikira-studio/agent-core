# Integrations

Agent Core exposes memory, credentials, connector actions, and activity tracking over **MCP** (Model Context Protocol) and **REST**. It doesn't schedule or orchestrate — it gives agents services to call when they need them.

The dashboard **Integrations** page at `/integrations` generates ready-to-paste configs for specific tools and is usually the fastest path. This doc explains what's happening under the hood and covers cases the generator doesn't handle. The current presets include Claude Code, Codex, Cursor, Windsurf, Antigravity, and a generic MCP/REST path.

The dashboard **Connectors** page at `/connectors` is where you register external capabilities for Agent Core itself. It supports:

- importing OpenAPI specs as connector types
- registering native MCP servers as connector types
- binding those capabilities to credentials and scopes

OpenAPI imports and MCP server registrations both become first-class connector types in the same catalog. The difference is only how the connector type was discovered and how Agent Core executes it later.

For operator-managed internal services on your own network, keep the default SSRF guard in place and opt in only through deployment config. Set `AGENT_CORE_ALLOWED_INTERNAL_HOSTS` for trusted hostnames like `firecrawl` or `searxng`, then use binding `config_json` overrides such as `{"base_url":"http://firecrawl:3002/v1","auth_mode":"none"}` when you need an imported OpenAPI spec to talk to that internal deployment.

---

## MCP

MCP is the native protocol for Claude Code, Cursor, Claude Desktop, and a growing list of other tools. When you connect via MCP, your agent can call tools like `memory_search` and `credential_get` directly from within a session, without writing any code.

**The MCP endpoint is at `/mcp`** and requires your agent API key.

Add it to any MCP-compatible client with this config:

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

### Antigravity

Antigravity uses the same MCP JSON shape, but the endpoint field should be `serverUrl` instead of `url`:

```json
{
  "mcpServers": {
    "agent-core": {
      "type": "http",
      "serverUrl": "http://localhost:3500/mcp",
      "headers": {
        "Authorization": "Bearer <agent-api-key>"
      }
    }
  }
}
```

### What MCP Gives Your Agents

Once connected, these tools are available in any session:

| Tool | What it does |
| --- | --- |
| `memory_search` | Search all memory the agent has access to, by natural language query |
| `memory_get` | List records in a specific scope |
| `memory_write` | Save a memory record (automatically checks for PII on shared scopes) |
| `memory_retract` | Soft-delete a memory record |
| `credential_get` | Get an `AC_SECRET_*` reference for a stored credential |
| `credential_list` | List credential entries the agent can access (metadata and references only — no raw values) |
| `activity_update` | Create or update an activity record, including progress notes and a completion result |
| `activity_get` | Get the status of an activity |
| `activity_list` | List activities visible to the current agent or user |
| `activity_pickup` | Claim the next active work item a human assigned to this agent in authorized scopes |
| `get_briefing` | Pull a briefing when taking over from another agent |
| `briefing_list` | List generated briefings visible to the current agent or user |
| `connectors_list` | List available connector types |
| `connectors_bindings_list` | List connector bindings in authorized scopes |
| `connectors_bindings_test` | Test a binding using the stored credential |
| `connectors_actions_list` | List actions supported by a connector type |
| `connectors_summary` | Summarize visible connector types, bindings, credentials, actions, and health state |
| `connectors_run` | Run one connector action server-side using a binding |

This is the pattern throughout Agent Core: agents connect, discover what they’re allowed to use, and call what they need. Agent Core provides the capabilities and logs the results — it doesn’t act as a workflow engine.

For memory writes, `slot_key` can make a preference deterministic by keeping one active value per slot, and `valid_from`, `valid_to`, and `last_confirmed_at` are optional freshness hints.

If one agent needs to hand work to another, write the durable state into the shared workspace scope and generate or link a briefing. If you are reviewing prior work, use `memory_search`, `activity_list`, and `briefing_list` together before changing anything. The private `agent:<id>` scope is only for scratch notes for that specific agent and should not be treated as the handoff channel.

### Scope Model

Agent Core uses a small set of scope types with different purposes:

- `agent:<id>` is the agent's private scope for scratch notes and agent-local state.
- `user:<id>` is the authenticated owner's personal scope. In the current UI, creator user read is automatic so agents can see owner-level preferences and user-scoped bindings, while user write remains an explicit advanced choice.
- `workspace:<id>` is the normal collaboration scope for shared project work and handoffs.
- `shared` / `global` is a cross-user shared access path, not just a visibility toggle.

When you generate reusable instructions, keep those distinctions in mind. For assistant-style agents, use the authenticated/default user scope when no workspace is selected, and include a workspace scope only when the user explicitly chose one. Treat user scope as read-only owner context unless user-scope write access is explicitly enabled; otherwise keep facts and decisions in the default shared scope, meaning the workspace scope when one is selected or the owner context when no workspace is selected.

## Connector Setup in Agent Core

Use **OpenAPI import** when the service already publishes a REST spec. Use **Import MCP Server** when the service is a native MCP server and you want Agent Core to keep the capability catalog, credentials, scopes, execution history, and audit trail in one place.

### OpenAPI import

- Go to `/connectors`
- Click `+ Import API Spec`
- Paste a URL, upload a file, or paste the JSON/YAML directly
- Click **Preview Spec** — this shows the connector name, auth type, base server URL, operation count, and any warnings before anything is saved
- Click **Import API** to commit the import
- Create a binding to a stored credential

When browsing the **API Directory**, specs with multiple variants (such as GitHub, which publishes separate specs for api.github.com, GHES, and GHEC) show a **View Variants** button instead of a direct Import. Open it, review the variants, and import the one that matches your deployment.

For internal deployments, the binding can override the target server with `config_json.base_url`. If the imported spec declares auth but the local service does not use it, set `config_json.auth_mode` to `none` so Agent Core does not require or inject a credential for that binding.

Example: for a local Firecrawl deployment, import the public spec from GitHub, then create a binding with:

```json
{
  "base_url": "http://firecrawl:3002/v1",
  "auth_mode": "none"
}
```

And set `AGENT_CORE_ALLOWED_INTERNAL_HOSTS=firecrawl` in the Agent Core environment. That keeps the imported spec public while routing execution to your trusted internal Firecrawl instance.

### HTTP Connector (+ Add HTTP Connector)

Use this when the service does not publish an OpenAPI spec and you just need to make authenticated HTTP calls to a known base URL. This creates a `generic_http` connector type — no spec import required.

- Go to `/connectors`
- Click `+ Add HTTP Connector`
- Enter a display name and base URL
- Choose an auth type:
  - **Bearer** — injects `Authorization: Bearer <credential>` (default; works for most REST APIs)
  - **Header** — injects the credential into a custom header name you specify
  - **Query** — appends the credential as a query parameter (e.g. `?api_key=...`)
  - **None** — no auth header (useful for internal services)
- Create a binding to a stored credential, then set the scope

#### Calling an HTTP connector from an agent

Agents call HTTP connectors with `connectors_run`. The action is a method and path pair: `METHOD /path`. Any params that are not transport keys (`method`, `path`, `url`, `headers`, `query`) are automatically bundled as the JSON request body.

```
connectors_run(
  binding_id = "<your-binding-id>",
  action     = "POST /chat/completions",
  params     = {
    "model": "google/gemini-3.1-flash-image-preview",
    "messages": [{"role": "user", "content": "A sunset over mountains"}],
    "modalities": ["image"]
  }
)
```

Agent Core injects the credential server-side, calls the external service, and returns the response. The raw API key never reaches the agent.

**Do not use `credential_get` for this.** HTTP connector bindings handle auth automatically. `credential_get` is for local tools that need to inject the secret themselves — it is not the right path when a connector binding exists.

#### Test connection behavior

The binding test in the dashboard hits the base URL directly (e.g. `GET https://openrouter.ai/api/v1`). Many REST APIs return HTML documentation pages or a 404 at their base URL, which the test reports as a failure. This is expected — it does not mean the binding is broken. Verify the binding works by running an actual action with `connectors_run` or `connectors_bindings_test` is reliable only for APIs that return a valid response at the base URL (like health check endpoints).

#### Example: OpenRouter

[OpenRouter](https://openrouter.ai) provides a unified OpenAI-compatible API for hundreds of models, including image generation, without publishing a public OpenAPI spec. The HTTP connector is the right path.

**Setup:**

1. Create a credential with your OpenRouter API key
2. Click `+ Add HTTP Connector` with:
   - Display name: `OpenRouter`
   - Base URL: `https://openrouter.ai/api/v1`
   - Auth type: `Bearer`
3. Create a binding linked to your OpenRouter credential, scoped to your workspace

**Text generation:**

```
connectors_run(
  binding_id = "<openrouter-binding-id>",
  action     = "POST /chat/completions",
  params     = {
    "model": "anthropic/claude-3.5-sonnet",
    "messages": [{"role": "user", "content": "Explain SSRF in one paragraph"}]
  }
)
```

**Image generation** (models that support it, e.g. `google/gemini-3.1-flash-image-preview`):

```
connectors_run(
  binding_id = "<openrouter-binding-id>",
  action     = "POST /chat/completions",
  params     = {
    "model": "google/gemini-3.1-flash-image-preview",
    "messages": [{"role": "user", "content": "A photorealistic sunset over mountains"}],
    "modalities": ["image"]
  }
)
```

Image responses return base64-encoded data in the `choices[0].message.content` array. The response body can be large — agents should extract the `data` field rather than logging the full response.

Check the [OpenRouter model list](https://openrouter.ai/models) for which models support image generation (`modalities: ["image"]`).

---

### MCP server import

- Go to `/connectors`
- Click `+ Import MCP Server`
- Enter the MCP endpoint URL, transport, and optional discovery headers
- Refresh later if the server's tool set changes
- Create a binding to a stored credential if the server needs one

The MCP import is server-side only: it discovers and stores the tool list in Agent Core, then agents call those tools through the same connector/binding execution path as OpenAPI-backed connectors.

#### HTTP transport only — stdio servers are not supported

Agent Core connects to MCP servers over HTTP. It cannot launch local processes. If your MCP client config uses `command` and `args` fields (stdio transport), that server cannot be imported directly. Options:

- Run the stdio server behind an HTTP bridge such as `mcp-proxy` and register the bridge URL
- Use **+ Add HTTP Connector** instead if the underlying service exposes a plain REST API

Common stdio-based servers that fall into this category: `playwright-mcp`, `chrome-devtools-mcp`, `sequential-thinking`, and most `npx`-launched servers.

#### Transport

- `streamable_http` — preferred for modern MCP servers (MCP spec 2025-03-26+). Use this for hosted services like Context7.
- `http` — alias; try this if `streamable_http` fails.
- Unsupported transports are rejected at import time.

#### Discovery auth vs. binding auth

These are two separate things that serve different purposes.

**Discovery auth** is used only during import and refresh to authenticate the `initialize` / `tools/list` calls. It is not stored anywhere after import. The import form has two fields: **Header name** and **Value** — enter the raw key value directly here (not a credential reference).

**Binding auth** is injected at execution time when an agent calls `connectors_run`. Configure it on the binding using a stored credential.

For servers using standard `Authorization: Bearer <token>`:
- Leave discovery auth blank — most Bearer servers allow unauthenticated tool discovery, or enter `Authorization` / `Bearer your-key` temporarily
- Store the token as a credential and create the binding normally; Bearer injection is automatic

For servers using a custom header name (e.g. `CONTEXT7_API_KEY`):
- In the discovery auth fields, enter the header name and raw key value
- After import, create a binding with the credential set to your key value and add this to the binding's config JSON:
  ```json
  { "auth_header": "CONTEXT7_API_KEY", "auth_scheme": "" }
  ```
  The empty `auth_scheme` tells Agent Core to inject the raw credential value without a `Bearer ` prefix.

MCP bindings can also carry `timeout_ms` in config JSON for slow servers. The endpoint URL must still pass the URL guard; register internal hostnames in `AGENT_CORE_ALLOWED_INTERNAL_HOSTS`.

#### Example: Context7

[Context7](https://context7.com) provides up-to-date library documentation for agents. It uses the MCP streamable HTTP transport with a custom `CONTEXT7_API_KEY` header.

**Setup:**

1. Create a credential with your Context7 API key
2. Click `+ Import MCP Server` with:
   - URL: `https://mcp.context7.com/mcp`
   - Transport: `streamable_http`
   - Discovery auth header: `CONTEXT7_API_KEY` / value: your raw key
3. Click **Import MCP Server** — Agent Core discovers the tool list
4. Click **Bind** on the Context7 connector type:
   - Select your Context7 credential
   - Config JSON: `{"auth_header": "CONTEXT7_API_KEY", "auth_scheme": ""}`
   - Scope: your workspace

**Using it from an agent:**

```
# Step 1 — resolve the library ID
connectors_run(
  binding_id = "<context7-binding-id>",
  action     = "resolve-library-id",
  params     = {"libraryName": "fastapi", "query": "fastapi"}
)
# → returns Context7-compatible library IDs like /fastapi/fastapi

# Step 2 — fetch docs
connectors_run(
  binding_id = "<context7-binding-id>",
  action     = "get-library-docs",
  params     = {"context7CompatibleLibraryID": "/fastapi/fastapi", "topic": "routing"}
)
```

Context7 responses include current code snippets with version information, drawn directly from official documentation sources.

Test that MCP is reachable:

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

To discover what Agent Core supports at runtime, any authenticated agent can call `GET /spec`:

```bash
curl http://localhost:3500/spec \
  -H "Authorization: Bearer <agent-api-key>"
```

Unauthenticated callers can use `/spec/public` for a minimal discovery response.

---

## Tool-Specific Setup

### Claude Code

**Option 1 — CLI (recommended):**

```bash
claude mcp add --transport http --scope user agent-core http://localhost:3500/mcp \
  --header "Authorization: Bearer YOUR_AGENT_API_KEY"
```

This adds Agent Core to your user-level config so it's available in every project.

**Option 2 — Project config file:**

Create `.mcp.json` in your repo root (this file can be committed and shared with your team):

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

> If you're committing `.mcp.json`, consider using an environment variable instead of a hardcoded key: `"Authorization": "Bearer ${AGENT_CORE_API_KEY}"`. Set the variable in your shell or `.env` before starting Claude Code.

**Verify the connection:**

```bash
curl -X POST http://localhost:3500/mcp \
  -H "Authorization: Bearer YOUR_AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tool": "memory_search", "params": {"query": "test", "limit": 5}}'
```

Expected: `{"ok": true, "data": {"records": [], "count": 0, "retrieval_mode": "fts_only"}}`

This curl test only confirms MCP reachability. For the full end-to-end verification flow, use the generated **Verification Prompt** and run it in the connected agent. That prompt writes a workspace memory record, reads it back, checks credential and connector visibility, and reports workspace-scoped and user-scoped bindings separately when both exist.

You can run that verification prompt immediately after wiring MCP. You do not need a `CLAUDE.md` or `AGENTS.md` file first; those files are only for persistent repository-level instructions when you want them.

**CLAUDE.md snippet:**

The **Integrations** page generates a full `CLAUDE.md` snippet tailored to your agent and workspace. Paste it into your repo's `CLAUDE.md` to give Claude Code context about what's available — which scopes to use, when to search memory, and how to handle credentials. Here's a minimal version to get started:

Some MCP hosts defer tool loading until the session explicitly discovers them. If a new session cannot see Agent Core tools right away, run the host's tool discovery or load step first, then retry the Agent Core call. That is a host behavior, not an Agent Core setup problem.

```markdown
## Agent Core

You are connected to Agent Core at http://localhost:3500.

- At startup or when idle, call `activity_pickup` to check for work a human has assigned to you. If it returns an activity, that is your current task. If it returns null, proceed with whatever the user is asking.
- Search memory at the start of each session: `memory_search` with a natural language query. If a broad query returns little or nothing, retry with exact topic values, exact words from prior records, or a known record id. When embeddings are unavailable, exact tokens and known ids are more reliable than conceptual searches.
- Store decisions, preferences, and facts: `memory_write`
- For credentials: use `credential_get` to retrieve an AC_SECRET_* reference — never ask the user for raw API keys
- Send `activity_update` heartbeats every 1–2 minutes while working on a task
- Use `task_note` for short in-flight progress updates
- If the session reloads, a handoff begins, or no active activity exists yet, open a fresh activity first with `status: active` before attempting to close it
- When finishing a task, include `status: completed` and a short `task_result` summary of what changed
```

---

### Claude Desktop

Edit (or create) the config file:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

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

Restart Claude Desktop after saving.

---

### Cursor

Create or edit the MCP config file:

- **Global (all projects):** `~/.cursor/mcp.json`
- **Workspace-level (this repo only):** `.cursor/mcp.json` in your project root

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

Restart Cursor after saving.

---

### Assistants

Use this section for assistant-style agents that manage their own MCP configuration. The agent should update its own config, reload or restart as supported, and verify Agent Core before doing work. Treat user scope as read-only owner context unless the agent is explicitly granted user-scope write access; otherwise keep preferences in the default shared scope and keep facts and decisions in workspace scope when a workspace is selected.
If you want a fresh bearer token for the generated prompt, use the one-time key button for this output the same way you would for MCP config or environment variables.
If a workspace is selected in the Integrations page, the generated prompt includes a workspace scope line. Otherwise it stays workspace-free and uses the authenticated/default user scope as the shared context.

```text
You are onboarding to Agent Core.

Agent Core is a local capability layer for:
- shared memory
- activity tracking
- handoffs and briefings
- credential references
- connector discovery and execution

Agent Core repository: https://github.com/nikira-studio/agent-core

Connection values:
- MCP URL: <AGENT_CORE_MCP_URL>
- Bearer token: <AGENT_CORE_API_KEY>

Task:
1. Use the connection values above to add Agent Core as an MCP server in your own configuration.
2. Verify the connection.
3. Use Agent Core tools as your durable shared backend.
4. Do not treat chat history as durable memory.

Setup instructions:
- Update your own MCP configuration using the connection values above.
- Register Agent Core under the appropriate MCP server entry for this tool.
- Write the final config using the connection values above.
- If the server already exists in config, update it rather than creating duplicates.
- After saving config, reload MCP or restart the agent as supported.
- Verify that the Agent Core server is visible before doing any Agent Core work.
- If the MCP server cannot be configured or verified, stop and ask for the missing value instead of guessing.

Scope guidance:
- Use the authenticated/default user scope as read-only owner context when you have user-scope read access.
- If user-scope write access is explicitly available, you may store stable user preferences there; otherwise keep preferences in the default shared scope, which means the workspace scope when selected or owner context when no workspace is selected.
- If a workspace was selected in the Integrations page, include that workspace scope for facts, decisions, and shared collaboration context.
- Use the agent's private scope only for temporary scratch notes.

When writing the config, use the MCP URL and bearer token from the connection values section above. Do not duplicate them elsewhere in the prompt.

Expected Agent Core tools:
- `memory_search`, `memory_get`, `memory_write`, `memory_retract`
- `activity_pickup`, `activity_update`, `activity_get`, `activity_list`
- `briefing_list`, `get_briefing`
- `credential_list`, `credential_get`
- `connectors_list`, `connectors_summary`, `connectors_bindings_list`, `connectors_actions_list`, `connectors_bindings_test`, `connectors_run`

Operating rules:
- At startup or when idle, call `activity_pickup` to check for work a human has assigned to you in this workspace. If it returns an activity, that is your current task — read it and start working. Only call pickup once per session start; do not loop infinitely claiming new tasks on your own.
- Start meaningful tasks with `activity_update` using `status: active`, a concise `task_description`, and the default shared scope.
- Refresh activity while actively working.
- Before making changes, search memory with `memory_search`.
- For handoffs or reviews, use `activity_list` and `briefing_list` first.
- Write durable facts and decisions to Agent Core memory with `memory_write`.
- Use `memory_retract` only when a record should no longer be active.
- Never store raw secrets in memory; use `credential_get` for `AC_SECRET_*` references instead.
- Use `credential_list` first when credentials may be needed.
- Use `connectors_list` and `connectors_bindings_list` to discover connectors.
- Use `connectors_actions_list` before unfamiliar connectors.
- Use `connectors_run` for server-side external actions.
- Use the workspace scope when available; otherwise use the authenticated/default user scope.
- Use full prefixed scope names exactly as provided by Agent Core. Do not invent scope names.

Behavioral goals:
- Treat Agent Core as the durable system of record.
- Treat chat history as temporary.
- Keep connector and credential usage minimal and intentional.

If the Agent Core MCP server cannot be added or verified, do not proceed with Agent Core-dependent work.
```

---

### Other MCP Hosts

Every MCP-compatible tool needs the same two things: the endpoint URL and the `Authorization` header. The JSON structure above works for any client that supports the `type: http` transport. Key names may vary slightly — check that tool's docs for where to put it.

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

---

## Generating Integration Files from the Dashboard

The **Integrations** page (`/integrations`) generates everything you need for a specific tool in one place:

- Ready-to-paste MCP JSON
- `CLAUDE.md` or `AGENTS.md` content with instructions for the agent
- Environment variable snippets
- Verification prompts to paste into the agent to confirm it's connected

Select your user, workspace, agent, and target tool. The generator has first-class presets for Claude Code, Codex, Cursor, Windsurf, Antigravity, and generic MCP/REST flows.

The Integrations page generates the canonical setup text and downloadable files. Each output type is available for every tool preset:

| Output type | What it contains |
| --- | --- |
| Instructions | Human-readable setup steps and scope guidance for the selected tool |
| MCP Config | Ready-to-paste JSON for the agent's MCP config file (`agent-core-mcp-config.txt`) |
| Environment Variables | `.env`-style variable snippet (`agent-core.env`) |
| CLAUDE.md | Repo-level instructions for Claude Code to paste into `CLAUDE.md` |
| AGENTS.md | Equivalent instructions for Codex or other `AGENTS.md`-aware tools |
| Assistants | Reusable onboarding instructions for assistant-style agents that update their own MCP config |
| Session Prompt | A startup prompt the agent can run at the beginning of each session |
| Verification Prompt | A one-time prompt that confirms the full end-to-end connection is working |

---

## Two Ways to Use Secrets

Agent Core gives you two distinct paths for using stored credentials. Pick the one that fits how the action happens:

**Credential Broker — your local tool needs the secret.** The agent gets an `AC_SECRET_*` reference, your local tool gets the real value injected at runtime, and the broker is what does the injection. The secret travels from Agent Core to your local process only, never through the model.

**Connectors — Agent Core runs the action for you.** The agent tells Agent Core which binding to use and what action to run. Agent Core uses the stored credential server-side, calls the external service, and returns the result. The raw secret never leaves Agent Core at all.

### Credential Broker Flow

1. The agent calls `credential_get` (MCP) or `POST /api/credentials/entries/{id}/reference` (REST).
2. Agent Core returns an `AC_SECRET_*` reference — not the actual token.
3. The agent includes that reference in the tool configuration.
4. When the tool runs, the local Credential Broker intercepts the reference and injects the real value into the tool's environment.

See [Credential Broker](credential-broker.md) for setup instructions.

### Connectors: Agent Core Runs Actions Directly

If you want Agent Core itself to call an imported OpenAPI spec or another service on your behalf, use the **Connectors** page at `/connectors`.

The current flow is:

1. Import an OpenAPI spec for the service you want, or use the built-in `generic_http` connector for a quick one-off endpoint.
2. Create or pick a stored credential. You can create credentials directly on the Connectors page or inline while creating a binding.
3. Create a connector binding for the imported connector type.
4. Bind that connector to a scope like `workspace:<id>`, `user:<id>`, or `shared`.
5. Test the binding from the dashboard.
6. Run actions through MCP with `connectors_run`.

Connector types are instance-wide catalog entries. The built-in `generic_http` type is always available, and any imported spec becomes visible to other authenticated users in the same Agent Core instance.

This is the clearest example of the capability-layer model. The connector catalog is a service directory, and each action is a server-side capability that the agent can call when it needs that external system.

The agent sends a binding ID, an action name, and parameters. Agent Core resolves the credential server-side, calls the external service, logs the execution, and returns the result. The raw secret never reaches the agent.

Credential scope and binding scope are both intentional:

- **Credential scope** controls who can access the stored secret.
- **Binding scope** controls where the connector is available to agents.

For normal workspace use, set both to the same workspace. For advanced use, a credential in a user scope can power multiple workspace bindings if the acting agent has access to both.

When you want to distinguish personal bindings from workspace bindings, call `connectors_bindings_list` once with no scope filter and again with an explicit `scope` such as `user:<id>` or `workspace:<id>`.

Each binding currently links to one credential. Connector-specific non-secret settings, such as a default repo, base URL, auth header name, or query parameter name, belong in the binding config JSON.

### Workspace Collaboration

If a workspace is shared with multiple users, each user can still keep their own agents. The workspace owner or an admin grants the users collaborator access on the workspace record, and those users can then scope their own agents to `workspace:<id>`.

This is the recommended setup for team use:

1. Share the workspace with the users who need it.
2. Pick or create each user's own agent.
3. Give that agent `workspace:<id>` access only after the user can see the workspace.

That keeps agent ownership, workspace access, and connector binding scope aligned without making users share one agent identity.

If you're importing a spec and using a PAT or other bearer token, the flow is simple: import the spec, create a credential, bind the credential, then call `connectors_run` with the action you want. The agent never needs the raw token, and you never need to paste it into a prompt.

### The Right Mental Model

Think of Agent Core less like an orchestrator and more like a building of services:

- **Memory** is the shared reference room.
- **Credentials** are the secured service keys.
- **Connectors** are the service counters agents can walk up to.
- **Activity** is the live status board and handoff mailroom.

Agents decide when to use a service. Agent Core makes the service available, enforces scope, and logs what happened. It does not schedule work for them.

---

## Activity Tracking

Agents can report what they're working on so you can see live status in the dashboard. This is especially useful when multiple agents are working in parallel, or when one agent leaves work for another agent in the same workspace.

The dashboard receives activity updates in real time over a server-sent event stream (`GET /api/events`). When an agent creates, updates, cancels, or heartbeats an activity, the overview stat cards (Open Activities, Stale / Blocked) and the recent activity table refresh automatically without any page reload. The activity page shows a "refresh" banner when new events arrive. Connector execution completions also surface as a brief indicator on the Connectors page. No polling is required — the browser opens one persistent connection per session and reconnects automatically on disconnect.

```bash
# Create an activity when you start a task
curl -X POST http://localhost:3500/api/activity \
  -H "Authorization: Bearer <agent-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "task_description": "Refactoring memory search tests",
    "memory_scope": "agent:coding-agent"
  }'

# Send a heartbeat periodically (every 60–120 seconds is fine)
curl -X POST http://localhost:3500/api/activity/<activity-id>/heartbeat \
  -H "Authorization: Bearer <agent-api-key>"
```

If an agent misses heartbeats for more than `AGENT_CORE_STALE_THRESHOLD_MINUTES` (default: 5 minutes), its activity is automatically marked `stale`. The dashboard surfaces these with options to resume, reassign to another agent, generate a briefing, or cancel.

If you want another agent to pick up the work, tell that agent to check for assigned work in its current workspace. The pickup step is explicit and deliberate: Agent Core stores the work record and the agent claims it when it checks.

## Assigning Work to Agents

A human can assign a task to a specific agent from the **Activity** page in the dashboard (click **+ Assign Work**). The form asks for the target agent, the task description, and the workspace scope. The activity is created immediately; the agent session discovers it on the next pickup check.

### The pickup convention

Agent sessions do not wake up automatically. The pickup is an explicit pull:

1. The human creates an activity in the dashboard, assigns it to an agent, and sets the workspace scope.
2. The agent session calls `activity_pickup` at startup or when idle.
3. If a matching activity exists (same `assigned_agent_id`, same readable workspace scope), it is returned and heartbeated.
4. The agent reads the task, starts working, and sends heartbeats via `activity_update`, using `task_note` for interim progress updates.
5. If no active activity exists yet after a reload or handoff, open one with `status: active` before closing it.
6. When done, the agent marks the activity `completed` with a short `task_result` summary, or `blocked` if it cannot finish.

**Pickup is workspace-aware.** An agent session only sees activities whose `memory_scope` is within its authorized read scopes. An agent configured for `workspace:project-a` cannot claim a task scoped to `workspace:project-b`, and it cannot claim tasks assigned to a different agent.

**Pickup does not recurse.** The agent should call pickup once per session start (or when explicitly asked to check for work). It should not loop infinitely calling pickup and claiming new tasks on its own — that would make it an orchestrator, which is out of scope for v1.

### How to trigger pickup

Via MCP:
```
Call activity_pickup with no parameters.
If it returns an activity, that is your assigned task. Read the task_description and start working.
If it returns null, there is no assigned work waiting.
```

Via REST:
```bash
curl -X POST http://localhost:3500/api/activity/pickup \
  -H "Authorization: Bearer <agent-api-key>"
```

### Workspace-scoped assignment example

1. Create a workspace `project-a` in Agent Core.
2. Create an agent `build-agent` with `workspace:project-a` in its read and write scopes.
3. In the dashboard, assign a task to `build-agent` and set the scope to `workspace:project-a`.
4. In the `build-agent` session, call `activity_pickup`. It returns the task.
5. A session for a different agent (or the same agent but with a different workspace) gets `null` — it cannot see or claim the task.

---

## Takeover Workflow

If one agent runs out of tokens, hits a weekly limit, or otherwise needs to stop before finishing, the next agent can continue from Agent Core state instead of starting blind.

The practical flow is:

1. The current agent keeps its activity record up to date with `activity_update` heartbeats.
2. It writes durable decisions, facts, and handoff notes to memory when something should survive beyond the current session.
3. When work stops, the next agent reads the latest activity, relevant memory, and any generated briefing.
4. If an activity is stale or being handed off intentionally, generate a briefing with `/api/briefings/handoff` or `get_briefing`. Briefings are on-demand task transfer artifacts, not something the system scheduler produces automatically.

This isn't automatic orchestration — it's a durable handoff trail and mailroom. A different agent picks up where the last one stopped, with full context, instead of starting blind.

If work needs to cross users or workspaces, make that explicit in the activity scope and briefing trail. The safest pattern is: leave the task in the correct workspace, have the receiving agent check for assigned work in that workspace, then claim it. If a broad memory search returns nothing, retry with exact topic values, specific words from prior records, or a known record ID — conceptual queries can miss when embeddings aren't available.

---

## Handing Work to Another Agent

When one agent needs to pass work to another — switching tools, handing off a task, or escalating — you can generate a briefing that gives the incoming agent immediate context:

```bash
curl -X POST http://localhost:3500/api/briefings/handoff \
  -H "Authorization: Bearer <agent-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"activity_id": "<activity-id>"}'
```

The briefing includes the current task description, any recorded task result, recent decisions, key facts, and relevant memory pulled from the activity's scope. The incoming agent can call `get_briefing` via MCP to pull this as part of its startup.

---

## REST Integration

If your tool doesn't support MCP, every feature is also available through REST using the same agent API key.

---

## Webhooks

Agent Core has two webhook features that work in opposite directions:

| Direction | What it does |
| --- | --- |
| **Inbound receiver** | External systems push work commands *into* Agent Core (`POST /api/webhooks/inbound`) |
| **Outbound notifications** | Agent Core pushes event notifications *out* to external endpoints (per-registered URL) |

Both are managed from the **Webhooks** page in the dashboard (admin-only).

---

## Inbound Webhook Receiver

External systems (n8n, Zapier, custom scripts, CI pipelines) can create and manage activities in Agent Core without using MCP by sending authenticated HTTP commands to the inbound endpoint.

### Setup

1. In the dashboard, go to **Webhooks** → **Inbound Receiver** and click **Generate Key**.
2. Copy the key — it is shown once, then hashed.
3. Use the inbound URL shown (`/api/webhooks/inbound`) and pass the key in the `X-Agent-Core-Inbound-Key` header.

```http
POST http://localhost:3500/api/webhooks/inbound
X-Agent-Core-Inbound-Key: ac_inbound_<your-key>
Content-Type: application/json

{
  "event_type": "activity.create",
  "assigned_agent_id": "codex",
  "task_description": "Review PR #142",
  "memory_scope": "workspace:agent-core"
}
```

### Supported commands

Commands use dot notation and imperative form. They are not the same as outbound event types.

| Command | Effect |
| --- | --- |
| `activity.create` | Create a new activity, assign it to an agent |
| `activity.assign` | Reassign an existing activity to a different agent |
| `activity.update` | Update status, description, note, result, or scope metadata of an existing activity |
| `activity.cancel` | Cancel an existing activity |
| `activity.note` | Append an append-only note to the activity's audit trail |

`activity.assign`, `activity.update`, `activity.cancel`, and `activity.note` all require `activity_id`.

`activity.note` writes only to the audit log — it does not modify the activity record or agent memory.
If `workspace` is supplied on `activity.create`, Agent Core stores it as optional metadata on the activity record for display and downstream automation.

### Key rotation

To rotate the key: click **Rotate Key** in the dashboard. The old key stops working immediately. The new key is shown once.

### Common pattern: push work from an external pipeline

```json
{
  "event_type": "activity.create",
  "assigned_agent_id": "codex",
  "task_description": "Investigate build failure in CI run #1234",
  "workspace": "workspace:eng"
}
```

Then later, close the loop:

```json
{
  "event_type": "activity.note",
  "activity_id": "<returned activity_id>",
  "note": "Build failure was a flaky test. Marked as fixed in PR #135."
}
```

---

## Outbound Webhook Notifications

Agent Core can push signed HTTP notifications to external systems when events occur. This lets automation tools like n8n, Zapier, or custom services react to activity changes or connector executions without polling.

Webhooks are **admin-only** and managed from the **Webhooks** page in the dashboard or via the REST API (`/api/webhooks`).

### What events are available

| Event | Fires when |
| --- | --- |
| `activity_created` | A new activity is created |
| `activity_updated` | An activity's status, metadata, progress note, or task result changes |
| `activity_heartbeat` | An agent sends a heartbeat on an active task |
| `activity_cancelled` | An activity is cancelled |
| `activity_recovered` | An activity is recovered (reassigned, resumed, closed) |
| `connector_executed` | A connector binding action completes |

### Payload shape

Every delivery is an HTTP POST with `Content-Type: application/json`. The envelope is consistent across all event types:

```json
{
  "event_type": "activity_cancelled",
  "timestamp": "2026-05-18T10:30:00.000000+00:00",
  "data": { ... }
}
```

Activity events (`activity_created`, `activity_updated`, `activity_heartbeat`, `activity_cancelled`, `activity_recovered`) include the full activity context so receivers can act without a follow-up lookup:

```json
{
  "activity_id": "abc123",
  "task_description": "Refactor auth middleware",
  "task_note": "Applied the middleware change and started the test pass",
  "task_result": "Completed auth middleware refactor and added tests",
  "agent_id": "my-agent",
  "assigned_agent_id": "my-agent",
  "user_id": "admin",
  "memory_scope": "workspace:my-project",
  "status": "cancelled",
  "started_at": "2026-05-18T09:00:00+00:00",
  "updated_at": "2026-05-18T10:30:00+00:00",
  "heartbeat_at": "2026-05-18T10:25:00+00:00",
  "ended_at": "2026-05-18T10:30:00+00:00",
  "previous_status": "active"
}
```

`task_note` is optional and is typically populated for in-flight updates. `task_result` is optional and is typically populated when a task is completed. `previous_status` is present on `activity_updated` and `activity_cancelled`. `recovery_action` and `result` are present on `activity_recovered`.

Connector events (`connector_executed`) include binding identity, scope, and result:

```json
{
  "binding_id": "bind123",
  "binding_name": "GitHub Actions",
  "scope": "workspace:my-project",
  "connector_type_id": "github",
  "connector_type_name": "GitHub",
  "action": "list_repos",
  "success": true,
  "duration_ms": 312,
  "status": "success",
  "error_message": null
}
```

### Signature verification

Every delivery includes `X-Agent-Core-Signature: sha256=<hex>`. Compute HMAC-SHA256 of the raw request body using your webhook secret and compare:

```python
import hashlib, hmac

def verify(body: bytes, secret: str, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
```

### v1 behavior

- **Fire-and-log only.** No retries. If a delivery fails, it is recorded in the delivery log.
- **5-second timeout.** Receivers should respond quickly; slow endpoints will time out.
- Deliveries are non-blocking — they do not delay the API response that triggered the event.
- The delivery log is visible per-webhook in the dashboard under **Deliveries**.

### Common outbound automation pattern

Push event → external automation decides what to do next. Agent Core does not schedule or orchestrate — it only notifies. Example n8n flow:

1. Register a webhook in Agent Core pointing to your n8n webhook URL
2. Subscribe to `activity_cancelled`
3. n8n receives the payload and runs a workflow (send Slack alert, create ticket, etc.)

To hand work *back into* Agent Core from n8n, use the inbound receiver described above.

---

A quick Python example:

```python
import httpx

BASE_URL = "http://localhost:3500"
API_KEY = "your-agent-api-key"
headers = {"Authorization": f"Bearer {API_KEY}"}

# Write a memory record
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
entries = httpx.get(f"{BASE_URL}/api/credentials/entries?scope=user:admin", headers=headers).json()
entry_id = entries["data"]["entries"][0]["id"]
ref = httpx.post(f"{BASE_URL}/api/credentials/entries/{entry_id}/reference", headers=headers).json()
# → {"ok": true, "data": {"variable_name": "AC_SECRET_SERVICE_TOKEN_1A2B3C4D"}}
```

See [API Reference](api.md) for the full endpoint documentation.
