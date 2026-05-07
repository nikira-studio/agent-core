# Backup and Restore

Agent Core stores everything in three files: `data/agent-core.db` (the database), `data/vault.key` (the current encryption key), and `data/vault.keyring` (JSON file with all historical keys). A full backup contains all three. Without the current key, you cannot decrypt vault entries from the database.

---

## Full Backup

Full backup requires an admin session and a valid OTP code.

```bash
curl -X POST http://localhost:3500/api/backup/export \
  -H "Authorization: Bearer <admin-session>" \
  -H "Content-Type: application/json" \
  -d '{"otp_code": "123456"}' \
  -o agent-core-backup.zip
```

The ZIP contains:
- `agent-core.db` — the full SQLite database
- `vault.key` — the current Fernet encryption key
- `vault.keyring` — JSON file containing the full keyring (all historical keys)
- `manifest.json` — version, export timestamp, exporting user, and SHA-256 checksums for all files

**The backup ZIP is sensitive.** It contains everything needed to restore your vault. Store it in an encrypted location. Do not commit it to version control.

You can also trigger a backup from the dashboard: **Settings → Backup → Download Backup**.

---

## Restore

Restore requires an admin session and a valid OTP code. It's a destructive operation.

```bash
curl -X POST http://localhost:3500/api/backup/restore \
  -H "Authorization: Bearer <admin-session>" \
  -F "backup=@agent-core-backup.zip" \
  -F "otp_code=123456" \
  -F "mode=replace_all"
```

**Mode options:**

- `replace_all` — replaces your current database and vault key with the backup contents. All existing records are lost.
- `merge` — adds records from the backup that don't conflict with existing records. Existing records (by primary key) are preserved. Vault entries from the backup are re-encrypted with the current vault key if the backup's key differs from the current one.

Before replacing anything, Agent Core:
1. Validates the ZIP structure (must contain `agent-core.db`, `vault.key`, and `manifest.json`).
2. Verifies SHA-256 checksums match the manifest.
3. Creates timestamped copies of the current database and vault key in `data/backups/`.

If validation fails, the existing data is untouched.

### Merge Restore Details

- Records with primary keys that already exist in the current DB are skipped (the current record wins).
- Memory records, agents, workspaces, and vault entries are merged independently.
- Vault entries imported via merge are re-encrypted with the current active key if the backup key differs, so they remain decryptable.
- Merge preserves the current `vault.key` — it does not adopt the backup's key.
- Merge is useful for consolidating two independent Agent Core installs without losing existing work.

---

## Partial Exports

These exports are scoped to the authenticated caller and never include raw vault values.

### Memory Export

```bash
# JSONL format (one record per line)
curl "http://localhost:3500/api/backup/export/memory?fmt=jsonl" \
  -H "Authorization: Bearer <session-or-agent-key>" \
  -o memory.jsonl

# CSV format
curl "http://localhost:3500/api/backup/export/memory?fmt=csv" \
  -H "Authorization: Bearer <session-or-agent-key>" \
  -o memory.csv
```

### Vault Metadata Export

Exports vault entry names, scopes, labels, types, and reference names. Never exports raw secret values.

```bash
curl http://localhost:3500/api/backup/export/vault \
  -H "Authorization: Bearer <session-or-agent-key>" \
  -o vault-metadata.json
```

### Audit Log Export

Admin only. Returns CSV.

```bash
curl "http://localhost:3500/api/backup/export/audit?fmt=csv" \
  -H "Authorization: Bearer <admin-session>" \
  -o audit.csv
```

---

## Startup Health Checks

Run this to verify Agent Core's operational state:

```bash
curl http://localhost:3500/api/backup/startup-checks \
  -H "Authorization: Bearer <admin-session>"
```

Checks include:
- Data directory is writable
- SQLite has FTS5 support and the memory FTS table can rebuild
- `vault.key` exists
- Broker credential exists

---

## Maintenance

Maintenance cleans up stale activity records and prunes old scratchpad memory. Run it periodically or on a schedule.

```bash
curl -X POST http://localhost:3500/api/backup/maintenance \
  -H "Authorization: Bearer <admin-session>"
```

What it does:
- Marks any activity record `stale` if its heartbeat has exceeded the stale threshold.
- Hard-deletes `scratchpad` memory records older than `scratchpad_retention_days` in system settings (default: 7 days). A count of deleted records is written to the audit log.

Pruned scratchpad records cannot be recovered. All other memory classes are unaffected.
