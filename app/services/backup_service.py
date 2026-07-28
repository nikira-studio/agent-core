import hashlib
import io
import json
import logging
import os
import secrets
import shutil
import sqlite3
import zipfile
from datetime import timedelta
from typing import Optional

from cryptography.fernet import Fernet

from app.branding import APP_VERSION, DB_FILENAME, MANIFEST_VERSION_KEY
from app.config import settings
from app.database import get_db
from app.time_utils import parse_utc_datetime, utc_now, utc_now_iso


logger = logging.getLogger(__name__)


def _system_setting_int(key: str, default: int) -> int:
    """Read an integer system setting, falling back to the default on anything odd.

    Maintenance must never fail because a setting row is missing or malformed —
    a sweep that refuses to run is how the logs grew to 142 MB in the first place.
    """
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM system_settings WHERE key = ?", (key,)
            ).fetchone()
        return int(row["value"]) if row else default
    except (ValueError, TypeError, sqlite3.Error):
        return default



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


def build_backup_manifest(
    db_path: str,
    credential_key_path: str,
    exported_by: str,
    app_version: str,
) -> dict:
    checksums = {}
    if os.path.exists(db_path):
        checksums[DB_FILENAME] = compute_sha256(db_path)

    env_key = _configured_env_key_bytes()
    if env_key is not None:
        checksums["credential.key"] = hashlib.sha256(env_key).hexdigest()
    elif os.path.exists(credential_key_path):
        checksums["credential.key"] = compute_sha256(credential_key_path)
    if os.path.exists(_keyring_path()):
        checksums["credential.keyring"] = compute_sha256(_keyring_path())

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
    manifest = build_backup_manifest(
        db_path, credential_key_path, exported_by, app_version
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.exists(db_path):
            zf.write(db_path, arcname=DB_FILENAME)
        env_key = _configured_env_key_bytes()
        if env_key is not None:
            zf.writestr("credential.key", env_key)
        elif os.path.exists(credential_key_path):
            zf.write(credential_key_path, arcname="credential.key")
        if os.path.exists(_keyring_path()):
            zf.write(_keyring_path(), arcname="credential.keyring")
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

    buf.seek(0)
    return buf


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
    version_key = MANIFEST_VERSION_KEY if MANIFEST_VERSION_KEY in data else "agent_core_version"
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


def _effective_key(credential_key_path: str, fallback: bytes | None = None) -> bytes:
    key = _configured_env_key_bytes()
    if key is not None:
        return key
    if os.path.exists(credential_key_path):
        with open(credential_key_path, "rb") as f:
            key = f.read()
        Fernet(key)
        return key
    if fallback:
        Fernet(fallback)
        return fallback
    key = Fernet.generate_key()
    with open(credential_key_path, "wb") as f:
        f.write(key)
    os.chmod(credential_key_path, 0o600)
    return key


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


def _row_dict(row) -> dict:
    return dict(row)


def _table_columns(conn, table: str) -> list[str]:
    return [
        row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    ]


# What a merge restore brings across. The test is whether a person would call
# it "their data": records, the things that own them, and the configuration that
# makes an installation behave the way they set it up.
#
# Deliberately excluded, and why: audit_log and webhook_delivery_log are logs of
# events that happened on the other installation, not state; sessions,
# otp_secrets and broker_credentials are this machine's login and identity, and
# importing them would hand out access; connector_oauth_states,
# connector_session_cache and tool_result_spill are caches that regenerate.
MERGED_TABLES = (
    "users",
    "workspaces",
    "workspace_collaborators",
    "agents",
    "memory_records",
    "memory_embeddings",
    "memory_proposals",
    "credentials",
    "agent_activity",
    "connector_types",
    "connector_bindings",
    "connector_executions",
    "adapter_installations",
    "webhook_registrations",
    "system_settings",
)


# Which columns point at rows in another merged table. Merging table by table
# with "current wins" is safe only for rows that stand alone: when a key exists
# in both databases with different content, the backup's row is dropped, and any
# row that referenced it would silently re-point at this installation's
# unrelated record of the same name. A binding whose credential was dropped this
# way would authenticate with a stranger's secret.
#
# Four kinds, because relationships here are not all plain foreign keys:
#   "id"          the column holds the parent's key directly
#   "scope"       the column holds a scope string, `workspace:x` / `agent:x` /
#                 `user:x`, which names a row in the matching table
#   "json_ids"    the column holds JSON containing a list of parent keys
#   "json_scopes" the column holds JSON containing a list of scope strings
#
# The scope arrays on `agents` are the sharpest case: they are the agent's
# permissions. An imported agent carrying ["workspace:proj"] would be granted
# access to whatever `proj` means *here*, which is how a restore hands a
# stranger's agent the keys to your workspace.
FOREIGN_REFERENCES = {
    "connector_bindings": (
        ("id", "credential_id", "credentials"),
        ("id", "connector_type_id", "connector_types"),
        ("scope", "scope", None),
        ("id", "created_by", "users"),
    ),
    "connector_executions": (("id", "binding_id", "connector_bindings"),),
    "memory_embeddings": (("id", "record_id", "memory_records"),),
    "adapter_installations": (("id", "installed_connector_type_id", "connector_types"),),
    "workspace_collaborators": (
        ("id", "workspace_id", "workspaces"),
        ("id", "user_id", "users"),
        ("id", "created_by", "users"),
    ),
    "agents": (
        ("id", "owner_user_id", "users"),
        ("id", "default_user_id", "users"),
        ("json_scopes", "read_scopes_json", None),
        ("json_scopes", "write_scopes_json", None),
        ("json_scopes", "default_recall_scopes_json", None),
    ),
    "workspaces": (("id", "owner_user_id", "users"),),
    "credentials": (("scope", "scope", None), ("id", "created_by", "users")),
    # A memory record belongs to a scope, and can point at the record it
    # replaced. Both are relationships even though neither is a foreign key.
    "memory_records": (
        ("scope", "scope", None),
        ("id", "supersedes_id", "memory_records"),
        ("id", "superseded_by_id", "memory_records"),
    ),
    "memory_proposals": (
        ("scope", "scope", None),
        ("json_ids", "target_ids_json", "memory_records"),
        ("id", "decided_by", "users"),
    ),
    # assigned_agent_id decides who may claim a piece of work, so a conflicting
    # agent id here is an authorization relationship, not just provenance.
    "agent_activity": (
        ("id", "agent_id", "agents"),
        ("id", "user_id", "users"),
        ("id", "assigned_agent_id", "agents"),
        ("id", "reassigned_from_agent_id", "agents"),
        ("scope", "memory_scope", None),
    ),
    "webhook_registrations": (("id", "created_by", "users"),),
}

# A scope string names a row in one of these tables.
SCOPE_PREFIXES = {"workspace": "workspaces", "agent": "agents", "user": "users"}


def _scope_parent(value) -> tuple[Optional[str], Optional[str]]:
    """Split `workspace:proj` into ("workspaces", "proj"). (None, None) if not a scope."""
    if not isinstance(value, str) or ":" not in value:
        return None, None
    prefix, _, name = value.partition(":")
    return SCOPE_PREFIXES.get(prefix), name or None


def _json_ids(value) -> list:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return []
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, str)]
    return []


def _row_is_blocked(table: str, row: dict, blocked: dict[str, set]) -> bool:
    """Whether this row points at something that means something else here."""
    for kind, column, parent in FOREIGN_REFERENCES.get(table, ()):
        value = row.get(column)
        if value is None:
            continue
        if kind == "id":
            if value in blocked.get(parent, ()):
                return True
        elif kind == "scope":
            scope_table, key = _scope_parent(value)
            if scope_table and key in blocked.get(scope_table, ()):
                return True
        elif kind == "json_ids":
            if any(item in blocked.get(parent, ()) for item in _json_ids(value)):
                return True
        elif kind == "json_scopes":
            for scope in _json_ids(value):
                scope_table, key = _scope_parent(scope)
                if scope_table and key in blocked.get(scope_table, ()):
                    return True
    return False


def _rows_differ(left: dict, right: dict) -> bool:
    shared = set(left) & set(right)
    return any(left[column] != right[column] for column in shared)


def _conflicting_keys(current_con, backup_con, table: str) -> set:
    """Single-column keys that exist in both databases with different content.

    These are the identities that mean two different things on the two
    installations. Same key, same content is a genuine match and not a conflict.
    """
    current_cols = _table_columns(current_con, table)
    backup_cols = _table_columns(backup_con, table)
    if not backup_cols or not current_cols:
        return set()
    key_cols = _primary_key_columns(current_con, table)
    if len(key_cols) != 1:
        return set()
    key = key_cols[0]
    shared = [c for c in current_cols if c in backup_cols]
    quoted = ",".join(shared)

    current_rows = {
        _row_dict(r)[key]: _row_dict(r)
        for r in current_con.execute(f"SELECT {quoted} FROM {table}").fetchall()
    }
    conflicts = set()
    for row in backup_con.execute(f"SELECT {quoted} FROM {table}").fetchall():
        row_dict = _row_dict(row)
        existing = current_rows.get(row_dict[key])
        if existing is not None and _rows_differ(existing, row_dict):
            conflicts.add(row_dict[key])
    return conflicts


def _blocked_keys(current_con, backup_con) -> dict[str, set]:
    """Every key that must not be imported, including everything downstream.

    Seeded with the keys that exist in both databases meaning different things,
    then grown to a fixpoint: a row that cannot be imported because its parent
    conflicts is itself missing here, so anything referencing *it* cannot be
    imported either. Without the closure, an execution whose binding was
    skipped is inserted and hits a foreign key constraint, failing the merge
    rather than declining one row.
    """
    parents = {
        parent
        for refs in FOREIGN_REFERENCES.values()
        for _, _, parent in refs
        if parent
    } | set(SCOPE_PREFIXES.values())

    blocked = {
        table: _conflicting_keys(current_con, backup_con, table) for table in parents
    }
    for table in FOREIGN_REFERENCES:
        blocked.setdefault(table, set())

    for _ in range(len(FOREIGN_REFERENCES) + 1):
        grew = False
        for table in FOREIGN_REFERENCES:
            key_cols = _primary_key_columns(current_con, table)
            backup_cols = _table_columns(backup_con, table)
            if len(key_cols) != 1 or key_cols[0] not in backup_cols:
                continue
            key = key_cols[0]
            rows = backup_con.execute(
                f"SELECT * FROM {table}"
            ).fetchall()
            for row in rows:
                row_dict = _row_dict(row)
                if row_dict[key] in blocked[table]:
                    continue
                if _row_is_blocked(table, row_dict, blocked):
                    blocked[table].add(row_dict[key])
                    grew = True
        if not grew:
            break
    return blocked


def _primary_key_columns(con, table: str) -> list[str]:
    """The table's real key, so tables not keyed on `id` can still be merged.

    Keying on `id` alone quietly skipped every table with a composite or named
    key — collaborator grants, settings, adapter installs — and reported the
    skip as "0 rows inserted", which is indistinguishable from "nothing to do".
    """
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    keyed = sorted(
        (r for r in rows if r["pk"]), key=lambda r: r["pk"]
    )
    return [r["name"] for r in keyed]


def _insert_missing_rows(
    current_con,
    backup_con,
    table: str,
    transform=None,
    conflicts: Optional[dict[str, set]] = None,
) -> tuple[int, int]:
    """Copy rows the current database does not have. Returns (inserted, skipped).

    `conflicts` maps a parent table to the keys that mean different things on
    the two installations. A row pointing at one of those is not imported: it
    would keep its own id while silently adopting this installation's unrelated
    parent.
    """
    current_cols = _table_columns(current_con, table)
    backup_cols = _table_columns(backup_con, table)
    if not backup_cols:
        # The backup predates this table. There is nothing to bring across, which
        # is an ordinary outcome for an older archive rather than a failure —
        # restoring a backup taken before a feature existed must still work.
        return 0, 0
    insert_cols = [c for c in current_cols if c in backup_cols]
    key_cols = [c for c in _primary_key_columns(current_con, table) if c in insert_cols]
    if not key_cols:
        raise ValueError(f"{table} has no usable primary key to merge on")


    inserted = 0
    skipped = 0
    quoted_cols = ",".join(insert_cols)
    placeholders = ",".join(["?" for _ in insert_cols])
    key_match = " AND ".join(f"{c} = ?" for c in key_cols)

    for row in backup_con.execute(f"SELECT {quoted_cols} FROM {table}").fetchall():
        row_dict = _row_dict(row)
        existing = current_con.execute(
            f"SELECT 1 FROM {table} WHERE {key_match}",
            tuple(row_dict[c] for c in key_cols),
        ).fetchone()
        if existing is not None:
            continue
        if conflicts and _row_is_blocked(table, row_dict, conflicts):
            skipped += 1
            continue
        if transform:
            row_dict = transform(row_dict)
        values = [row_dict.get(c) for c in insert_cols]
        current_con.execute(
            f"INSERT INTO {table} ({quoted_cols}) VALUES ({placeholders})",
            values,
        )
        inserted += 1

    return inserted, skipped


def merge_restore_from_zip(
    zip_bytes: io.BytesIO,
    db_path: str,
    credential_key_path: str,
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
            f.write(extracted[DB_FILENAME])

        backup_con = sqlite3.connect(db_temp)
        backup_con.row_factory = sqlite3.Row

        backup_key = extracted["credential.key"]
        current_key = _effective_key(credential_key_path, fallback=backup_key)
        if (
            not os.path.exists(credential_key_path)
            and settings.ENCRYPTION_KEY.lower() == "auto"
        ):
            with open(credential_key_path, "wb") as f:
                f.write(current_key)
            os.chmod(credential_key_path, 0o600)

        backup_fernet = Fernet(backup_key)
        current_fernet = Fernet(current_key)

        def transform_credential(row: dict) -> dict:
            if backup_key != current_key and row.get("value_encrypted"):
                plaintext = backup_fernet.decrypt(row["value_encrypted"].encode())
                row["value_encrypted"] = current_fernet.encrypt(plaintext).decode()
            return row

        inserted_counts = {}
        failures: dict[str, str] = {}
        backup_dir_str = str(backup_dir)
        _backup_existing_file(db_path, backup_dir_str, timestamp, "merge-pre-db")
        _backup_existing_file(
            credential_key_path, backup_dir_str, timestamp, "merge-pre-key"
        )

        skipped_conflicts: dict[str, int] = {}

        with get_db() as current_con:
            current_con.execute("BEGIN IMMEDIATE")

            # Worked out before anything is written, because a row's parent may
            # live in a table that is merged after it, and because a blocked row
            # blocks its own dependants in turn.
            conflicts = _blocked_keys(current_con, backup_con)

            for table in MERGED_TABLES:
                try:
                    inserted, skipped = _insert_missing_rows(
                        current_con,
                        backup_con,
                        table,
                        transform=transform_credential
                        if table == "credentials"
                        else None,
                        conflicts=conflicts,
                    )
                    inserted_counts[table] = inserted
                    if skipped:
                        skipped_conflicts[table] = skipped
                except Exception as exc:
                    # A table that could not be merged is a partial restore, and
                    # the operator has to know which one. Recording it as "0
                    # inserted" made a failure look exactly like a table that had
                    # nothing new in it.
                    logger.exception("Merge restore failed on table %s", table)
                    failures[table] = str(exc)

            current_con.commit()
        backup_con.close()
    except Exception:
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
        "credential_key_handling": "current effective key preserved; imported credentials re-encrypted when backup key differs",
        "inserted_counts": inserted_counts,
        "tables_merged": list(MERGED_TABLES),
        "failed_tables": failures,
        "skipped_conflicts": skipped_conflicts,
    }
    if skipped_conflicts:
        manifest_data["merge"]["conflict_note"] = (
            "Rows referencing a record whose id already means something different "
            "here were not imported, because importing them would have attached "
            "them to this installation's unrelated record."
        )
    if failures:
        # Partial is not success. The rows that did merge are kept — undoing
        # them would be its own risk — but the caller is told plainly which
        # tables did not, because "Restore complete" over a half-restored
        # database is the failure that gets discovered weeks later.
        return (
            False,
            "Restore was incomplete: " + ", ".join(sorted(failures)),
            manifest_data,
        )
    return True, "", manifest_data


def restore_from_zip(
    zip_bytes: io.BytesIO,
    db_path: str,
    credential_key_path: str,
) -> tuple[bool, str, dict]:
    ok, msg, manifest_data, extracted = _read_validated_backup(zip_bytes)
    if not ok:
        return False, msg, {}

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

    def atomic_replace(src_bytes: bytes, dst_path: str, backup_suffix: str):
        tmp_path = dst_path + f".{timestamp}.tmp"
        with open(tmp_path, "wb") as f:
            f.write(src_bytes)
        if os.path.exists(dst_path):
            backup_path = os.path.join(
                backup_dir, f"{os.path.basename(dst_path)}.{backup_suffix}"
            )
            shutil.copy2(dst_path, backup_path)
        os.replace(tmp_path, dst_path)

    atomic_replace(extracted[DB_FILENAME], db_path, "db")
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

    stale_count = mark_stale_activities()

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

    # Hard-delete retracted/superseded records past their grace period. These
    # are no longer the active truth and (per design) aren't meant to be read
    # again in normal operation -- see get_memory_by_scope's record_status
    # default -- but nothing previously reclaimed them, so they accumulated
    # forever. status_changed_at (not created_at) is the cutoff basis: an old
    # record retracted five minutes ago must still get its full grace period,
    # not be immediately purge-eligible because it happens to be old.
    retracted_retention_days = _system_setting_int("retracted_retention_days", 30)

    retracted_cutoff = utc_now() - timedelta(days=retracted_retention_days)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, status_changed_at FROM memory_records "
            "WHERE record_status IN ('retracted', 'superseded')"
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
    # record what agents *did* had none, and on the first real deployment the
    # connector execution log alone reached 142 MB of a 180 MB database. These
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
                    details={**verification, "proposals_queued": outcome["proposals_queued"]},
                )
        except Exception:
            logger.exception("Verification pass failed; continuing with maintenance")

    result = {
        "stale_activities_marked": stale_count,
        "scratchpad_pruned": pruned,
        "ttl_swept": ttl_deleted,
        "retracted_purged": purged,
        "executions_pruned": executions_pruned,
        "webhook_deliveries_pruned": webhook_deliveries_pruned,
        "records_verified": verification["verified"],
        "records_unverifiable": verification["checked"] - verification["verified"],
        "anchors_missing": verification["missing"],
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
