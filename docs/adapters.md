# Adapters

Adapters are the unified way to add **any** external service to Agent Core. One manifest file describes what the service is, what credentials it needs, what actions it exposes, and how Agent Core should call it — Agent Core's built-in engines do the rest. No Python is added to your installation, adapters survive upgrades, and the same adapter file works on any other person's Agent Core when you share it.

This guide has two parts:

- **[Part 1: Using adapters](#part-1-using-adapters)** — installing, binding credentials, calling actions
- **[Part 2: Building adapters](#part-2-building-adapters)** — the manifest spec, the three backends, templating, auth/refresh/sessions, testing, sharing

If you just want to enable Gmail or Transmission, Part 1 is enough. If you want to add a service that isn't shipped, Part 2 walks through everything.

---

## When to use an adapter

Use an adapter when you want to add a service that **doesn't fit the simpler paths**:

| Path | When |
|---|---|
| **Import API Spec** (OpenAPI) | the service publishes a clean REST spec |
| **Import MCP Server** | the service is already a native MCP server |
| **Add HTTP Connector** | one-off authenticated HTTP, no per-action schemas needed |
| **Adapter** | session handshakes, OAuth refresh, multi-field credentials, CLI tools, named actions with schemas, anything you want to share with others |

Adapters cost a bit more upfront (you write a manifest) and give you a lot more in return: per-action input schemas the agent can introspect, automatic OAuth refresh, session-handshake retry, request templating, response extraction, and a shareable artifact that survives upgrades.

---

# Part 1: Using adapters

## Where adapters live

Agent Core has a **two-tier adapter library**:

| Library | Path | Who owns it | Survives upgrade? |
|---|---|---|---|
| **System** | `app/adapter_templates/<id>/adapter.json` | shipped with Agent Core, maintained by the project | replaced on each upgrade — it *is* the product |
| **User** | `data/adapters/<id>/adapter.json` | adapters you installed or wrote yourself | **yes**, lives in the data dir alongside the DB |

The Browse Adapters page at **`/connectors/adapters`** lists adapters from both libraries. Installing a system adapter copies it into the user library, so any local edits you make persist across upgrades and you keep the version you tested. The `adapter_installations` DB table is the source of truth for *which* adapters are active in the connector catalog; a restart re-seeds them from there.

## Install an adapter (3 ways)

### 1. Browse Adapters page (easiest)

1. Go to **`/connectors`** → click **Browse Adapters** (or visit `/connectors/adapters` directly).
2. Pick an adapter from the list. Each entry shows its description, version, backend (`http`/`mcp`/`cli`), and whether its `requires` (binaries, env vars) are satisfied.
3. Click **Install**. If it's a system adapter, Agent Core copies it from `app/adapter_templates/` into `data/adapters/`, seeds the connector type, and records the installation. If it's already in `data/adapters/`, the install just seeds and records.
4. The new connector type now shows in your `/connectors` catalog.

For `mcp` and `cli` adapters, a **dangerous-pattern scan** runs first and surfaces anything suspicious in the manifest before enabling. `http` adapters are pure data and don't need scanning.

### 2. Drop in a file

For an adapter you have on disk (or want to author locally):

1. Create `data/adapters/<adapter-id>/adapter.json`.
2. Restart Agent Core (or hit the rescan endpoint), or click **Install** on it from the Browse Adapters page.

Invalid manifests are logged and skipped — they never crash startup. The list of valid manifests appears on the Browse page automatically.

### 3. Install from a git URL

For adapters someone has published in a git repo:

```
git:owner/repo@ref
```

Agent Core clones the repo, validates the manifest, runs the dangerous-pattern scan (for code-bearing backends), and copies it into `data/adapters/`. The `@ref` (branch, tag, or commit SHA) is optional.

## Bind a credential and call actions

Adapters use the same binding model as every other connector type:

1. **Store the credential.** From `/credentials`, store the secret(s) the adapter needs. For multi-field credentials (OAuth, basic auth with username+password), paste the value as a JSON object — see the adapter's `credential_schema` for the required fields.
2. **Create a binding.** From `/connectors`, find the adapter's connector type, create a binding scoped to your workspace, and link your stored credential. Add any per-binding config the adapter requires (`requires.config` lists what's needed).
3. **Call actions from an agent** using `connectors_run`:

   ```
   connectors_run(
     binding_id = "<your-binding-id>",
     action     = "list_torrents",
     params     = { "ids": [42, 43] }
   )
   ```

Per-action input schemas are exposed via `connectors_actions_list`, so the agent can introspect what each action accepts. The raw credential is resolved server-side and never reaches the agent.

## Uninstall an adapter

From the Browse Adapters page, click **Uninstall** on an installed adapter. This:

- Removes the adapter's connector type from the catalog (its bindings become orphaned).
- Clears the install record.
- **Leaves the file in `data/adapters/`** so you can reinstall later with one click — uninstall is non-destructive of user data.

System adapters can be reinstalled from the system library; user-only adapters stay in `data/adapters/` and reinstall from there.

## The requires gating model

Manifests can declare what they need to function via the `requires` block:

| Key | What it gates | Behavior when unmet |
|---|---|---|
| `requires.bins` | local binaries (e.g. `gh`, `rclone`) | adapter shown as *unavailable*, install blocked |
| `requires.env` | environment variables | adapter shown as *unavailable*, install blocked |
| `requires.config` | per-binding config fields | adapter is installable; the binding itself fails at execution if the field is missing |

`bins` and `env` are **operator-level gates** (something the operator must provision before Agent Core can run the adapter at all). `config` is **per-binding** (the adapter is fine; each binding just needs the right config). This is why an adapter that says `requires: { config: ["base_url"] }` still appears available — you configure it when you create the binding.

---

# Part 2: Building adapters

A minimal adapter is a single `adapter.json` file. The structure has two parts: an **envelope** (identity, credentials, actions) and a **backend block** (how Agent Core executes those actions).

## The complete envelope

```json
{
  "spec_version": "1.0",
  "id": "my_service",
  "display_name": "My Service",
  "version": "1.0.0",
  "author": "your name",
  "description": "One-line summary for the catalog and Browse page.",
  "credential_schema": {
    "fields": [
      { "name": "api_key", "type": "string", "secret": true, "required": true }
    ]
  },
  "requires": {
    "bins": [],
    "env": [],
    "config": ["base_url"]
  },
  "actions": [
    {
      "name": "list_items",
      "description": "List items the agent can act on.",
      "side_effect": "read",
      "input_schema": {
        "type": "object",
        "properties": {
          "limit": { "type": "integer", "description": "Max items to return." }
        }
      }
    }
  ],
  "backend": {
    "type": "http",
    "...": "see backend-specific sections below"
  }
}
```

**Required fields:** `spec_version` (currently `"1.0"`), `id` (lowercase, `[a-z0-9_]+`), `version` (semver `X.Y.Z`), `backend`.

**`actions[]` shape** — each action takes:
- `name` — the action key callers pass to `connectors_run`.
- `description` — agent-oriented; this is what the agent sees when discovering tools. Write it for the agent, not the developer.
- `side_effect` — `"read"`, `"write"`, or `"destructive"`. Surfaces to the agent so it can confirm before destructive operations.
- `input_schema` — real JSON Schema for the params. Lets the agent know what to pass without trial and error.

**`credential_schema.fields[]`** — each field declares `name`, `type`, `secret` (true = redacted in UI/logs), `required`. The UI uses this to build the credential entry form. For single-secret connectors (one `api_key` field), the stored value is the secret string. For multi-field credentials, the stored value is a JSON object with those field names as keys.

## The three backends

Agent Core's built-in engines interpret the `backend` block. Pick one based on what your service needs:

| Backend | When to use | Code? |
|---|---|---|
| **`http`** | the service is HTTP, you can describe its requests declaratively | no — pure data |
| **`mcp`** | the service is (or has) a native MCP server you already run | external server, out-of-process |
| **`cli`** | the service has a local CLI you want to drive (e.g. `gh`, `rclone`) | external binary, out-of-process |

In-process Python is never required.

## `http` backend

The biggest and most-used backend. Describes HTTP requests declaratively with templates.

### Minimal `http` example (Stripe-style, API key, bearer)

```json
"backend": {
  "type": "http",
  "base_url": "https://api.stripe.com",
  "auth": {
    "type": "bearer",
    "apply": {
      "target": "request_header",
      "name": "Authorization",
      "template": "Bearer {{ cred.raw }}"
    }
  },
  "requests": {
    "list_customers": {
      "method": "GET",
      "path": "/v1/customers",
      "query_params": {
        "limit": "{{ params.limit | default(10, as=int) }}"
      },
      "response": {
        "success_when": "$.status >= 200 and $.status < 300",
        "extract": "$.data"
      }
    }
  }
}
```

### `base_url`

Either a hard-coded string (`"https://api.stripe.com"`) or a config-driven object so each binding can supply it:

```json
"base_url": { "from": "config", "field": "base_url" }
```

If you use the config form, list the field in `requires.config` so each binding remembers to provide it.

### `auth`

Six common shapes:

| `auth.type` | Credential is | Example `apply` |
|---|---|---|
| `none` | (none) | omit `apply` |
| `api_key` / `bearer` | single secret string in `cred.raw` | `"template": "Bearer {{ cred.raw }}"` in `request_header: Authorization` |
| `basic` | JSON `{username, password}` in `cred.fields` | `"template": "Basic {{ cred.base64_credentials }}"` (built-in helper that base64-encodes `username:password`) |
| `oauth2` | JSON `{client_id, client_secret, refresh_token, access_token, expires_at}` | `"template": "Bearer {{ cred.access_token }}"` — pair with a `refresh` block |
| `custom_header` | single token | `"name": "X-API-Token", "template": "{{ cred.raw }}"` |
| (anything else) | adapter-internal | the engine just resolves the template — describe whatever wire shape you need |

The `apply` block has `target` (`request_header`, `query`, or `body`), `name`, and `template`. You can apply auth multiple ways if needed (most services pick one).

### `session` (challenge-retry handshake)

For services like Transmission that demand a session ID after a 409, declare a `session` block. The engine handles the handshake internally per request — the agent never sees it:

```json
"session": {
  "type": "challenge_retry",
  "trigger": { "http_status": 409 },
  "capture": {
    "source": "response_header",
    "name": "X-Transmission-Session-Id",
    "as": "session_id"
  },
  "apply": {
    "target": "request_header",
    "name": "X-Transmission-Session-Id",
    "from": "session_id"
  },
  "max_retries": 1
}
```

On a triggering response, the engine captures the value from the source, re-issues the request with it applied, up to `max_retries` times. Session tokens are cached per-binding for subsequent calls.

### `refresh` (OAuth2)

For services with access-token expiry, declare a `refresh` block. The engine refreshes under a per-binding lock (concurrent calls won't double-refresh) and retries the action once after the new token lands:

```json
"refresh": {
  "trigger": {
    "http_status": 401,
    "or_expired": "cred.expires_at"
  },
  "token_url": "https://oauth2.googleapis.com/token",
  "grant": "refresh_token",
  "response_map": {
    "access_token": "access_token",
    "expires_in": "expires_in"
  },
  "persist": {
    "refresh_token": "credential_if_present.refresh_token"
  }
}
```

- `trigger` — what fires the refresh. `http_status` is reactive; `or_expired` is proactive (checks the named credential field against the current time before sending).
- `token_url` + `grant` — where to POST and which OAuth grant type to use.
- `response_map` — which response fields land where in the session.
- `persist.refresh_token` — if the provider rotates the refresh token, persist the new one to the credential automatically. (Gmail does this; many others do not.)

### `requests`

One entry per declared action. Each describes a single HTTP call:

```json
"send_message": {
  "method": "POST",
  "path": "/v1/chat/{{ params.channel_id }}",
  "query_params": {
    "thread_ts": "{{ params.thread_ts | default('', as=str) }}"
  },
  "body": {
    "template": {
      "text": "{{ params.text }}",
      "blocks": "{{ params.blocks }}"
    }
  },
  "response": {
    "success_when": "$.body.ok == true",
    "extract": "$.body.ts"
  }
}
```

- `method` — HTTP verb.
- `path` — appended to `base_url`. Supports `{{ params.x }}` interpolation.
- `query_params` — map of param name to template.
- `body.template` — object or string. Renders with the templating engine; non-string values (`True`, `42`, arrays) round-trip with their type preserved, so `"limit": "{{ params.limit | default(10, as=int) }}"` produces a JSON integer, not the string `"10"`.
- `response.success_when` — a jsonpath-ish expression evaluated on the response. Default: `2xx` status.
- `response.extract` — jsonpath to pull the useful part out (`$.data`, `$.results.items`, etc.).

## `cli` backend

For wrapping a local CLI tool. The engine runs the binary as a subprocess with `shell=False` (no shell injection) and parses its output.

```json
"backend": {
  "type": "cli",
  "bin": "gh",
  "timeout": 30,
  "env": {
    "GH_TOKEN": "{{ cred.raw }}"
  },
  "commands": {
    "list_repos": {
      "args": [
        "repo", "list",
        "--json", "name,url,visibility",
        "--limit", "{{ params.limit | default(30, as=int) }}"
      ],
      "parse": { "type": "jsonpath", "path": "$" }
    },
    "create_issue": {
      "args": [
        "issue", "create",
        "--repo", "{{ params.repo }}",
        "--title", "{{ params.title }}",
        "--body", "{{ params.body | default('', as=str) }}"
      ],
      "parse": { "type": "text" }
    }
  }
}
```

- `bin` — the binary name. Declare it in `requires.bins` so the adapter is gated on the binary being installed.
- `timeout` — seconds; the engine kills the subprocess and returns a `TIMEOUT` error if exceeded.
- `env` — environment variables to inject (templated). Use this for tokens; never pass secrets via argv.
- `commands.<action>.args` — array of arguments, each templated. Arrays prevent shell injection.
- `parse.type` — `"jsonpath"` (parse stdout as JSON, extract by path), `"regex"` (with `pattern`), or `"text"` (return raw stdout).

Non-zero exit code = error (stderr is included in the error message). Exit 0 with malformed JSON for `parse.type: "jsonpath"` returns the raw text.

## `mcp` backend

For pointing at an MCP server you already run. The adapter is a thin envelope; Agent Core dispatches to the MCP server through its existing MCP execution path.

```json
"backend": {
  "type": "mcp",
  "endpoint_url": "https://mcp.example.com/mcp",
  "transport": "streamable_http"
}
```

This is the lightest-weight way to register an MCP server as a named connector in your catalog. Use it when you want to give an MCP server a stable name, scope, credential, or per-action description in the catalog without re-importing it manually.

## Templating reference

All templates use the same locked-down resolver — no `eval`, no raw Jinja, no shell expansion. Three namespaces and a small set of filters:

### Namespaces

| Namespace | What you get |
|---|---|
| `{{ params.X }}` | the value the caller passed for action param `X` |
| `{{ cred.X }}` | the credential's field `X` (or `cred.raw` for single-secret credentials) |
| `{{ config.X }}` | the binding's `config_json` field `X` |

Bareword namespace access (`{{ params | filter }}`) returns the whole object, useful for filters that consume the full param dict.

### Filters

- **`default(<value>[, as=<type>])`** — supply a fallback when the resolved value is empty/None. The `as=` cast (`int`, `str`, `bool`, `list`) preserves type round-tripping. Example: `"{{ params.limit | default(10, as=int) }}"`.
- **Built-in cred helpers** — `{{ cred.base64_credentials }}` (base64-encodes `username:password` from `cred.fields`).

### Templating in non-string contexts

For body templates that produce typed JSON (booleans, integers, lists), the engine preserves the type via JSON round-trip:

```json
"body": { "template": {
  "active": "{{ params.active | default(false, as=bool) }}",
  "tags":   "{{ params.tags | default([], as=list) }}",
  "count":  "{{ params.count | default(0, as=int) }}"
}}
```

After rendering, `active` is a JSON boolean, not the string `"true"`.

## Testing your adapter

Wire-level tests are the single most valuable thing you can write. They mock the transport layer (HTTP or subprocess) and assert the **actual** rendered request matches what the service expects.

For an `http` adapter:

```python
class FakeHttpEngine:
    """Capture engine._send calls and return canned responses."""
    pass

def test_my_adapter_renders_correct_request(self):
    from app.connectors.http_engine import HttpEngine

    engine = HttpEngine({"backend_json": json.dumps({...your backend block...})})
    captured = []
    engine._send = lambda req, config: (captured.append(req) or _fake_200())

    engine.execute(
        "my_action",
        {"param_x": "value"},
        Credential(raw="secret"),
        '{"base_url": "https://api.example.com"}',
        session=None,
    )

    assert captured[0]["method"] == "POST"
    assert "api.example.com/v1/things" in captured[0]["url"]
    assert captured[0]["headers"]["Authorization"] == "Bearer secret"
    assert captured[0]["body"]["param_x"] == "value"
```

For a `cli` adapter: patch `subprocess.run`, capture the `args` list, assert the rendered argv matches.

See `tests/integration/test_transmission_adapter.py`, `tests/integration/test_gmail_adapter.py`, and `tests/integration/test_github_cli_adapter.py` for full examples shipping with Agent Core. The Gmail test, for example, decodes the rendered `raw` field to verify the RFC822 message contains the right subject and body — that's the level of rigor request-template tests should reach.

## Sharing your adapter

The shareable artifact is the `adapter.json` (plus its directory if you bundle any helper assets). Two distribution paths:

### Drop-in

Email or send the directory. The recipient drops it in their `data/adapters/<id>/` and clicks **Install** from Browse Adapters (or restarts).

### Git

Push the adapter to a public or private git repo with `adapter.json` at the root (or in a subdirectory matching the adapter id). Recipients install with:

```
git:owner/repo@ref
```

via the Browse Adapters page. The ref (branch/tag/commit) is optional. Agent Core clones, validates, runs the dangerous-pattern scan for code-bearing backends, and installs.

A future agent-core registry would make this `agent-core adapters install <slug>` — that path is intentionally deferred until there's demand to operate a hosted catalog.

## Reference: shipped adapters

| Adapter | Backend | What it demonstrates |
|---|---|---|
| `transmission` | `http` | session-handshake (`challenge_retry`), multi-field basic auth, destructive actions |
| `google_gmail` | `http` | OAuth2 refresh, refresh-token rotation persistence, per-binding refresh lock, RFC822 message construction via the `rfc822_base64url` filter |
| `github_cli` | `cli` | subprocess execution, `requires.bins` gating, JSON output parsing, env-var token injection |

Browse their manifests in `app/adapter_templates/<id>/adapter.json` for working, tested examples of every feature.

## Reference: minimum viable adapters (cheat sheet)

**API-key bearer** (simplest case):
```json
{
  "spec_version": "1.0",
  "id": "my_service", "version": "1.0.0", "display_name": "My Service",
  "credential_schema": { "fields": [ { "name": "api_key", "secret": true, "required": true } ]},
  "actions": [{ "name": "ping", "side_effect": "read", "input_schema": { "type": "object" } }],
  "backend": {
    "type": "http",
    "base_url": "https://api.example.com",
    "auth": { "type": "bearer",
      "apply": { "target": "request_header", "name": "Authorization", "template": "Bearer {{ cred.raw }}" }
    },
    "requests": { "ping": { "method": "GET", "path": "/v1/ping" } }
  }
}
```

**OAuth2**: copy the `auth`, `refresh`, and `credential_schema` blocks from `app/adapter_templates/google_gmail/adapter.json`; change `token_url`, `base_url`, and the action `requests`.

**Session handshake**: copy the `auth` and `session` blocks from `app/adapter_templates/transmission/adapter.json`; change the trigger status, header names, and `requests`.

**CLI wrapper**: copy the `backend` and `requires.bins` from `app/adapter_templates/github_cli/adapter.json`; change `bin`, `env`, `commands`.

---

## Troubleshooting

**Adapter shows up in Browse Adapters as "unavailable":** check its `requires.bins` and `requires.env`. The adapter declared a binary or env var that's missing on this host. Install the binary or set the env var, then restart.

**Adapter installs but binding fails at execute:** check the adapter's `requires.config`. The binding's `config_json` is missing a required field.

**"Adapter manifest invalid":** check your `spec_version`, `id` (`[a-z0-9_]+`), `version` (semver), and the `backend.type`. The full schema is in `app/connectors/manifest.py`.

**OAuth refresh isn't firing:** verify `refresh.trigger.http_status` matches what the provider returns on token expiry (usually 401), and that the credential's `expires_at` field is present if you used `or_expired`. Remember: the OAuth access-token expiry lives inside the credential JSON blob, **not** on the credential row's own `expires_at` (setting that would make the whole credential unreachable for refresh).

**Templated value comes through as the string `"None"` or `"True"`:** the resolver is the locked-down kind. Use `| default(<value>, as=<type>)` to provide a typed default; the type cast preserves the JSON shape through the render → re-parse cycle.

**Dangerous-pattern scan rejects my mcp/cli manifest:** the scan looks for known dangerous patterns in shell args, env values, and command strings. Review the rejected lines; if they're false positives for your adapter, file an issue with the manifest excerpt.

---

## Design notes (for the curious)

The adapter module system is the result of one explicit design choice: **distributable adapters never add code to a user's Agent Core instance.** Every install path produces a manifest in `data/adapters/`, never a Python file in `app/connectors/`. This is what makes adapters safe to share and what makes them survive Agent Core upgrades.

`app/connectors/` (the engines) is maintainer territory and is replaced on upgrade — it's the product itself. `app/adapter_templates/` ships the curated catalog. `data/adapters/` is yours. The boundary is the same one OpenClaw skills, OpenAPI specs, and MCP servers already use: data and external processes can be shared safely; in-process code cannot.

For the deeper design rationale and constraints, the maintainer's plan lives in `plan.md` at the repo root (gitignored — local-only).
