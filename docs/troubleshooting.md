# Troubleshooting

Something not working? Here's how to diagnose and fix the most common issues.

---

## Startup

### The service won't start

Check the logs first — they almost always tell you exactly what's wrong:

```bash
# Docker
docker compose logs agent-core

# Local
uvicorn app.main:app --reload --port 3500
# errors will print to the terminal
```

Most common causes:
- Port 3500 is already in use by something else
- The `data/` directory isn't writable
- An environment variable has an invalid value

### Docker reports a Python `SyntaxError`

The Docker image runs Python 3.11. If you developed locally on a newer Python version, you may have used syntax that 3.11 doesn't support. Run this to catch it before rebuilding:

```bash
python3.11 -m compileall app tests
```

Fix any errors reported there, then rebuild. Don't use Python 3.12+ syntax unless you're intentionally updating the Dockerfile.

### Database tables are missing

Agent Core creates its schema automatically on startup. If you deleted the database and want a fresh start, delete the file (not the `data/` directory) and restart — Agent Core will rebuild everything on boot.

### The encryption key file is missing

Agent Core generates its encryption key on first startup and saves it to `data/credential.key`. If that file is gone but the database still exists, your stored credentials are unrecoverable without a backup.

Restore from a backup that includes both the database and key material, or start fresh by deleting the database and letting Agent Core rebuild from scratch. Encrypted backup exports also require the one-time backup key that was shown at download time.

### Database is locked

SQLite allows one writer at a time. If you're seeing lock errors, you probably have two Agent Core processes pointed at the same `data/agent-core.db`. WAL mode and a 5-second busy timeout help with transient contention, but concurrent multi-process writes aren't supported. Make sure only one instance is running against that data directory.

---

## Authentication

### My agent key returns `401`

Possible causes:
- The key was rotated after you copied it — API keys are shown once and can't be retrieved
- The agent record has been deactivated
- The wrong value is in the `Authorization` header

Rotate the key from the dashboard (**Agents → [agent] → Rotate Key**) to get a fresh one, then update wherever you've configured it.

### Admin routes return `403`

Only the admin user can access global audit, backup, broker rotation, and user administration routes. The first registered user becomes admin; others are non-admin unless the admin grants that role.

For memory and credentials, agents can only access what their `read_scopes` and `write_scopes` allow. Use workspace scopes (`workspace:<id>`) for cross-user collaboration — don't try to grant one agent another user's personal scope.

### OTP verification fails

TOTP codes are time-sensitive. Make sure your authenticator app and the server clock are in sync.

---

## Memory

### I get `SCOPE_DENIED`

The agent or session doesn't have the required read or write access to that scope. By default, agents can only write to `agent:<their-id>`. Grant additional scopes through the dashboard (**Agents → [agent] → Edit**) or via `PUT /api/agents/{agent_id}`.

### I get `PII_DETECTED`

Writes to the `shared` scope are scanned for personal data and credential patterns (emails, phone numbers, API keys, etc.). If your content triggered this, move the write to a narrower scope like `agent:<id>` or `user:<id>`.

### Search returns no results

Things to check in order:

1. **Is the record in a scope the caller can see?** The agent's `read_scopes` determine what's searchable.
2. **Is the record retracted or superseded?** Both are excluded by default. Add `"include_retracted": true` or `"include_superseded": true` to your search request if you want them.
3. **Is the query too short or noisy?** Queries under 2 characters are rejected, and queries that match common stop-word patterns are also rejected.

### FTS5 errors on search or write

Run the startup health checks:

```bash
curl http://localhost:3500/api/backup/startup-checks \
  -H "Authorization: Bearer <admin-session>"
```

If your SQLite build doesn't include FTS5, the Docker image is the fix — it bundles a compatible Python and SQLite build where FTS5 is always available.

---

## Credentials

### Broker resolution fails

Work through this checklist:

- Is the broker credential current? If it was rotated, the file at `data/broker.credential` needs to be updated with the new value.
- Is the `Authorization` header using `Broker <credential>` (not `Bearer`)? The broker uses a different auth scheme from agents.
- Is the configured `agent_id` active and not deactivated?
- Does that agent have read access to the credential entry's scope?
- Does the credential entry have an `expires_at` TTL that has passed?

### Reveal fails

Revealing a raw credential value requires a user session. Agent API keys can't reveal credentials — this is intentional. Most workflows should not need reveal; use connector bindings for server-side actions or the Credential Broker for local process injection.

### Credential edits did not change the secret

On the Connectors page, the credential edit form has an optional replacement secret field. If you leave it blank, Agent Core keeps the existing encrypted value and updates only metadata such as name, label, and type. Enter a new value only when you want to replace the stored secret.

---

## Backup and Restore

### Restore is rejected

The decrypted backup ZIP must include `agent-core.db`, `credential.key`, and `manifest.json`. It may also contain `credential.keyring` from older rotations. The manifest must include SHA-256 checksums for `agent-core.db` and `credential.key` that match the extracted files. A missing required file or checksum mismatch causes the restore to be rejected and leaves your existing data untouched.

### I lost my OTP

If OTP is enabled and you can no longer generate codes, use **Settings → Security** to disable OTP and enroll again with a fresh authenticator app.

---

## Dashboard

### Dashboard pages show 404

The dashboard root is `/`. Main pages are: `/users` (admin only), `/agents`, `/workspaces`, `/memory`, `/connectors`, `/integrations` (Integrations), `/activity`, and `/settings`. Admins can open the audit log from Overview or Settings. If you're getting 404 on these, confirm the service is running and check the logs.

### CORS errors in the browser

This happens when a web app on a different origin tries to make authenticated requests to Agent Core. Set `AGENT_CORE_CORS_ORIGINS` in your `.env` to that app's origin:

```
AGENT_CORE_CORS_ORIGINS=http://localhost:5173
```

---

## Docker

### The container exits immediately

```bash
docker compose logs agent-core
```

Common causes: port 3500 already in use on the host, the `./data` mount isn't writable, or an environment variable has an invalid format.

### Data disappears after restart

Make sure your `docker-compose.yml` mounts a persistent path:

```yaml
volumes:
  - ./data:/data
environment:
  AGENT_CORE_DATA_PATH: /data
```

Without this volume mount, data lives inside the container layer and is gone when the container is removed or recreated.

---

## Keeping Secrets Out of Version Control

Before pushing or sharing:

```bash
git status --ignored   # verify data/, .env, and backups are excluded
python3.11 -m compileall app tests
pytest -q
```

If you have a secret scanner available:

```bash
gitleaks detect --source . --verbose
# or
trufflehog filesystem .
```

Never commit `data/`, `.env`, encrypted backup archives, backup keys, or anything under `private/`.
