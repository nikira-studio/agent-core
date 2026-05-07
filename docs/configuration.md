# Configuration

Agent Core is configured through environment variables. For local development, copy `.env.example` to `.env` — all settings have sensible defaults and Agent Core will run without changing anything.

---

## Runtime Version

The supported runtime is the Docker image defined in the repository `Dockerfile`. It currently uses Python 3.11.

Local development should also use Python 3.11. Do not use syntax or dependency versions that require a newer Python version unless you update the Dockerfile, requirements, and documentation together.

Compatibility check:

```bash
python3.11 -m compileall app tests
```

For startup or dependency changes, verify the container too:

```bash
docker compose build
docker compose up -d
curl http://localhost:3500/health
```

---

## Core Settings

| Variable | Default | Description |
| --- | --- | --- |
| `AGENT_CORE_PORT` | `3500` | The port Agent Core listens on. |
| `AGENT_CORE_DATA_PATH` | `./data` | Where Agent Core stores its database, vault key, and broker credential. Use `/data` inside Docker (see below). |
| `AGENT_CORE_ENCRYPTION_KEY` | `auto` | Fernet encryption key for vault values. `auto` generates a key on first startup and saves it to `vault.key` in the data directory. If you set this manually, use a base64-encoded 32-byte Fernet key. |

---

## Sessions

| Variable | Default | Description |
| --- | --- | --- |
| `AGENT_CORE_SESSION_DURATION_HOURS` | `8` | How long a logged-in session lasts before requiring a new login. |
| `AGENT_CORE_INACTIVITY_TIMEOUT_MINUTES` | `30` | How long a session stays active without any requests before it expires. |
| `AGENT_CORE_COOKIE_SECURE` | `false` | When `true`, dashboard session cookies are sent with the browser `Secure` flag. Enable this when Agent Core is served over HTTPS or behind a TLS-terminating reverse proxy. |

---

## Security and Network

| Variable | Default | Description |
| --- | --- | --- |
| `AGENT_CORE_CORS_ORIGINS` | *(empty)* | Comma-separated list of allowed browser origins. When empty, CORS is permissive but credentials (cookies) are disabled for cross-origin requests. Set this if a separate frontend needs to make authenticated requests. |
| `AGENT_CORE_ALLOWED_IPS` | *(empty)* | Comma-separated IPs or CIDR ranges. When set, any request from an IP outside the list is rejected with `403`. Useful if Agent Core is reachable on a LAN and you want to restrict access. |

---

## Agents and Memory

| Variable | Default | Description |
| --- | --- | --- |
| `AGENT_CORE_SHARED_SCOPE_AGENTS` | *(empty)* | Comma-separated agent IDs that are allowed to write to the `shared` memory scope, in addition to agents that have `shared` explicitly in their `write_scopes`. This does not grant vault access. |
| `AGENT_CORE_EMBEDDING_MODEL` | `nomic-embed-text` | The Ollama embedding model used for semantic memory search. If Ollama is unavailable, memory search falls back to FTS5 automatically. |
| `AGENT_CORE_OLLAMA_URL` | `http://localhost:11434` | The Ollama endpoint. Change this if Ollama runs on a different host or port. |
| `AGENT_CORE_STALE_THRESHOLD_MINUTES` | `5` | How many minutes without a heartbeat before an active agent task is automatically marked `stale`. |

---

## Data Directory

Agent Core keeps all its runtime state under `AGENT_CORE_DATA_PATH`:

```text
data/
  agent-core.db       ← SQLite database (memory, agents, vault metadata, activity, sessions)
  vault.key           ← Current Fernet encryption key for vault values (primary key)
  vault.keyring       ← JSON file containing key history (all historical keys for decryption)
  broker.credential   ← Plaintext credential for the local Credential Broker (auto-generated)
  backups/            ← Backup ZIPs and pre-restore snapshots land here
```

**`vault.key` and `vault.keyring` are gitignored and should never be committed.** If you lose `vault.key`, you lose the ability to decrypt your vault entries unless you have a keyring with historical keys that can still decrypt them. Back up both files alongside the database.

**Vault key rotation:** When you rotate the vault key from the dashboard or API, a new primary key is generated, added to the keyring, and all vault entries are re-encrypted with the new key. The old key remains in the keyring so existing entries encrypted with it can still be decrypted.

---

## Docker Setup

The example `docker-compose.yml` mounts the data directory into the container:

```yaml
volumes:
  - ./data:/data
environment:
  AGENT_CORE_DATA_PATH: /data
```

This means data persists on your host machine across container restarts. For a shared or production deployment, use a named volume or a well-backed host path:

```yaml
volumes:
  - agent-core-data:/data

volumes:
  agent-core-data:
```

---

## What You Actually Need to Change

For a basic local setup, the defaults are fine. The variables most likely to matter:

- **`AGENT_CORE_ENCRYPTION_KEY`** — leave as `auto` unless you want to manage the key yourself (advanced).
- **`AGENT_CORE_ALLOWED_IPS`** — set if the service is accessible beyond localhost.
- **`AGENT_CORE_CORS_ORIGINS`** — set if a separate web app needs authenticated access.
- **`AGENT_CORE_OLLAMA_URL`** — set if Ollama runs on a different host (NAS, another container, etc.).
