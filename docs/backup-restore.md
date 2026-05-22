# Backup and Restore

Agent Core's state lives in three files: the database (`data/agent-core.db`), the current encryption key, and the key history file. A full backup export wraps those files in an encrypted archive. You keep the encrypted archive and the one-time backup key separately. If you restore the archive without the matching backup key, Agent Core cannot decrypt it.

---

## Full Backup

A full backup requires an admin session. This is intentional — the encrypted archive and backup key together contain enough to restore all your credentials, so both are treated as sensitive.

```bash
curl -X POST http://localhost:3500/api/backup/export \
  -H "Authorization: Bearer <admin-session>" \
  -H "Content-Type: application/json" \
  -o agent-core-backup.zip.enc
```

The exported archive contains the same backup contents as before, but encrypted with a separate one-time backup key. The dashboard shows that key once after export, and the REST response also includes it in the `X-Agent-Core-Backup-Key` header.

The underlying backup archive contains:

- `agent-core.db` — the full database (memory, agents, credentials metadata, activity, connector types, bindings, and execution history)
- `credential.key` — the current encryption key
- `credential.keyring` — all historical keys (needed to decrypt entries from before a key rotation)
- `manifest.json` — version, timestamp, and SHA-256 checksums for everything in the archive

You can also trigger a backup from the dashboard: **Settings → Backup → Export Encrypted Backup**.

**Store the encrypted archive and the backup key securely, separately.** If either one is lost, restore becomes much harder. The archive is still sensitive even though it is encrypted.

---

## Restore

Restore also requires an admin session. It's a destructive operation, so read the modes before choosing.

```bash
curl -X POST http://localhost:3500/api/backup/restore \
  -H "Authorization: Bearer <admin-session>" \
  -F "backup=@agent-core-backup.zip.enc" \
  -F "backup_key=<one-time-backup-key>" \
  -F "mode=replace_all"
```

**Mode options:**

- **`replace_all`** — replaces your current database and encryption key entirely with the backup contents. Everything you have now is gone. Use this to recover from a corrupted or lost database.
- **`merge`** — adds records from the backup that don't conflict with existing records. If a record with the same primary key already exists, the current version wins. Useful for combining two independent Agent Core installs or bringing back specific records without overwriting what you have.

Before replacing anything, Agent Core always:

1. Decrypts the archive if you provided a backup key
2. Validates the ZIP structure — must contain `agent-core.db`, `credential.key`, and `manifest.json`
3. Verifies SHA-256 checksums match what the manifest says
4. Creates timestamped copies of your current database and key material in `data/backups/`

If any validation step fails, your existing data is untouched.

### Merge Details

A merge restore is more surgical:

- Records with primary keys that already exist in the current database are skipped — current wins
- Memory records, agents, workspaces, credential entries, connector types, connector bindings, and execution history are merged independently
- Credential entries from the backup are automatically re-encrypted with your current key if the backup key differs, so they remain readable
- The merge does not adopt the backup's encryption key — your current key stays in place

---

## Partial Exports

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

### Credential Metadata

Exports names, scopes, labels, types, and reference names — everything except the actual secrets.

```bash
curl http://localhost:3500/api/backup/export/credentials \
  -H "Authorization: Bearer <session-or-agent-key>" \
  -o credentials-metadata.json
```

### Audit Log

Admin only. Returns CSV.

```bash
curl "http://localhost:3500/api/backup/export/audit?fmt=csv" \
  -H "Authorization: Bearer <admin-session>" \
  -o audit.csv
```

---

## Health Checks

Run this to verify Agent Core's operational state — useful after a restore or if something seems off:

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

Maintenance cleans up stale activity records and prunes old scratchpad memory. Run it periodically or on a schedule.

```bash
curl -X POST http://localhost:3500/api/backup/maintenance \
  -H "Authorization: Bearer <admin-session>"
```

What it does:

- Marks any activity record `stale` if its heartbeat has exceeded the stale threshold
- Hard-deletes `scratchpad` memory records older than `scratchpad_retention_days` in system settings (default: 7 days)

Pruned scratchpad records can't be recovered. All other memory classes (`fact`, `preference`, `decision`) are untouched.
