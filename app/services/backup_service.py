import hashlib
import io
import json
import logging
import os
import pathlib
import secrets
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import timedelta
from typing import Optional

from cryptography.fernet import Fernet

from app.branding import APP_VERSION, DB_FILENAME, MANIFEST_VERSION_KEY
from app.config import settings
from app.database import DatabaseUnavailable, exclusive_access, get_db

# Shared with credential key rotation and restoration; see the definition for
# why these four operations cannot interleave.
from app.security.encryption import KEY_OPERATION_LOCK, write_private_key
from app.time_utils import parse_utc_datetime, utc_now, utc_now_iso


logger = logging.getLogger(__name__)


def _system_setting_int(key: str, default: int) -> int:
    """Read an integer system setting, falling back to the default on anything odd.

    Maintenance must never fail because a setting row is missing or malformed —
    a sweep that refuses to run is how the logs grew to 142 MB in the first place.
    """
    from app.services import system_settings_service

    return system_settings_service.read_int(key, default)


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
    return str(settings.data_dir / "credential.keyring")


def _snapshot_database(db_path: str, destination: str) -> None:
    """Copy the database through SQLite rather than off the filesystem.

    The database runs in WAL mode, so committed data lives in `-wal` until a
    checkpoint folds it back. Archiving the `.db` file alone captures whatever
    happened to have been checkpointed — a backup taken right after a write can
    be missing the write, and in the worst case missing a whole table. The
    backup API reads through a connection, so what lands is a consistent
    snapshot including everything committed.
    """
    source = sqlite3.connect(db_path)
    try:
        target = sqlite3.connect(destination)
        try:
            with target:
                source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _revoke_consumer_grants(db_path: str) -> None:
    """Ensure archived/restored databases cannot carry usable consumer grants."""
    con = sqlite3.connect(db_path)
    try:
        consumer_grants_exist = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='credential_consumer_grants'"
        ).fetchone()
        if consumer_grants_exist:
            con.execute(
                "UPDATE credential_consumer_grants SET revoked_at=COALESCE(revoked_at, ?)",
                (utc_now_iso(),),
            )
        con.commit()
    finally:
        con.close()


def build_backup_manifest(
    db_path: str,
    credential_key_path: str,
    exported_by: str,
    app_version: str,
    key_bytes: Optional[bytes] = None,
    keyring_bytes: Optional[bytes] = None,
) -> dict:
    checksums = {}
    if os.path.exists(db_path):
        checksums[DB_FILENAME] = compute_sha256(db_path)

    # Callers that already hold the key bytes pass them in, so the manifest
    # describes exactly what is archived rather than whatever is on disk by the
    # time the archive is written.
    if key_bytes is None and keyring_bytes is None:
        env_key = _configured_env_key_bytes()
        if env_key is not None:
            key_bytes = env_key
        elif os.path.exists(credential_key_path):
            key_bytes = pathlib.Path(credential_key_path).read_bytes()
        if os.path.exists(_keyring_path()):
            keyring_bytes = pathlib.Path(_keyring_path()).read_bytes()

    if key_bytes is not None:
        checksums["credential.key"] = hashlib.sha256(key_bytes).hexdigest()
    if keyring_bytes is not None:
        checksums["credential.keyring"] = hashlib.sha256(keyring_bytes).hexdigest()

    return {
        MANIFEST_VERSION_KEY: app_version,
        "exported_at": utc_now_iso(),
        "exported_by": exported_by,
        "files": checksums,
    }


def build_backup_zip(
    db_path: str,
    credential_key_path: str,
    exported_by: str,
    app_version: str = APP_VERSION,
) -> io.BytesIO:
    # Snapshot first, then describe and archive the snapshot: the manifest
    # checksum has to be of the bytes that actually go into the archive, not of
    # a live file that may move between hashing and reading.
    snapshot_dir = tempfile.mkdtemp(prefix="agent-core-backup-")
    snapshot_path = os.path.join(snapshot_dir, DB_FILENAME)
    try:
        # The key files are read once, under the lock, and both hashed and
        # archived from those same bytes. Hashing the path and then reading it
        # again leaves room for a rotation in between, which yields an archive
        # whose manifest describes a key the payload does not contain.
        with KEY_OPERATION_LOCK:
            if os.path.exists(db_path):
                _snapshot_database(db_path, snapshot_path)
                _revoke_consumer_grants(snapshot_path)

            env_key = _configured_env_key_bytes()
            if env_key is not None:
                key_bytes = env_key
            elif os.path.exists(credential_key_path):
                key_bytes = pathlib.Path(credential_key_path).read_bytes()
            else:
                key_bytes = None
            keyring_bytes = (
                pathlib.Path(_keyring_path()).read_bytes()
                if os.path.exists(_keyring_path())
                else None
            )

        manifest = build_backup_manifest(
            snapshot_path,
            credential_key_path,
            exported_by,
            app_version,
            key_bytes=key_bytes,
            keyring_bytes=keyring_bytes,
        )

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            if os.path.exists(snapshot_path):
                zf.write(snapshot_path, arcname=DB_FILENAME)
            if key_bytes is not None:
                zf.writestr("credential.key", key_bytes)
            if keyring_bytes is not None:
                zf.writestr("credential.keyring", keyring_bytes)
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))

        buf.seek(0)
        return buf
    finally:
        shutil.rmtree(snapshot_dir, ignore_errors=True)


def build_encrypted_backup_package(
    db_path: str,
    credential_key_path: str,
    exported_by: str,
    app_version: str = APP_VERSION,
) -> tuple[io.BytesIO, bytes]:
    """
    Build the regular backup ZIP, then encrypt the archive with a one-time Fernet key.
    The returned key must be saved separately to restore the encrypted archive.
    """
    archive_buf = build_backup_zip(
        db_path,
        credential_key_path,
        exported_by,
        app_version=app_version,
    )
    backup_key = Fernet.generate_key()
    encrypted_bytes = Fernet(backup_key).encrypt(archive_buf.getvalue())
    return io.BytesIO(encrypted_bytes), backup_key


def decrypt_backup_package(backup_bytes: bytes, backup_key: bytes) -> io.BytesIO:
    plaintext = Fernet(backup_key).decrypt(backup_bytes)
    return io.BytesIO(plaintext)


def parse_manifest(data: dict) -> tuple[bool, str]:
    version_key = (
        MANIFEST_VERSION_KEY if MANIFEST_VERSION_KEY in data else "agent_core_version"
    )
    required = [version_key, "exported_at", "exported_by", "files"]
    for field in required:
        if field not in data:
            return False, f"Missing field: {MANIFEST_VERSION_KEY}"

    if not isinstance(data.get("files"), dict):
        return False, "files must be a dict"

    if version_key != MANIFEST_VERSION_KEY and version_key in data:
        data[MANIFEST_VERSION_KEY] = data[version_key]

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


def _read_validated_backup(
    zip_bytes: io.BytesIO,
) -> tuple[bool, str, dict, dict[str, bytes]]:
    allowed_zip_entries = {
        DB_FILENAME,
        "credential.key",
        "credential.keyring",
        "manifest.json",
    }

    try:
        zip_bytes.seek(0)
        with zipfile.ZipFile(zip_bytes, "r") as zf:
            names = set(zf.namelist())
            unexpected = names - allowed_zip_entries
            if unexpected:
                return (
                    False,
                    f"Unexpected file in archive: {sorted(unexpected)[0]}",
                    {},
                    {},
                )
            if "manifest.json" not in names:
                return False, "Missing manifest.json", {}, {}

            manifest_data = json.loads(zf.read("manifest.json"))
            extracted = {
                name: zf.read(name)
                for name in (DB_FILENAME, "credential.key", "credential.keyring")
                if name in names
            }
    except Exception:
        return False, "Invalid archive", {}, {}

    ok, msg = validate_manifest(manifest_data)
    if not ok:
        return False, msg, {}, {}

    checksums = manifest_data.get("files", {})
    required_files = {DB_FILENAME, "credential.key"}
    allowed_files = {DB_FILENAME, "credential.key", "credential.keyring"}
    checksum_keys = set(checksums.keys())
    if not required_files.issubset(checksum_keys):
        return (
            False,
            f"manifest.files must include {required_files}, got {checksum_keys}",
            {},
            {},
        )
    unexpected_files = checksum_keys - allowed_files
    if unexpected_files:
        return (
            False,
            f"Unexpected file in manifest: {sorted(unexpected_files)[0]}",
            {},
            {},
        )

    for fname, expected_sha in checksums.items():
        if fname not in extracted:
            return (
                False,
                f"manifest references {fname} but it is not in the archive",
                {},
                {},
            )
        actual_sha = hashlib.sha256(extracted[fname]).hexdigest()
        if actual_sha != expected_sha:
            return False, f"{fname} checksum mismatch; archive may be tampered", {}, {}

    return True, "", manifest_data, extracted


def _backup_existing_file(
    path: str, backup_dir: str, timestamp: str, suffix: str
) -> None:
    if os.path.exists(path):
        backup_path = os.path.join(
            backup_dir, f"{os.path.basename(path)}.{timestamp}.{suffix}"
        )
        shutil.copy2(path, backup_path)
        os.chmod(backup_path, 0o600)


def _reencrypt_credential_rows_in_db_bytes(
    db_bytes: bytes, old_key: bytes, new_key: bytes, timestamp: str
) -> bytes:
    if old_key == new_key:
        return db_bytes

    tmp_path = str(
        settings.data_dir / f"restore-reencrypt-{timestamp}-{secrets.token_hex(4)}.db"
    )
    with open(tmp_path, "wb") as f:
        f.write(db_bytes)

    old_fernet = Fernet(old_key)
    new_fernet = Fernet(new_key)
    try:
        con = sqlite3.connect(tmp_path)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT id, value_encrypted FROM credentials").fetchall()
        for row in rows:
            plaintext = old_fernet.decrypt(row["value_encrypted"].encode())
            con.execute(
                "UPDATE credentials SET value_encrypted = ? WHERE id = ?",
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


def restore_from_zip(
    zip_bytes: io.BytesIO,
    db_path: str,
    credential_key_path: str,
    drain_timeout: float = 30.0,
) -> tuple[bool, str, dict]:
    """Replace this installation with the contents of a backup.

    Everything after validation happens with the database held still. A
    connection opened before the swap keeps its handle on the replaced file and
    would go on accepting writes that no later reader can see — a commit that
    reports success and is gone. So new work waits, in-flight work drains, and
    if it will not drain the restore refuses rather than running over it.
    """
    ok, msg, manifest_data, extracted = _read_validated_backup(zip_bytes)
    if not ok:
        return False, msg, {}

    try:
        # Lock order is fixed everywhere: key lock first, then the database
        # gate. Rotation takes the key lock and then opens connections, so a
        # restore that took the gate first would hold what rotation needs while
        # waiting for what rotation holds — a deadlock that resolves only when
        # one side times out, having already done half its work.
        with KEY_OPERATION_LOCK, exclusive_access(drain_timeout):
            return _restore_extracted(
                extracted, manifest_data, db_path, credential_key_path
            )
    except DatabaseUnavailable as exc:
        return False, str(exc), {}


def _restore_extracted(
    extracted: dict,
    manifest_data: dict,
    db_path: str,
    credential_key_path: str,
) -> tuple[bool, str, dict]:
    backup_dir = settings.data_dir / "backups"
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = utc_now().strftime("%Y%m%d_%H%M%S")

    backup_key = extracted["credential.key"]
    effective_key = _configured_env_key_bytes() or backup_key
    extracted[DB_FILENAME] = _reencrypt_credential_rows_in_db_bytes(
        extracted[DB_FILENAME],
        backup_key,
        effective_key,
        timestamp,
    )
    inert_path = str(
        settings.data_dir / f"restore-inert-{timestamp}-{secrets.token_hex(4)}.db"
    )
    try:
        with open(inert_path, "wb") as f:
            f.write(extracted[DB_FILENAME])
        _revoke_consumer_grants(inert_path)
        with open(inert_path, "rb") as f:
            extracted[DB_FILENAME] = f.read()
    finally:
        if os.path.exists(inert_path):
            os.remove(inert_path)

    def atomic_replace(src_bytes: bytes, dst_path: str, backup_suffix: str):
        tmp_path = dst_path + f".{timestamp}.tmp"
        write_private_key(tmp_path, src_bytes)
        if os.path.exists(dst_path):
            backup_path = os.path.join(
                backup_dir, f"{os.path.basename(dst_path)}.{backup_suffix}"
            )
            shutil.copy2(dst_path, backup_path)
            os.chmod(backup_path, 0o600)
        os.replace(tmp_path, dst_path)

    atomic_replace(extracted[DB_FILENAME], db_path, "db")
    # The old `-wal` and `-shm` describe the database we just replaced. Left in
    # place, SQLite either replays them over the restored file or refuses it
    # outright; either way the result is not the backup that was restored.
    for sidecar in (f"{db_path}-wal", f"{db_path}-shm"):
        if os.path.exists(sidecar):
            _backup_existing_file(sidecar, str(backup_dir), timestamp, "db-sidecar")
            os.remove(sidecar)
    if settings.ENCRYPTION_KEY and settings.ENCRYPTION_KEY.lower() != "auto":
        _backup_existing_file(credential_key_path, str(backup_dir), timestamp, "key")
    else:
        atomic_replace(effective_key, credential_key_path, "key")
        if "credential.keyring" in extracted:
            atomic_replace(extracted["credential.keyring"], _keyring_path(), "keyring")
        else:
            atomic_replace(
                json.dumps({"keys": [effective_key.decode()]}).encode(),
                _keyring_path(),
                "keyring",
            )

    # The key files on disk have changed; the ones this process is holding have
    # not. Without dropping them the process keeps decrypting with the previous
    # installation's key against the restored database, and reports success
    # while doing it. The mismatch only surfaces at the next resolve — or at the
    # next restart, whichever comes first.
    from app.security.encryption import reset_key_cache

    reset_key_cache()

    return True, "", manifest_data


def export_memory_jsonl(user_id: Optional[str] = None) -> io.StringIO:
    buf = io.StringIO()
    with get_db() as conn:
        query = "SELECT id, content, memory_class, scope, topic, confidence, importance, source_kind, created_at, record_status FROM memory_records WHERE record_status = 'active'"
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
                "topic": row["topic"],
                "confidence": row["confidence"],
                "importance": row["importance"],
                "source_kind": row["source_kind"],
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
    writer.writerow(
        [
            "id",
            "content",
            "memory_class",
            "scope",
            "topic",
            "confidence",
            "importance",
            "source_kind",
            "created_at",
            "record_status",
        ]
    )

    with get_db() as conn:
        query = "SELECT id, content, memory_class, scope, topic, confidence, importance, source_kind, created_at, record_status FROM memory_records WHERE record_status = 'active'"
        params = []
        if user_id:
            query += " AND scope = ?"
            params.append(f"user:{user_id}")
        query += " ORDER BY created_at DESC"
        rows = conn.execute(query, params)
        for row in rows:
            writer.writerow(
                [
                    row["id"],
                    row["content"],
                    row["memory_class"],
                    row["scope"],
                    row["topic"],
                    row["confidence"],
                    row["importance"],
                    row["source_kind"],
                    row["created_at"],
                    row["record_status"],
                ]
            )

    buf.seek(0)
    return buf


def export_credentials_metadata(user_id: Optional[str] = None) -> io.StringIO:
    buf = io.StringIO()
    with get_db() as conn:
        query = (
            "SELECT id, scope, name, label, reference_name, created_at FROM credentials"
        )
        params = []
        if user_id:
            query += " WHERE scope = ?"
            params.append(f"user:{user_id}")
        rows = conn.execute(query, params)
        records = []
        for row in rows:
            records.append(
                {
                    "id": row["id"],
                    "scope": row["scope"],
                    "name": row["name"],
                    "label": row["label"],
                    "reference_name": row["reference_name"],
                    "created_at": row["created_at"],
                }
            )
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
    writer.writerow(
        [
            "timestamp",
            "actor_type",
            "actor_id",
            "action",
            "resource_type",
            "resource_id",
            "result",
            "details_json",
            "ip_address",
        ]
    )

    events = audit_service.query_events(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        limit=limit,
    )
    for e in events:
        writer.writerow(
            [
                e.get("timestamp", ""),
                e.get("actor_type", ""),
                e.get("actor_id", ""),
                e.get("action", ""),
                e.get("resource_type", ""),
                e.get("resource_id", ""),
                e.get("result", ""),
                json.dumps(e.get("details")),
                e.get("ip_address", ""),
            ]
        )

    buf.seek(0)
    return buf


def run_startup_checks() -> list[dict]:
    issues = []

    if os.access(settings.data_dir, os.W_OK):
        issues.append(
            {
                "check": "data_dir_writable",
                "status": "OK",
                "message": f"{settings.data_dir} is writable",
            }
        )
    else:
        issues.append(
            {
                "check": "data_dir_writable",
                "status": "FAIL",
                "message": f"{settings.data_dir} is not writable",
            }
        )

    import sqlite3

    try:
        con = sqlite3.connect(settings.db_path)
        compile_opts = {
            row[0] for row in con.execute("PRAGMA compile_options").fetchall()
        }
        if "ENABLE_FTS5" not in compile_opts:
            issues.append(
                {
                    "check": "sqlite_fts5",
                    "status": "FAIL",
                    "message": "FTS5 not compiled in",
                }
            )
        else:
            con.execute(
                "INSERT INTO memory_records_fts(memory_records_fts) VALUES('rebuild')"
            ).fetchall()
            con.commit()
            issues.append(
                {
                    "check": "sqlite_fts5",
                    "status": "OK",
                    "message": "FTS5 available and healthy",
                }
            )
        con.close()
    except Exception:
        issues.append(
            {"check": "sqlite_fts5", "status": "FAIL", "message": "FTS5 check failed"}
        )

    if os.path.exists(settings.credential_key_path):
        issues.append(
            {
                "check": "encryption_key",
                "status": "OK",
                "message": "credential.key present",
            }
        )
    else:
        issues.append(
            {
                "check": "encryption_key",
                "status": "FAIL",
                "message": "credential.key not found",
            }
        )

    from app.services.broker_service import get_broker_credential_hash

    if get_broker_credential_hash():
        issues.append(
            {
                "check": "broker_credential",
                "status": "OK",
                "message": "Broker credential present",
            }
        )
    else:
        issues.append(
            {
                "check": "broker_credential",
                "status": "FAIL",
                "message": "No broker credential found",
            }
        )

    return issues


_MAINTENANCE_LOCK_KEY = "maintenance_lock_until"


def try_acquire_maintenance_lock(lease_seconds: int = 120) -> bool:
    """Best-effort lease lock so that multiple uvicorn worker processes sharing
    one SQLite file (production runs --workers 4 by default) don't all run the
    scheduled sweep on the same tick — each worker has its own independent
    scheduler task, since the lifespan hook runs once per process.

    Only meant to gate the *automatic* scheduled run (see
    scheduler_service._run_once). The manual "Run Maintenance" button is a
    deliberate human action, not redundant background noise, so it always
    runs regardless of this lock.
    """
    now_iso = utc_now_iso()
    lease_until_iso = (utc_now() + timedelta(seconds=lease_seconds)).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)",
            (_MAINTENANCE_LOCK_KEY, "1970-01-01T00:00:00+00:00"),
        )
        cursor = conn.execute(
            "UPDATE system_settings SET value = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE key = ? AND value < ?",
            (lease_until_iso, _MAINTENANCE_LOCK_KEY, now_iso),
        )
        conn.commit()
        return cursor.rowcount > 0


def run_scheduled_maintenance(triggered_by: str = "manual") -> dict:
    from app.services.activity_service import mark_stale_activities
    from app.services.workspace_sync_service import (
        run_maintenance as run_sync_maintenance,
    )

    stale_count = mark_stale_activities()
    sync_maintenance = run_sync_maintenance()

    retention_days = _system_setting_int("scratchpad_retention_days", 7)

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
            placeholders = ",".join("?" for _ in delete_ids)
            # memory_embeddings.record_id has a FK to memory_records(id) with
            # foreign_keys=ON (see database.py), so embedded scratchpad rows
            # must be cleared first or the DELETE below raises IntegrityError.
            # This mirrors the TTL sweep below, which already does this correctly.
            conn.execute(
                f"DELETE FROM memory_embeddings WHERE record_id IN ({placeholders})",
                delete_ids,
            )
            cursor = conn.execute(
                f"DELETE FROM memory_records WHERE id IN ({placeholders})",
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

    # Sweep expired memory records (opt-in via expires_at field)
    ttl_deleted = 0
    with get_db() as conn:
        expired_rows = conn.execute(
            "SELECT id FROM memory_records WHERE expires_at IS NOT NULL AND datetime(expires_at) < datetime('now')"
        ).fetchall()
        expired_ids = [row["id"] for row in expired_rows]
        if expired_ids:
            placeholders = ",".join("?" for _ in expired_ids)
            conn.execute(
                f"DELETE FROM memory_embeddings WHERE record_id IN ({placeholders})",
                expired_ids,
            )
            cursor = conn.execute(
                f"DELETE FROM memory_records WHERE id IN ({placeholders})",
                expired_ids,
            )
            ttl_deleted = cursor.rowcount
        conn.commit()

    if ttl_deleted > 0:
        audit_service.write_event(
            actor_type="system",
            actor_id="maintenance",
            action="memory_ttl_swept",
            result="success",
            details={"deleted_count": ttl_deleted},
        )

    # Hard-delete retracted records past their grace period. A retraction says
    # the record was wrong, so nothing reads it again. Superseded records are
    # kept: they are the history behind `as_of` searches and supersession
    # chains, and they were true for their validity window. status_changed_at
    # (not created_at) is the cutoff basis: an old
    # record retracted five minutes ago must still get its full grace period,
    # not be immediately purge-eligible because it happens to be old.
    retracted_retention_days = _system_setting_int("retracted_retention_days", 30)

    retracted_cutoff = utc_now() - timedelta(days=retracted_retention_days)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, status_changed_at FROM memory_records "
            "WHERE record_status = 'retracted'"
        ).fetchall()
        purge_ids = [
            row["id"]
            for row in rows
            if row["status_changed_at"]
            and parse_utc_datetime(row["status_changed_at"]) < retracted_cutoff
        ]
        if purge_ids:
            placeholders = ",".join("?" for _ in purge_ids)
            conn.execute(
                f"DELETE FROM memory_embeddings WHERE record_id IN ({placeholders})",
                purge_ids,
            )
            cursor = conn.execute(
                f"DELETE FROM memory_records WHERE id IN ({placeholders})",
                purge_ids,
            )
            purged = cursor.rowcount
        else:
            purged = 0
        conn.commit()

    if purged > 0:
        audit_service.write_event(
            actor_type="system",
            actor_id="maintenance",
            action="retracted_records_purged",
            result="success",
            details={
                "deleted_count": purged,
                "retention_days": retracted_retention_days,
            },
        )

    # Operational logs. Memory had retention from the start; the logs that
    # record what agents *did* had none, and the connector execution log alone
    # can grow to dominate the database. These
    # are diagnostics with a short useful life, not durable records.
    execution_retention_days = _system_setting_int("execution_log_retention_days", 30)
    webhook_retention_days = _system_setting_int("webhook_log_retention_days", 30)

    from app.services import connector_service

    executions_pruned = 0
    try:
        executions_pruned = connector_service.prune_executions(execution_retention_days)
    except Exception:
        logger.exception("Could not prune the connector execution log")

    webhook_deliveries_pruned = 0
    if webhook_retention_days > 0:
        try:
            with get_db() as conn:
                cursor = conn.execute(
                    "DELETE FROM webhook_delivery_log WHERE delivered_at < datetime('now', ?)",
                    (f"-{webhook_retention_days} days",),
                )
                conn.commit()
                webhook_deliveries_pruned = cursor.rowcount or 0
        except Exception:
            logger.exception("Could not prune the webhook delivery log")

    if executions_pruned or webhook_deliveries_pruned:
        audit_service.write_event(
            actor_type="system",
            actor_id="maintenance",
            action="operational_logs_pruned",
            result="success",
            details={
                "connector_executions": executions_pruned,
                "webhook_deliveries": webhook_deliveries_pruned,
                "execution_retention_days": execution_retention_days,
                "webhook_retention_days": webhook_retention_days,
            },
        )

    # Credential resolutions are high-volume operational audit data. Keep the
    # recent rows intact, compact aged successes transactionally, and preserve
    # security-relevant credential events individually for their longer window.
    credential_audit_retention = {"ok": False, "error_type": "NotRun"}
    try:
        from app.services import credential_audit_retention_service

        credential_audit_retention = {
            "ok": True,
            **credential_audit_retention_service.apply_retention(),
        }
    except Exception as exc:
        logger.exception("Could not apply credential audit retention")
        credential_audit_retention = {
            "ok": False,
            "error_type": type(exc).__name__,
        }

    # Verification runs unattended because it can: it only writes evidence on a
    # clean pass, and anything it cannot confirm becomes a proposal for a human
    # rather than an action. Capped per run so a large corpus is worked through
    # over several nights instead of hammering repos and services in one go —
    # oldest-confirmation-first, so attention goes where it is most overdue.
    verification = {"checked": 0, "verified": 0, "missing": 0}
    if _system_setting_int("verification_pass_enabled", 1):
        try:
            from app.services import verification_service

            outcome = verification_service.verify_scope(
                limit=_system_setting_int("verification_pass_limit", 50)
            )
            verification = {
                "checked": outcome["checked"],
                "verified": outcome["verified"],
                "missing": outcome["missing"],
            }
            if outcome["proposals_queued"]:
                audit_service.write_event(
                    actor_type="system",
                    actor_id="maintenance",
                    action="memory_verified",
                    result="success",
                    details={
                        **verification,
                        "proposals_queued": outcome["proposals_queued"],
                    },
                )
        except Exception:
            logger.exception("Verification pass failed; continuing with maintenance")

    # Consolidation: scan the active corpus and queue pending proposals for a
    # human reviewer, capped by the rules in Workstream 5 so unattended
    # generation cannot flood the queue. Independently toggleable so an
    # operator who wants verification alone can leave this off. Manual
    # generation through `/api/memory/proposals/generate` shares the same
    # transactional, capped, rotated path — neither caller is special-cased.
    proposals_generated = 0
    proposals_skipped_at_cap = 0
    if _system_setting_int("consolidation_scan_enabled", 1):
        try:
            from app.services import memory_proposal_service

            proposal_outcome = memory_proposal_service.generate_proposals()
            proposals_generated = int(proposal_outcome.get("created", 0))
            proposals_skipped_at_cap = int(proposal_outcome.get("skipped_at_cap", 0))
            if proposals_generated > 0:
                audit_service.write_event(
                    actor_type="system",
                    actor_id="maintenance",
                    action="memory_proposals_generated",
                    result="success",
                    details={
                        "created": proposals_generated,
                        "skipped_already_known": int(
                            proposal_outcome.get("skipped_already_known", 0)
                        ),
                        "skipped_at_cap": proposals_skipped_at_cap,
                    },
                )
        except Exception:
            logger.exception("Consolidation scan failed; continuing with maintenance")

    result = {
        "stale_activities_marked": stale_count,
        **sync_maintenance,
        "scratchpad_pruned": pruned,
        "ttl_swept": ttl_deleted,
        "retracted_purged": purged,
        "executions_pruned": executions_pruned,
        "webhook_deliveries_pruned": webhook_deliveries_pruned,
        "credential_audit_retention": credential_audit_retention,
        "records_verified": verification["verified"],
        "records_unverifiable": verification["checked"] - verification["verified"],
        "anchors_missing": verification["missing"],
        "proposals_generated": proposals_generated,
        "proposals_skipped_at_cap": proposals_skipped_at_cap,
    }

    # Record last-run status so it's visible (Settings page, status endpoint)
    # without digging through the audit log — a silently-broken or never-wired
    # scheduler should be obvious at a glance, not discoverable only by noticing
    # memory records piling up.
    now_iso = utc_now_iso()
    with get_db() as conn:
        for key, value in (
            ("maintenance_last_run_at", now_iso),
            ("maintenance_last_run_by", triggered_by),
            ("maintenance_last_run_summary_json", json.dumps(result)),
        ):
            conn.execute(
                """
                INSERT INTO system_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )
        conn.commit()

    return result


def get_maintenance_status() -> dict:
    """Last-run info plus the configured automatic schedule, for the Settings
    page and a status API — the single place to check "is this actually
    running" without querying the audit log or DB directly."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT key, value FROM system_settings WHERE key IN "
            "('maintenance_last_run_at', 'maintenance_last_run_by', 'maintenance_last_run_summary_json')"
        ).fetchall()
    values = {row["key"]: row["value"] for row in rows}

    summary = None
    if values.get("maintenance_last_run_summary_json"):
        try:
            summary = json.loads(values["maintenance_last_run_summary_json"])
        except (json.JSONDecodeError, TypeError):
            summary = None

    return {
        "last_run_at": values.get("maintenance_last_run_at"),
        "last_run_by": values.get("maintenance_last_run_by"),
        "last_run_summary": summary,
        "scheduler_enabled": settings.MAINTENANCE_INTERVAL_MINUTES > 0,
        "interval_minutes": settings.MAINTENANCE_INTERVAL_MINUTES,
    }
