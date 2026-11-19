# Troubleshooting

Something not working? Here's how to diagnose and fix the most common issues.

---

## Startup

### Agent Core won't start

Check the logs first; they almost always tell you exactly what's wrong:

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

Agent Core creates its schema automatically on startup. If you deleted the database and want a fresh start, delete the file (not the `data/` directory) and restart; Agent Core will rebuild everything on boot.

### The encryption key file is missing

Agent Core generates its encryption key on first startup and saves it to `data/credential.key`. If that file is gone but the database still exists, your stored credentials are unrecoverable without a backup.

Restore from a backup that includes both the database and key material, or start fresh by deleting the database and letting Agent Core rebuild from scratch. Encrypted backup exports also require the one-time backup key that was shown at download time.

### Database is locked

SQLite allows one writer at a time. If you're seeing lock errors, you probably have two Agent Core processes pointed at the same `data/agent-core.db`. WAL mode and a 5-second busy timeout help with transient contention, but concurrent multi-process writes aren't supported. Make sure only one instance is running against that data directory.

### The upgrade is refused by the access check

Some upgrade steps change how agents, services, connections or credentials are stored, and each one runs under a check: Agent Core records access before and after the step, and rolls the whole step back if anything differs that the step did not say it would change. Nothing needs repairing, because the database is at the previous version. Read the report named in the error (`migration-reports/`), start the previous version again, and send the report to the project. See [Upgrading](upgrading.md) for the whole procedure.

To see what an upgrade would do without changing anything, run `python -m app.access_check /path/to/agent-core.db` against a copy of the database.

---

## Authentication

### Login says too many failed attempts

Failed logins are limited per client address and per account, and the count is kept in the database, so a restart does not clear it. Behind a reverse proxy without `AGENT_CORE_TRUSTED_PROXIES`, every client shares one address, so failures from anything can lock everyone out until the limit refills (about one attempt per minute). Set `AGENT_CORE_TRUSTED_PROXIES` to the proxy's address. To clear the lockout now, from the host:

```bash
docker exec agent-core python3 -c "from app.database import get_db
with get_db() as c: c.execute(\"DELETE FROM rate_limit_buckets WHERE bucket_key LIKE 'login_failed:%' OR bucket_key LIKE 'otp_failed:%'\"); c.commit()"
```

Adjust the container name to your install.

### My agent key returns `401`

Possible causes:
- The key was rotated after you copied it (API keys are shown once and can't be retrieved)
- The agent record has been deactivated
- The wrong value is in the `Authorization` header

Add a new key from the dashboard (**Agents → More → Keys**, or generate one on the Connect page), revoke the old one there, then update wherever you've configured it.

### Admin routes return `403`

Only the admin user can access global audit, backup, broker rotation, and user administration routes. The first registered user becomes admin; others are non-admin unless the admin grants that role.

For memory and credentials, agents can only access what their `read_scopes` and `write_scopes` allow. Use workspace scopes (`workspace:<id>`) for cross-user collaboration; don't try to grant one agent another user's personal scope.

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
  -H "Authorization: Bearer <operator-token>"
```

Replace `<operator-token>` with a token from **Settings → Account → Operator tokens** (valid 5 to 60 minutes, shown once). The sign-in session token is refused on these routes.


If your SQLite build doesn't include FTS5, the Docker image is the fix; it bundles a compatible Python and SQLite build where FTS5 is always available.

---

## Credentials

### Broker resolution fails

Work through this checklist:

- Is the helper key current? Agent Core updates `data/broker.credential`
  during rotation. Update brokers that use `--token` or a copied token file.
- Is the `Authorization` header using `Broker <credential>` (not `Bearer`)? The helper uses a different auth scheme from agents.
- Is the configured `agent_id` active and not deactivated?
- Is that entry an agent and not a service? A service fetches its secrets with a grant, and the answer is `403 NOT_AN_AGENT`.
- Does that agent hold the Reference credentials permission? Without it the answer is `403 PERMISSION_REQUIRED`. Switch it on under **Agents > Access**, with **Turn on**.
- Does that agent have read access to the credential entry's scope?
- Does the credential entry have an `expires_at` TTL that has passed?

### Reveal fails

Revealing a raw credential value requires a user session. Agent API keys can't reveal credentials; this is intentional. Most workflows should not need reveal; use connections for server-side actions or the Local credential helper for local process injection.

### Credential edits did not change the secret

On the Credentials page, the credential edit form has an optional replacement secret field. If you leave it blank, Agent Core keeps the existing encrypted value and updates only metadata such as name, label, and type. Enter a new value only when you want to replace the stored secret.

---

## Access

### An agent cannot use a connection

Two things are needed, and they are edited in different places.

1. The agent must be on the connection's **Who can use it** list: a named agent, everyone in a workspace, or all of one person's agents, with an exclusion for a single agent. Being on that list lets the agent use the connection and nothing more; it gives no memory access and no credential access.
2. The agent must hold the **Run capabilities** permission. Where it does not, the row is shown as needing that permission, with a **Turn on** button. Nothing switches a permission on by itself.

An agent that could use the connection before 1.4.0 still can: the upgrade filled every list from the connection's owner, so no agent gained anything and none lost anything.

### A credential made with a connection

A credential entered while adding a connection is stored for that connection alone. No agent can reach it, and only Agent Core uses it, so an agent that asks for it by name gets nothing. Tick **Also let agents reference this credential** when you create it to store it under the connection's owner instead.

Deleting a connection asks whether its credential goes with it. Through the API the credential is kept unless you ask for it to be deleted; a kept one is listed on the Access page under Left behind, because no agent can reach it any more.

### A service cannot own a connection

Creating a connection with a service as its owner, or changing a connection's owner to a service, is refused with `SERVICE_CANNOT_OWN`. A service has no permission to see or run capabilities, so it could never use one. Connections you already have are not changed, and the Owner list no longer offers services.

### The Health strip says a connection is failing

A connection counts as failing when its last check failed, or when one of its actions failed within the last 7 days and either its latest run failed or at least a fifth of at least five recent calls failed. A failure older than that, with nothing failing since, is history: the Capabilities table shows it as a quiet line and the Health strip does not count it. A caller naming an action the connection does not have is not counted as the connection failing.

---

## Backup and restore

### Restore is rejected

The decrypted backup ZIP must include `agent-core.db`, `credential.key`, and `manifest.json`. It may also contain `credential.keyring` from older rotations. The manifest must include SHA-256 checksums for `agent-core.db` and `credential.key` that match the extracted files. A missing required file or checksum mismatch causes the restore to be rejected and leaves your existing data untouched.

### I lost my OTP

If OTP is enabled and you can no longer generate codes, use **Settings → Account → Sign-in** to turn off two-step sign-in and enroll again with a fresh authenticator app.

---

## Dashboard

### Dashboard pages show 404

The dashboard root is `/` (Needs you). Main pages are: `/agents`, `/integrations` (Connect), `/workspaces`, `/memory`, `/connectors` (Capabilities), `/credentials` (Credentials), `/services` (Services), `/access` (Access), `/activity`, `/approvals`, and `/settings`; admins also have `/users`, `/webhooks` and `/audit` (the Audit log tab under Activity). If you're getting 404 on these, confirm Agent Core is running and check the logs.

The old address `/agents?show=services` redirects to `/services`, where the programs that only fetch secrets are listed.

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

Common causes: port 3500 already in use on the host, the `./data` mount isn't writable (the container runs as uid 1001; a folder Docker created as root needs `chown 1001:1001 data`), or an environment variable has an invalid format.

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

## Keeping secrets out of version control

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
