import hashlib
import io
import json
import os
import secrets
import shutil
import sqlite3
import zipfile
from datetime import timedelta
from typing import Optional

from cryptography.fernet import Fernet

from app.config import settings
from app.database import get_db
from app.time_utils import parse_utc_datetime, utc_now, utc_now_iso


def _configured_env_key_bytes() -> bytes | None:
    if settings.ENCRYPTION_KEY and settings.ENCRYPTION_KEY.lower() != "auto":
        key = settings.ENCRYPTION_KEY.encode()
        Fernet(key)
        return key
    return None


def compute_sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _keyring_path() -> str:
    return str(settings.data_dir / "vault.keyring")


def build_backup_manifest(
    db_path: str,
    vault_key_path: str,
    exported_by: str,
    agent_core_version: str,
) -> dict:
    checksums = {}
    if os.path.exists(db_path):
        checksums["agent-core.db"] = compute_sha256(db_path)

    env_key = _configured_env_key_bytes()
    if env_key is not None:
        checksums["vault.key"] = hashlib.sha256(env_key).hexdigest()
    elif os.path.exists(vault_key_path):
        checksums["vault.key"] = compute_sha256(vault_key_path)
    if os.path.exists(_keyring_path()):
        checksums["vault.keyring"] = compute_sha256(_keyring_path())

    return {
        "agent_core_version": agent_core_version,
        "exported_at": utc_now_iso(),
        "exported_by": exported_by,
        "files": checksums,
    }


def build_backup_zip(db_path: str, vault_key_path: str, exported_by: str, agent_core_version: str = "1.0.0") -> io.BytesIO:
    manifest = build_backup_manifest(db_path, vault_key_path, exported_by, agent_core_version)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.exists(db_path):
            zf.write(db_path, arcname="agent-core.db")
        env_key = _configured_env_key_bytes()
        if env_key is not None:
            zf.writestr("vault.key", env_key)
        elif os.path.exists(vault_key_path):
            zf.write(vault_key_path, arcname="vault.key")
        if os.path.exists(_keyring_path()):
            zf.write(_keyring_path(), arcname="vault.keyring")
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

    buf.seek(0)
    return buf


def parse_manifest(data: dict) -> tuple[bool, str]:
    required = ["agent_core_version", "exported_at", "exported_by", "files"]
    for field in required:
        if field not in data:
            return False, f"Missing field: {field}"

    if not isinstance(data.get("files"), dict):
        return False, "files must be a dict"

    return True, ""


def validate_manifest(data: dict) -> tuple[bool, str]:
    ok, msg = parse_manifest(data)
    if not ok:
        return False, msg

    exported_at = data.get("exported_at", "")
    try:
        dt = parse_utc_datetime(exported_at)
    except ValueError:
        return False, "Invalid exported_at timestamp"

    max_age = timedelta(days=365 * 5)
    if utc_now() - dt > max_age:
        return False, f"Backup is older than {max_age.days} days"

    return True, ""


def _read_validated_backup(zip_bytes: io.BytesIO) -> tuple[bool, str, dict, dict[str, bytes]]:
    allowed_zip_entries = {"agent-core.db", "vault.key", "vault.keyring", "manifest.json"}

    try:
        zip_bytes.seek(0)
        with zipfile.ZipFile(zip_bytes, "r") as zf:
            names = set(zf.namelist())
            unexpected = names - allowed_zip_entries
            if unexpected:
                return False, f"Unexpected file in archive: {sorted(unexpected)[0]}", {}, {}
            if "manifest.json" not in names:
                return False, "Missing manifest.json", {}, {}

            manifest_data = json.loads(zf.read("manifest.json"))
            extracted = {
                name: zf.read(name)
                for name in ("agent-core.db", "vault.key", "vault.keyring")
                if name in names
            }
    except Exception:
        return False, "Invalid archive", {}, {}

    ok, msg = validate_manifest(manifest_data)
    if not ok:
        return False, msg, {}, {}

    checksums = manifest_data.get("files", {})
    required_files = {"agent-core.db", "vault.key"}
    allowed_files = {"agent-core.db", "vault.key", "vault.keyring"}
    checksum_keys = set(checksums.keys())
    if not required_files.issubset(checksum_keys):
        return False, f"manifest.files must include {required_files}, got {checksum_keys}", {}, {}
    unexpected_files = checksum_keys - allowed_files
    if unexpected_files:
        return False, f"Unexpected file in manifest: {sorted(unexpected_files)[0]}", {}, {}

    for fname, expected_sha in checksums.items():
        if fname not in extracted:
            return False, f"manifest references {fname} but it is not in the archive", {}, {}
        actual_sha = hashlib.sha256(extracted[fname]).hexdigest()
        if actual_sha != expected_sha:
            return False, f"{fname} checksum mismatch; archive may be tampered", {}, {}

    return True, "", manifest_data, extracted


def _backup_existing_file(path: str, backup_dir: str, timestamp: str, suffix: str) -> None:
    if os.path.exists(path):
        backup_path = os.path.join(backup_dir, f"{os.path.basename(path)}.{timestamp}.{suffix}")
        shutil.copy2(path, backup_path)


def _effective_vault_key(vault_key_path: str, fallback: bytes | None = None) -> bytes:
    key = _configured_env_key_bytes()
    if key is not None:
        return key
    if os.path.exists(vault_key_path):
        with open(vault_key_path, "rb") as f:
            key = f.read()
        Fernet(key)
        return key
    if fallback:
        Fernet(fallback)
        return fallback
    key = Fernet.generate_key()
    with open(vault_key_path, "wb") as f:
        f.write(key)
    os.chmod(vault_key_path, 0o600)
    return key


def _reencrypt_vault_rows_in_db_bytes(db_bytes: bytes, old_key: bytes, new_key: bytes, timestamp: str) -> bytes:
    if old_key == new_key:
        return db_bytes

    tmp_path = str(settings.data_dir / f"restore-reencrypt-{timestamp}-{secrets.token_hex(4)}.db")
    with open(tmp_path, "wb") as f:
        f.write(db_bytes)

    old_fernet = Fernet(old_key)
    new_fernet = Fernet(new_key)
    try:
        con = sqlite3.connect(tmp_path)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT id, value_encrypted FROM vault_entries").fetchall()
        for row in rows:
            plaintext = old_fernet.decrypt(row["value_encrypted"].encode())
            con.execute(
                "UPDATE vault_entries SET value_encrypted = ? WHERE id = ?",
                (new_fernet.encrypt(plaintext).decode(), row["id"]),
            )
        con.commit()
        con.close()
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        try:
            con.close()
        except Exception:
            pass
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _row_dict(row) -> dict:
    return dict(row)


def _table_columns(conn, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _insert_missing_rows(
    current_con,
    backup_con,
    table: str,
    transform=None,
) -> int:
    current_cols = _table_columns(current_con, table)
    backup_cols = _table_columns(backup_con, table)
    insert_cols = [c for c in current_cols if c in backup_cols]
    if "id" not in insert_cols:
        return 0

    inserted = 0
    quoted_cols = ",".join(insert_cols)
    placeholders = ",".join(["?" for _ in insert_cols])

    for row in backup_con.execute(f"SELECT {quoted_cols} FROM {table}").fetchall():
        row_dict = _row_dict(row)
        existing = current_con.execute(
            f"SELECT id FROM {table} WHERE id = ?",
            (row_dict["id"],),
        ).fetchone()
        if existing is not None:
            continue
        if transform:
            row_dict = transform(row_dict)
        values = [row_dict.get(c) for c in insert_cols]
        current_con.execute(
            f"INSERT INTO {table} ({quoted_cols}) VALUES ({placeholders})",
            values,
        )
        inserted += 1

    return inserted


def merge_restore_from_zip(
    zip_bytes: io.BytesIO,
    db_path: str,
    vault_key_path: str,
) -> tuple[bool, str, dict]:
    ok, msg, manifest_data, extracted = _read_validated_backup(zip_bytes)
    if not ok:
        return False, msg, {}

    backup_dir = settings.data_dir / "backups"
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = utc_now().strftime("%Y%m%d_%H%M%S")

    db_temp = db_path + f".{timestamp}.merge.tmp"
    backup_con = None
    try:
        with open(db_temp, "wb") as f:
            f.write(extracted["agent-core.db"])

        backup_con = sqlite3.connect(db_temp)
        backup_con.row_factory = sqlite3.Row

        backup_key = extracted["vault.key"]
        current_key = _effective_vault_key(vault_key_path, fallback=backup_key)
        if not os.path.exists(vault_key_path) and settings.ENCRYPTION_KEY.lower() == "auto":
            with open(vault_key_path, "wb") as f:
                f.write(current_key)
            os.chmod(vault_key_path, 0o600)

        backup_fernet = Fernet(backup_key)
        current_fernet = Fernet(current_key)

        def normalize_vault_category(value: str | None) -> str:
            if value in ("token", "key"):
                return "api"
            if value == "endpoint":
                return "url"
            if value in ("api", "password", "url", "config", "other"):
                return value
            return "other"

        def transform_vault(row: dict) -> dict:
            row["value_type"] = normalize_vault_category(row.get("value_type"))
            if backup_key != current_key and row.get("value_encrypted"):
                plaintext = backup_fernet.decrypt(row["value_encrypted"].encode())
                row["value_encrypted"] = current_fernet.encrypt(plaintext).decode()
            return row

        inserted_counts = {}
        backup_dir_str = str(backup_dir)
        _backup_existing_file(db_path, backup_dir_str, timestamp, "merge-pre-db")
        _backup_existing_file(vault_key_path, backup_dir_str, timestamp, "merge-pre-key")

        with get_db() as current_con:
            current_con.execute("BEGIN IMMEDIATE")

            for table in ("users", "workspaces", "agents", "memory_records", "vault_entries", "agent_activity"):
                try:
                    inserted_counts[table] = _insert_missing_rows(
                        current_con,
                        backup_con,
                        table,
                        transform=transform_vault if table == "vault_entries" else None,
                    )
                except Exception:
                    inserted_counts[table] = 0

            try:
                inserted_counts["memory_embeddings"] = _insert_missing_rows(
                    current_con,
                    backup_con,
                    "memory_embeddings",
                )
            except Exception:
                inserted_counts["memory_embeddings"] = 0

            current_con.commit()
        backup_con.close()
    except Exception as e:
        try:
            backup_con.close()
        except Exception:
            pass
        if os.path.exists(db_temp):
            os.remove(db_temp)
        return False, "Merge restore failed", {}
    finally:
        if os.path.exists(db_temp):
            os.remove(db_temp)

    manifest_data["merge"] = {
        "conflict_behavior": "existing records kept; backup records with conflicting primary keys skipped",
        "vault_key_handling": "current effective vault key preserved; imported vault entries re-encrypted when backup key differs",
        "inserted_counts": inserted_counts,
    }
    return True, "", manifest_data


def restore_from_zip(
    zip_bytes: io.BytesIO,
    db_path: str,
    vault_key_path: str,
) -> tuple[bool, str, dict]:
    ok, msg, manifest_data, extracted = _read_validated_backup(zip_bytes)
    if not ok:
        return False, msg, {}

    backup_dir = settings.data_dir / "backups"
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = utc_now().strftime("%Y%m%d_%H%M%S")

    backup_key = extracted["vault.key"]
    effective_key = _configured_env_key_bytes() or backup_key
    extracted["agent-core.db"] = _reencrypt_vault_rows_in_db_bytes(
        extracted["agent-core.db"],
        backup_key,
        effective_key,
        timestamp,
    )

    def atomic_replace(src_bytes: bytes, dst_path: str, backup_suffix: str):
        tmp_path = dst_path + f".{timestamp}.tmp"
        with open(tmp_path, "wb") as f:
            f.write(src_bytes)
        if os.path.exists(dst_path):
            backup_path = os.path.join(backup_dir, f"{os.path.basename(dst_path)}.{backup_suffix}")
            shutil.copy2(dst_path, backup_path)
        os.replace(tmp_path, dst_path)

    atomic_replace(extracted["agent-core.db"], db_path, "db")
    if settings.ENCRYPTION_KEY and settings.ENCRYPTION_KEY.lower() != "auto":
        _backup_existing_file(vault_key_path, str(backup_dir), timestamp, "key")
    else:
        atomic_replace(effective_key, vault_key_path, "key")
        if "vault.keyring" in extracted:
            atomic_replace(extracted["vault.keyring"], _keyring_path(), "keyring")
        else:
            atomic_replace(json.dumps({"keys": [effective_key.decode()]}).encode(), _keyring_path(), "keyring")

    return True, "", manifest_data


def export_memory_jsonl(user_id: Optional[str] = None) -> io.StringIO:
    buf = io.StringIO()
    with get_db() as conn:
        query = "SELECT id, content, memory_class, scope, domain, topic, confidence, importance, source_kind, event_time, created_at, record_status FROM memory_records WHERE record_status = 'active'"
        params = []
        if user_id:
            query += " AND scope = ?"
            params.append(f"user:{user_id}")
        query += " ORDER BY created_at DESC"
        rows = conn.execute(query, params)
        for row in rows:
            record = {
                "id": row["id"],
                "content": row["content"],
                "memory_class": row["memory_class"],
                "scope": row["scope"],
                "domain": row["domain"],
                "topic": row["topic"],
                "confidence": row["confidence"],
                "importance": row["importance"],
                "source_kind": row["source_kind"],
                "event_time": row["event_time"],
                "created_at": row["created_at"],
                "record_status": row["record_status"],
            }
            buf.write(json.dumps(record) + "\n")
    buf.seek(0)
    return buf


def export_memory_csv(user_id: Optional[str] = None) -> io.StringIO:
    import csv

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "content", "memory_class", "scope", "domain", "topic",
        "confidence", "importance", "source_kind", "event_time", "created_at", "record_status",
    ])

    with get_db() as conn:
        query = "SELECT id, content, memory_class, scope, domain, topic, confidence, importance, source_kind, event_time, created_at, record_status FROM memory_records WHERE record_status = 'active'"
        params = []
        if user_id:
            query += " AND scope = ?"
            params.append(f"user:{user_id}")
        query += " ORDER BY created_at DESC"
        rows = conn.execute(query, params)
        for row in rows:
            writer.writerow([
                row["id"], row["content"], row["memory_class"], row["scope"],
                row["domain"], row["topic"], row["confidence"], row["importance"],
                row["source_kind"], row["event_time"], row["created_at"], row["record_status"],
            ])

    buf.seek(0)
    return buf


def export_vault_metadata(user_id: Optional[str] = None) -> io.StringIO:
    buf = io.StringIO()
    with get_db() as conn:
        query = "SELECT id, scope, name, label, value_type, reference_name, created_at FROM vault_entries"
        params = []
        if user_id:
            query += " WHERE scope = ?"
            params.append(f"user:{user_id}")
        rows = conn.execute(query, params)
        records = []
        for row in rows:
            records.append({
                "id": row["id"],
                "scope": row["scope"],
                "name": row["name"],
                "label": row["label"],
                "value_type": row["value_type"],
                "reference_name": row["reference_name"],
                "created_at": row["created_at"],
            })
    json.dump(records, buf, indent=2)
    buf.seek(0)
    return buf


def export_audit_csv(
    actor_type: Optional[str] = None,
    actor_id: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 10000,
) -> io.StringIO:
    import csv
    from app.services import audit_service

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp", "actor_type", "actor_id", "action", "resource_type", "resource_id", "result", "details_json", "ip_address"])

    events = audit_service.query_events(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        limit=limit,
    )
    for e in events:
        writer.writerow([
            e.get("timestamp", ""),
            e.get("actor_type", ""),
            e.get("actor_id", ""),
            e.get("action", ""),
            e.get("resource_type", ""),
            e.get("resource_id", ""),
            e.get("result", ""),
            json.dumps(e.get("details")),
            e.get("ip_address", ""),
        ])

    buf.seek(0)
    return buf


def run_startup_checks() -> list[dict]:
    issues = []

    if os.access(settings.data_dir, os.W_OK):
        issues.append({"check": "data_dir_writable", "status": "OK", "message": f"{settings.data_dir} is writable"})
    else:
        issues.append({"check": "data_dir_writable", "status": "FAIL", "message": f"{settings.data_dir} is not writable"})

    import sqlite3
    try:
        con = sqlite3.connect(settings.db_path)
        compile_opts = {row[0] for row in con.execute("PRAGMA compile_options").fetchall()}
        if "ENABLE_FTS5" not in compile_opts:
            issues.append({"check": "sqlite_fts5", "status": "FAIL", "message": "FTS5 not compiled in"})
        else:
            con.execute("INSERT INTO memory_records_fts(memory_records_fts) VALUES('rebuild')").fetchall()
            con.commit()
            issues.append({"check": "sqlite_fts5", "status": "OK", "message": "FTS5 available and healthy"})
        con.close()
    except Exception:
        issues.append({"check": "sqlite_fts5", "status": "FAIL", "message": "FTS5 check failed"})

    if os.path.exists(settings.vault_key_path):
        issues.append({"check": "encryption_key", "status": "OK", "message": "vault.key present"})
    else:
        issues.append({"check": "encryption_key", "status": "FAIL", "message": "vault.key not found"})

    from app.services.broker_service import get_broker_credential_hash
    if get_broker_credential_hash():
        issues.append({"check": "broker_credential", "status": "OK", "message": "Broker credential present"})
    else:
        issues.append({"check": "broker_credential", "status": "FAIL", "message": "No broker credential found"})

    return issues


def run_scheduled_maintenance() -> dict:
    from app.services.activity_service import mark_stale_activities
    from app.config import settings as app_settings

    stale_count = mark_stale_activities()

    retention_days = 7
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM system_settings WHERE key = 'scratchpad_retention_days'").fetchone()
            if row:
                retention_days = int(row["value"])
    except Exception:
        pass

    cutoff = utc_now() - timedelta(days=retention_days)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, created_at FROM memory_records WHERE memory_class = 'scratchpad' AND record_status = 'active'"
        ).fetchall()
        delete_ids = [
            row["id"]
            for row in rows
            if row["created_at"] and parse_utc_datetime(row["created_at"]) < cutoff
        ]
        if delete_ids:
            cursor = conn.execute(
                f"DELETE FROM memory_records WHERE id IN ({','.join('?' for _ in delete_ids)})",
                delete_ids,
            )
        else:
            cursor = None
        conn.commit()
        pruned = cursor.rowcount if cursor else 0

    from app.services import audit_service
    if pruned > 0:
        audit_service.write_event(
            actor_type="system",
            actor_id="maintenance",
            action="scratchpad_pruned",
            result="success",
            details={"deleted_count": pruned, "retention_days": retention_days},
        )

    return {"stale_activities_marked": stale_count, "scratchpad_pruned": pruned}
