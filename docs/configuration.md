# Configuration

Agent Core is configured through environment variables. For local development, copy `.env.example` to `.env` — everything has sensible defaults and it'll run without changes.

**The short version:** for a basic local setup, you probably don't need to change anything.

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

**Vector search** — the embedding provider, endpoint URL, model, and auth type — is configured from **Settings → Vector Search** in the dashboard, not through environment variables. Semantic search is off by default. When it's disabled or the embedding backend is unreachable, Agent Core falls back to full-text search automatically.

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
