# Backup and restore

Agent Core's state lives in three files: the database (`data/agent-core.db`), the current encryption key, and the key history file. A full backup export wraps those files in an encrypted archive. You keep the encrypted archive and the one-time backup key separately. If you restore the archive without the matching backup key, Agent Core cannot decrypt it.

---

## Full backup

A full backup requires an admin session. This is intentional: the encrypted archive and backup key together contain enough to restore all your credentials, so both are treated as sensitive.

```bash
curl -X POST http://localhost:3500/api/backup/export \
  -H "Authorization: Bearer <admin-session>" \
  -H "Content-Type: application/json" \
  -o agent-core-backup.zip.enc
```

The exported archive contains the same backup contents as before, but encrypted with a separate one-time backup key. The dashboard shows that key once after export, and the REST response also includes it in the `X-Agent-Core-Backup-Key` header.

The underlying backup archive contains:

- `agent-core.db`: the full database (memory, agents, credentials metadata, activity, connector types, bindings, and execution history)
- `credential.key`: the current encryption key
- `credential.keyring`: all historical keys (needed to decrypt entries from before a key rotation)
- `manifest.json`: version, timestamp, and SHA-256 checksums for everything in the archive

You can also trigger a backup from the dashboard: **Settings → Backup → Export Encrypted Backup**.

**Store the encrypted archive and the backup key securely, separately.** If either one is lost, restore becomes much harder. The archive is still sensitive even though it is encrypted.

---

## Restore

Restore also requires an admin session. It's a destructive operation: it replaces your current database and encryption key entirely with the backup contents. Everything you have now is gone. Use it to recover from a corrupted or lost database.

```bash
curl -X POST http://localhost:3500/api/backup/restore \
  -H "Authorization: Bearer <admin-session>" \
  -F "backup=@agent-core-backup.zip.enc" \
  -F "backup_key=<one-time-backup-key>" \
  -F "mode=replace_all"
```

`mode` is optional and defaults to `replace_all`, the only supported mode. Any other value, including the removed `merge`, is rejected with `400 INVALID_MODE`.

A restore holds the database still while it swaps: requests already in flight are allowed to finish, and requests arriving during the swap get a `503` with a `Retry-After` rather than a connection to a file that is about to be replaced. If in-flight work does not finish within the drain window, the restore refuses and changes nothing. **This gate is per process**, so a restore is only safe on the supported single-worker deployment; see [Configuration](configuration.md#workers).

Before replacing anything, Agent Core always:

1. Decrypts the archive if you provided a backup key
2. Validates the ZIP structure: must contain `agent-core.db`, `credential.key`, and `manifest.json`
3. Verifies SHA-256 checksums match what the manifest says
4. Creates timestamped copies of your current database and key material in `data/backups/`

If any validation step fails, your existing data is untouched.

---

## Partial exports

These exports are scoped to what the caller has access to, and never include raw credential values.

### Memory

```bash
# One record per line (easier to process programmatically)
curl "http://localhost:3500/api/backup/export/memory?fmt=jsonl" \
  -H "Authorization: Bearer <session-or-agent-key>" \
  -o memory.jsonl

# Spreadsheet-friendly
curl "http://localhost:3500/api/backup/export/memory?fmt=csv" \
  -H "Authorization: Bearer <session-or-agent-key>" \
  -o memory.csv
```

### Credential metadata

Exports names, scopes, labels, types, and reference names; everything except the actual secrets.

```bash
curl http://localhost:3500/api/backup/export/credentials \
  -H "Authorization: Bearer <session-or-agent-key>" \
  -o credentials-metadata.json
```

### Audit log

Admin only. Returns CSV.

```bash
curl "http://localhost:3500/api/backup/export/audit?fmt=csv" \
  -H "Authorization: Bearer <admin-session>" \
  -o audit.csv
```

---

## Health checks

Run this to verify Agent Core's operational state; useful after a restore or if something seems off:

```bash
curl http://localhost:3500/api/backup/startup-checks \
  -H "Authorization: Bearer <admin-session>"
```

Checks include:

- Data directory is writable
- SQLite has FTS5 support and the memory FTS table can rebuild
- Current encryption key file exists
- Broker credential file exists

---

## Maintenance

Maintenance runs automatically in-process (default: every 60 minutes, first run 5 minutes after startup; see `AGENT_CORE_MAINTENANCE_INTERVAL_MINUTES` in [configuration](configuration.md)). You can also trigger it manually from **Settings → Backup & Restore** or the API:

```bash
curl -X POST http://localhost:3500/api/backup/maintenance \
  -H "Authorization: Bearer <admin-session>"
```

What it does:

- Marks any activity record `stale` if its heartbeat has exceeded the stale threshold
- Hard-deletes `scratchpad` memory records older than `scratchpad_retention_days` in system settings (default: 7 days)
- Hard-deletes any memory record whose `expires_at` TTL has passed
- Hard-deletes retracted and superseded memory records `retracted_retention_days` after they stopped being active (default: 30 days), measured from retraction/supersession time, not creation time, so a fresh retraction always gets its full grace window to be restored first
- Verifies anchored facts when verification is enabled
- Generates bounded clean-up proposals when `consolidation_scan_enabled` is on

Deleted records can't be recovered. Active `fact`, `preference`, and `decision` records without an `expires_at` are never deleted by maintenance; consolidation only queues proposals for human review. The last run's time, trigger, and per-step counts—including proposals generated and skipped at cap—are shown on the Settings page and at `GET /api/backup/maintenance/status`.
