# Configuration

Agent Core is configured through environment variables. For local development, copy `.env.example` to `.env` — everything has sensible defaults and it'll run without changes.

**The short version:** for a basic local setup, you probably don't need to change anything.

For what these settings actually affect — the memory model, verification, the review queue — see [How It Works](how-it-works.md).

---

## Core Settings

| Variable | Default | What it does |
| --- | --- | --- |
| `AGENT_CORE_PORT` | `3500` | The port Agent Core listens on |
| `AGENT_CORE_DATA_PATH` | `./data` | Where Agent Core stores its database, encryption keys, and broker credential. Set this to `/data` inside Docker (see below) |
| `AGENT_CORE_ENCRYPTION_KEY` | `auto` | Encryption key for stored credentials. Leave as `auto` — Agent Core generates a key on first startup and saves it to `data/credential.key`. Only set this manually if you need to manage the key yourself (advanced use) |

---

## Sessions

These control how long dashboard logins stay active.

| Variable | Default | What it does |
| --- | --- | --- |
| `AGENT_CORE_SESSION_DURATION_HOURS` | `8` | Maximum session lifetime before requiring a new login |
| `AGENT_CORE_INACTIVITY_TIMEOUT_MINUTES` | `30` | How long a session can sit idle before expiring |
| `AGENT_CORE_COOKIE_SECURE` | `false` | Set to `true` if Agent Core is served over HTTPS or behind a TLS proxy — makes browser cookies require a secure connection |
| `AGENT_CORE_TRUSTED_PROXIES` | *(empty)* | Comma-separated proxy IPs that may supply `X-Forwarded-For` for login rate-limit identity |

---

## Security and Network

| Variable | Default | What it does |
| --- | --- | --- |
| `AGENT_CORE_CORS_ORIGINS` | *(empty)* | Comma-separated list of allowed browser origins. Only needed if a separate web frontend needs to make authenticated requests. Example: `http://localhost:5173` |
| `AGENT_CORE_ALLOWED_IPS` | *(empty)* | Comma-separated IPs or CIDR ranges. When set, requests from any IP not on this list are rejected with `403`. Useful if Agent Core is reachable on a LAN and you want to limit who can connect |
| `AGENT_CORE_BLOCK_INTERNAL_HOSTS` | `false` | Set to `true` to block connector bindings/imports from reaching private, loopback, or link-local hosts. Use this if you want to disable local probing by default |
| `AGENT_CORE_ALLOWED_INTERNAL_HOSTS` | *(empty)* | Comma-separated hostnames that are always allowed even when `AGENT_CORE_BLOCK_INTERNAL_HOSTS=true`. Useful for operator-managed internal services such as `firecrawl` or `searxng` |

---

## Memory and Agents

| Variable | Default | What it does |
| --- | --- | --- |
| `AGENT_CORE_SHARED_SCOPE_AGENTS` | *(empty)* | Comma-separated agent IDs that can write to the `shared` memory scope, in addition to any agent that has `shared` explicitly in its `write_scopes`. This does not grant credential access |
| `AGENT_CORE_STALE_THRESHOLD_MINUTES` | `5` | How long an agent can go without sending a heartbeat before its active task is automatically marked stale |
| `AGENT_CORE_TOOL_RESULT_SPILL_THRESHOLD` | `8000` | MCP tool outputs larger than this many serialized characters are stored in Agent Core and returned as a `result_fetch` handle. Set to `0` to disable spilling |
| `AGENT_CORE_TOOL_RESULT_SPILL_TTL_HOURS` | `24` | How long spilled tool results remain retrievable before cleanup |

---

## Automatic Maintenance

| Variable | Default | What it does |
| --- | --- | --- |
| `AGENT_CORE_MAINTENANCE_INTERVAL_MINUTES` | `60` | How often the in-process maintenance sweep runs: marks stale activities, prunes scratchpad memories past retention, sweeps `expires_at` TTL'd records, and purges retracted/superseded records past their retention window. Set to `0` to disable the automatic schedule — the manual **Run Maintenance** button in Settings still works |
| `AGENT_CORE_MAINTENANCE_INITIAL_DELAY_SECONDS` | `300` | Delay before the first automatic run after startup |

The scratchpad and retracted/superseded retention windows themselves (7 and 30 days by default) are configured from **Settings → System Behavior** in the dashboard. The last run's time, trigger, and results are shown in **Settings → Backup & Restore** and available from `GET /api/backup/maintenance/status`.

The maintenance sweep also prunes the connector execution and webhook delivery logs (30 days by default, `0` keeps them forever) and runs the memory verification pass described below. All of these windows are set from **Settings → System Behavior**.

**Vector search** — the embedding provider, endpoint URL, model, and auth type — is configured from **Settings → Vector Search** in the dashboard, not through environment variables. Semantic search is off by default. When it's disabled or the embedding backend is unreachable, Agent Core falls back to full-text search automatically.

---

## Memory Verification

Facts can carry a `subject_anchor` naming what would confirm them — `repo:<path>`, `host:<name-or-ip>`, or `service:<binding_id>`. The maintenance sweep checks anchored facts against the real thing and records what it found.

To make `repo:` anchors resolvable, map each workspace scope to a directory on the machine running Agent Core. Set `workspace_repo_roots` in `system_settings` to a JSON object:

```json
{
  "workspace:my-project": "/srv/projects/my-project",
  "workspace:docs": "/srv/projects/docs"
}
```

**Anchor paths are relative to that root, never absolute.** The same directory has a different absolute path inside every agent's container, so an absolute anchor is resolvable only by whoever wrote it. Agent Core resolves the relative path against this installation's root, which is what lets agents with different mounts share the same memory.

A scope with no configured root reports `unverifiable` rather than guessing — it never treats "I could not check this" as "this is wrong". `host:` anchors are verified through a connector binding that already targets that host, so credentials stay in the connector layer.

| Setting (in `system_settings`) | Default | What it does |
| --- | --- | --- |
| `workspace_repo_roots` | `{}` | JSON map of workspace scope to filesystem root |
| `verification_pass_enabled` | `1` | Whether the maintenance sweep verifies anchored facts |
| `verification_pass_limit` | `50` | Records checked per run, least-recently-attempted first |

---

## Review Model (optional)

Some judgements cannot be made by rules, because they depend on meaning rather than shape: whether a record is still worth keeping, or whether a newer memory supersedes an older one. Agent Core can use a language model for those.

**It is a capability, not a dependency.** Memory, search, credentials, connectors and the mechanical clean-up rules all work with no model configured. Configuring one turns on the features that need judgement — including for records written long before it existed. Leave it unset and those features report themselves as unconfigured rather than failing.

Configure it from **Settings → Review Model** in the dashboard, or set these directly:

| Setting (in `system_settings`) | What it does |
| --- | --- |
| `review_model_provider` | `ollama` for a direct endpoint, `binding` to use a connector binding, empty for none |
| `review_model_url` / `review_model_name` | For `ollama`: where the model runs and which model to use. **Load Models** on that card lists what the endpoint has installed, the same way the embedding card does; the field stays free text so a model the endpoint does not report still works |
| `review_model_binding_id` / `review_model_action` | For `binding`: which binding to call, and its chat-completions action |
| `review_model_timeout_seconds` | Defaults to 60 |

Point it at a model on a machine you control and record content never leaves it. Point it at a hosted API and it does — that is the trade-off, and it is why there is no default.

The **Test** button on that card (or `POST /api/dashboard/review-model/test`) checks it. Configuration alone does not mean a model answers, and these features need one that can follow a format instruction, so the check asks for a small JSON reply and reports what came back.

---

## Verification Beyond Code

`repo:`, `host:` and `service:` are what Agent Core can check on its own. What actually settles a record depends on what the record is about — a web page, a policy document, a contact, a ticket — so **anchor types are open**, and an installation teaches the system to check its own by mapping a type to a connector binding it already has:

```json
{
  "url":   {"binding_id": "<firecrawl-or-http-binding>", "action": "GET /scrape", "value_param": "url"},
  "doc":   {"binding_id": "<drive-binding>", "action": "GET /files", "value_param": "q"}
}
```

Stored in `system_settings` under `verification_bindings`. The anchor's value is passed as `value_param` (default `target`), along with any fixed `params`.

A binding that answers verifies the anchor. One that reports 404/not-found is evidence the subject is gone, and becomes a review proposal. **Anything else — a timeout, an error, an unconfigured type — leaves the record unverifiable rather than condemning it.** An outage is not a false memory, and a system that forgets that will delete true records the first time the network hiccups.

Writing a record with an anchor type nothing can check is allowed and warned about, not refused: the anchor still records what *would* settle it, which is useful to a human even when no automation can follow it.

---

## Usefulness Review (optional, off by default)

Agent Core can ask a language model whether a memory record is actionable for a future session — the one judgement the mechanical rules cannot make, since a record can be perfectly accurate and still useless.

**This sends record content to whatever model you point it at.** Point it at a model running on your own machine and nothing leaves it; point it at a hosted API and your memory content is sent there. That is why it is off by default and why there is no default provider.

Configure a review model (above), then turn this feature on:

| Setting (in `system_settings`) | Default | What it does |
| --- | --- | --- |
| `usefulness_review_enabled` | `0` | Must be `1` for anything to run |
| `usefulness_review_limit` | `20` | Records judged per run |

It uses the review model configured above, so a model is set up once for the whole system rather than per feature.

Run it with `POST /api/memory/proposals/review-usefulness` (admin). Records that plainly name a path, command, version, address, or rule are skipped without a model call. Anything the model judges unhelpful becomes a **proposal in the review queue with the model's reason attached** — it is never applied automatically, no matter how accurate it proves, because the cost of a wrong call is deleting a constraint someone depended on.

---

## Data Directory Layout

Agent Core keeps all its state under `AGENT_CORE_DATA_PATH`:

```
data/
  agent-core.db       ← SQLite database (memory, agents, credential metadata, activity, sessions)
  credential.key      ← Current encryption key for credentials
  credential.keyring  ← JSON file with all historical keys (needed for decryption after rotation)
  broker.credential   ← Credential for the local Credential Broker (auto-generated at first startup)
  backups/            ← Backup ZIPs and pre-restore snapshots land here automatically
```

**The key files matter a lot:**

- `credential.key` and `credential.keyring` are gitignored and should never be committed. If you lose the current key file, you lose the ability to decrypt stored credentials — unless you have a backup that includes the key material or a separately saved restored key.
- The database and key files need to travel together for local restore, but the dashboard backup export now encrypts the archive with a separate one-time backup key that is shown after export.

**Key rotation:** When you rotate the credential encryption key, Agent Core generates a new primary key, backs up the old one to the keyring, and re-encrypts all credential entries. The keyring means older entries can still be decrypted — nothing breaks during rotation.

---

## Docker Setup

The example `docker-compose.yml` mounts your local `data/` directory into the container at `/data`:

```yaml
volumes:
  - ./data:/data
environment:
  AGENT_CORE_DATA_PATH: /data
```

This means data persists on your host machine across container restarts and rebuilds. For a shared or more permanent deployment, consider a named Docker volume instead:

```yaml
services:
  agent-core:
    volumes:
      - agent-core-data:/data
    environment:
      AGENT_CORE_DATA_PATH: /data

volumes:
  agent-core-data:
```

### Workers

Agent Core runs as **a single process**, and that is the supported configuration. `AGENT_CORE_WORKERS` defaults to `1`.

Some state is held per process rather than in the database, on purpose — it is the kind of state that would cost more to coordinate than it saves:

| State | What a second worker does to it |
| --- | --- |
| Rate-limit buckets | Each process keeps its own, so the effective limit is multiplied by the worker count — including the login throttle |
| Concurrent-search guard | Same: the cap applies per process, not per installation |
| Dashboard event stream | A browser is connected to one process and never sees events published by another, so the live view goes quiet at random |

The maintenance sweep is the exception: it takes a lease in the database, so only one process runs it per tick regardless of how many exist.

You can raise `AGENT_CORE_WORKERS`, and nothing will crash — the effects above are quiet, not loud, which is exactly why the default is 1. A single process handles a local-first workload comfortably; horizontal scaling would need shared coordination that does not exist yet.

---

## Runtime Version

The Docker image is the supported runtime and currently uses Python 3.11.

If you're running locally, use Python 3.11 too. Newer Python versions accept syntax that the Docker image will reject — so code that works locally might fail when you rebuild the container.

Quick compatibility check:

```bash
python3.11 -m compileall app tests
```

For startup or dependency changes, verify the container as well:

```bash
docker compose build
docker compose up -d
curl http://localhost:3500/health
```

---

## What You Actually Need to Change

For a local setup, the defaults are usually fine. Here's what's actually worth looking at:

- **`AGENT_CORE_ALLOWED_IPS`** — set this if Agent Core will be accessible to other machines on your network
- **`AGENT_CORE_BLOCK_INTERNAL_HOSTS`** — set this to `true` if you want to block connector bindings/imports from private, loopback, or link-local hosts.
- **`AGENT_CORE_ALLOWED_INTERNAL_HOSTS`** — use this only as an exception list when `AGENT_CORE_BLOCK_INTERNAL_HOSTS=true`, for trusted internal services like `firecrawl` or `searxng`
- **`AGENT_CORE_CORS_ORIGINS`** — set this if you're building a separate web app that needs to make authenticated requests
- **`AGENT_CORE_COOKIE_SECURE`** — set to `true` if serving over HTTPS
- **`AGENT_CORE_ENCRYPTION_KEY`** — leave as `auto` unless you have a specific reason to manage the key yourself
- **Vector search settings** — configured from **Settings → Vector Search** in the dashboard, not here. If your Ollama instance is on a different host, update the URL there after starting
