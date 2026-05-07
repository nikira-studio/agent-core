# Troubleshooting

Common issues and how to fix them.

---

## Startup

### Docker reports a Python `SyntaxError`

The Docker image is the supported runtime and currently uses Python 3.11. If local checks passed on a newer Python version but Docker fails during import, first run the compatibility check with Python 3.11:

```bash
python3.11 -m compileall app tests
```

Fix any syntax error reported there before rebuilding. Do not rely on Python 3.12+ or 3.14+ syntax unless the Docker base image is intentionally updated in the same change.

### The service won't start

Check the logs first:

```bash
# Docker
docker compose logs agent-core

# Local
uvicorn app.main:app --reload --port 3500
# errors will print to the terminal
```

Common causes: port 3500 is already in use, the `data/` directory isn't writable, or an environment variable has an invalid value.

### Database tables are missing

Agent Core creates its current schema automatically on startup. If you deleted the database and want a fresh one, restart the service after the data file is removed. The app will rebuild the tables on boot.

### `vault.key` is missing

Agent Core generates `data/vault.key` automatically on first startup. If the file is gone but the database still exists, vault entries in that database cannot be decrypted. You'll need to restore from a backup that includes both files, or accept that existing vault entries are unrecoverable and start fresh.

### Database is locked

SQLite allows one writer at a time. If you're seeing lock errors, confirm only one Agent Core instance is using the same `data/agent-core.db` file. WAL mode and a 5-second busy timeout are configured, but this is a local-first SQLite deployment — concurrent writes from multiple processes aren't supported.

---

## Authentication

### My agent key returns `401`

The key may have been rotated, the agent record may be inactive, or the wrong value is in the `Authorization` header. Agent API keys are shown once at creation or rotation and cannot be retrieved. Rotate the key from the dashboard (**Agents → [agent] → Rotate Key**) to get a new one.

### Admin routes return `403`

Only admin users can access global audit, backup, broker rotation, and user administration routes. Users can manage agents they own. Agents belong to one owner/default user, so personal `user:<id>` scopes are limited to that owner; use workspace scopes (`workspace:<id>`) for multi-user collaboration. Non-admin users cannot grant another user's workspaces, another user's agents, or shared write access. The first registered user becomes admin. Subsequent users registered by an admin are non-admin unless the admin assigns the admin role.

### OTP verification fails

TOTP codes are time-sensitive. Check that your authenticator app and the server clock are in sync. If you regenerated backup codes, old codes are immediately invalidated.

---

## Memory

### I get `SCOPE_DENIED`

The agent or session doesn't have the required read or write scope for the operation. Default agent write access covers only `agent:<agent_id>`. You can grant additional scopes through the dashboard (**Agents → [agent] → Edit**) or via `PUT /api/agents/{agent_id}`.

### I get `PII_DETECTED`

Writes to the `shared` scope are rejected if the content resembles PII or a credential (emails, phone numbers, API key patterns). Use a narrower scope (`agent:<id>` or `user:<id>`) for operational data that might contain sensitive values.

### Search returns no results

Things to check:
- Is the record in a scope the caller has read access to?
- Was the record retracted or superseded? Those are excluded by default. Add `"include_retracted": true` or `"include_superseded": true` to your search request.
- Is the query too short or trivially common (single stop words)? Queries under 2 characters are rejected, and queries matching common noise patterns are also rejected.

### FTS5 errors on search or write

Run the startup health checks:

```bash
curl http://localhost:3500/api/backup/startup-checks \
  -H "Authorization: Bearer <admin-session>"
```

If your SQLite build doesn't include FTS5, use the Docker image, which bundles a compatible Python and SQLite build.

---

## Vault

### Broker resolution fails

Work through this checklist:
- Is the broker credential current? If it was rotated, update `data/broker.credential`.
- Is the `Authorization` header set to `Bearer <broker-credential>` (not the agent key)?
- Is the configured `agent_id` active (not deactivated)?
- Does that agent have read scope on the vault entry's scope?
- Is the vault entry active and not past its `expires_at` TTL?

### Reveal fails

Revealing a vault value requires an admin session and a valid OTP code. Non-admin sessions and agent keys cannot reveal raw values.

---

## Backup and Restore

### Restore is rejected

The backup ZIP must contain exactly three files: `agent-core.db`, `vault.key`, and `manifest.json`. The manifest must include SHA-256 checksums that match the extracted files. Any extra file, missing file, or checksum mismatch causes the restore to be rejected.

### I lost my backup OTP

OTP is required for both backup export and restore. If you can't generate an OTP code and have exhausted your backup codes, you'll need direct filesystem access to the data directory. There is no API bypass.

---

## Dashboard

### Dashboard pages show 404

The dashboard root is `/`. The main pages are `/users` for admins, `/agents`, `/workspaces`, `/memory`, `/vault`, `/agent-setup` (Integration), `/activity`, and `/settings`. Admins can open the audit log from Overview or Settings. If you're getting 404 on these, confirm the service is running and check the logs.

### CORS errors in the browser

If a separate frontend app needs to make authenticated requests to Agent Core, set `AGENT_CORE_CORS_ORIGINS` to the origin of that app. Example: `AGENT_CORE_CORS_ORIGINS=http://localhost:5173`.

---

## Docker

### The container exits immediately

```bash
docker compose logs agent-core
```

Common causes: port 3500 already in use on the host, the `./data` mount is unwritable, or an environment variable has an invalid format.

### Data disappears after restart

Confirm your `docker-compose.yml` mounts a persistent path:

```yaml
volumes:
  - ./data:/data
environment:
  AGENT_CORE_DATA_PATH: /data
```

Without the volume mount, data lives inside the container layer and is lost when the container is removed.

---

## Keeping Secrets Out of Version Control

Before committing or publishing:

```bash
git status --ignored   # verify data/, .env, backups are excluded
python3.11 -m compileall app tests
pytest -q              # confirm tests pass
```

If you have a secret scanner available:

```bash
gitleaks detect --source . --verbose
# or
trufflehog filesystem .
```

Do not commit `data/`, `.env`, backup ZIPs, or anything under `private/`.
