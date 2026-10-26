"""Exact-entry machine resolution. Scope inheritance never authorizes a read."""
import hashlib
import json
import secrets

from app.database import get_db
from app.security.encryption import decrypt_value, encrypt_value, KEY_OPERATION_LOCK
from app.security.exceptions import APIError
from app.services.credential_service import is_expired
from app.time_utils import utc_now, parse_utc_datetime


def _audit(conn, actor_type, actor_id, action, resource_id, result):
    # Metadata only; never arguments, ciphertext, plaintext or bootstrap hashes.
    conn.execute(
        "INSERT INTO audit_log(actor_type,actor_id,action,resource_type,resource_id,result) "
        "VALUES (?,?,?,?,?,?)",
        (actor_type, actor_id, action, "credential_consumer_grant", resource_id, result),
    )


def create_grant(consumer_id, entry_id, owner_id, expires_at=None, allow_write=False):
    if type(allow_write) is not bool:
        raise APIError("INVALID_GRANT", "Write authority must be an explicit boolean", 400)
    if expires_at is not None:
        expires_at = parse_utc_datetime(expires_at).isoformat()
        if parse_utc_datetime(expires_at) <= utc_now():
            raise APIError("INVALID_EXPIRY", "Grant expiry must be in the future", 400)
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        agent = conn.execute("SELECT * FROM agents WHERE id=?", (consumer_id,)).fetchone()
        entry = conn.execute("SELECT id FROM credentials WHERE id=?", (entry_id,)).fetchone()
        if not agent or not agent["is_active"] or not entry:
            raise APIError("NOT_FOUND", "Active consumer or credential not found", 404)
        try:
            caps = json.loads(agent["capabilities_json"])
        except (ValueError, TypeError):
            caps = []
        if not isinstance(caps, list) or "credentials_resolve" not in caps:
            raise APIError("CAPABILITY_DENIED", "Consumer requires credentials_resolve", 403)
        existing = conn.execute("SELECT id FROM credential_consumer_grants WHERE consumer_agent_id=? AND entry_id=? AND revoked_at IS NULL", (consumer_id, entry_id)).fetchone()
        if existing:
            raise APIError("GRANT_EXISTS", "Revoke the existing grant before changing entry authority", 409)
        grant_id = secrets.token_urlsafe(16)
        conn.execute(
            "INSERT INTO credential_consumer_grants(id,consumer_agent_id,entry_id,created_by,expires_at,created_at,allow_write) "
            "VALUES(?,?,?,?,?,?,?)",
            (grant_id, consumer_id, entry_id, owner_id, expires_at, utc_now().isoformat(), int(allow_write)),
        )
        _audit(conn, "user", owner_id, "credential_consumer_grant_created", grant_id, "success")
        return dict(conn.execute("SELECT * FROM credential_consumer_grants WHERE id=?", (grant_id,)).fetchone())


def revoke_grant(grant_id, owner_id):
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        grant = conn.execute("SELECT * FROM credential_consumer_grants WHERE id=?", (grant_id,)).fetchone()
        if not grant:
            raise APIError("NOT_FOUND", "Grant not found", 404)
        if not grant["revoked_at"]:
            conn.execute("UPDATE credential_consumer_grants SET revoked_at=? WHERE id=?", (utc_now().isoformat(), grant_id))
            _audit(conn, "user", owner_id, "credential_consumer_grant_revoked", grant_id, "success")
        return {"id": grant_id, "revoked": True}


def resolve(api_key, entry_id, metadata_only=False):
    with KEY_OPERATION_LOCK:
        return _resolve(api_key, entry_id, metadata_only)


def _resolve(api_key, entry_id, metadata_only=False):
    """Serialize grant revocation/key changes with authorization and decryption.

    Reauthenticate inside the transaction, so stale dependency contexts cannot
    resolve after a key rotation or deactivation committed before this read.
    An already returned value cannot be recalled; consumers must enforce leases.
    """
    denied = None
    result = None
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        agent = conn.execute("SELECT a.* FROM agents a JOIN users u ON u.id=a.owner_user_id WHERE a.api_key_hash=? AND u.is_active=1", (hashlib.sha256(api_key.encode()).hexdigest(),)).fetchone()
        if not agent or not agent["is_active"]:
            raise APIError("INVALID_KEY", "Invalid or inactive agent API key", 401)
        try:
            caps = json.loads(agent["capabilities_json"])
        except (TypeError, ValueError):
            caps = []
        grants = conn.execute(
            "SELECT * FROM credential_consumer_grants WHERE consumer_agent_id=? AND entry_id=? AND revoked_at IS NULL",
            (agent["id"], entry_id),
        ).fetchall()
        valid = [g for g in grants if not is_expired({"expires_at": g["expires_at"]})]
        entry = conn.execute("SELECT * FROM credentials WHERE id=?", (entry_id,)).fetchone()
        if not isinstance(caps, list) or "credentials_resolve" not in caps or not valid:
            denied = APIError("RESOLVE_DENIED", "No active exact-entry consumer grant", 403)
        elif not entry or is_expired(dict(entry)) or _write_blocked(entry):
            denied = APIError("CREDENTIAL_UNAVAILABLE", "Credential is unavailable or expired", 409)
        else:
            try:
                value = None if metadata_only else decrypt_value(entry["value_encrypted"])
                if not metadata_only and (not value or not value.strip()):
                    denied = APIError("CREDENTIAL_UNAVAILABLE", "Credential is not ready", 409)
                else:
                    result = {
                        "entry_id": entry_id,
                        "grant_id": valid[-1]["id"],
                        "version": hashlib.sha256(entry["value_encrypted"].encode()).hexdigest(),
                        "expires_at": entry["expires_at"],
                        "grant_expires_at": valid[-1]["expires_at"],
                        "revalidate_after_seconds": 30,
                    }
                    if not metadata_only:
                        result["value"] = value
            except Exception:
                # Crypto exceptions must not expose secret-containing diagnostics.
                denied = APIError("CREDENTIAL_UNAVAILABLE", "Credential cannot be resolved", 409)
        # Status/lease checks return no secret and are frequent heartbeats. Keep
        # denials auditable, but do not turn every successful heartbeat into a
        # durable per-entry "resolved" event.
        if denied or not metadata_only:
            _audit(conn, "agent", agent["id"], "credential_consumer_denied" if denied else "credential_consumer_resolved", entry_id, "blocked" if denied else "success")
    if denied:
        raise denied
    return result


def lease_status(api_key, entry_ids):
    """Validate one consumer's bounded exact-entry lease set in one transaction.

    This path deliberately reads metadata only: it neither decrypts secret values
    nor writes a success audit row per heartbeat. Denials remain auditable.
    """
    if not isinstance(entry_ids, list) or not 1 <= len(entry_ids) <= 64:
        raise APIError("INVALID_LEASE_SET", "Lease status requires 1 to 64 entry IDs", 400)
    if any(not isinstance(v, str) or not v or len(v) > 128 for v in entry_ids) or len(set(entry_ids)) != len(entry_ids):
        raise APIError("INVALID_LEASE_SET", "Lease status entry IDs must be unique valid identifiers", 400)
    denied = None
    result = []
    with KEY_OPERATION_LOCK, get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        agent = conn.execute("SELECT a.* FROM agents a JOIN users u ON u.id=a.owner_user_id WHERE a.api_key_hash=? AND u.is_active=1", (hashlib.sha256(api_key.encode()).hexdigest(),)).fetchone()
        if not agent or not agent["is_active"]:
            raise APIError("INVALID_KEY", "Invalid or inactive agent API key", 401)
        try:
            caps = json.loads(agent["capabilities_json"])
        except (TypeError, ValueError):
            caps = []
        for entry_id in entry_ids:
            grants = conn.execute(
                "SELECT * FROM credential_consumer_grants WHERE consumer_agent_id=? AND entry_id=? AND revoked_at IS NULL",
                (agent["id"], entry_id),
            ).fetchall()
            valid = [g for g in grants if not is_expired({"expires_at": g["expires_at"]})]
            entry = conn.execute("SELECT * FROM credentials WHERE id=?", (entry_id,)).fetchone()
            if not isinstance(caps, list) or "credentials_resolve" not in caps or not valid:
                denied = APIError("RESOLVE_DENIED", "No active exact-entry consumer grant", 403)
                break
            if not entry or is_expired(dict(entry)) or _write_blocked(entry):
                denied = APIError("CREDENTIAL_UNAVAILABLE", "Credential is unavailable or expired", 409)
                break
            result.append({
                "entry_id": entry_id,
                "grant_id": valid[-1]["id"],
                "version": hashlib.sha256(entry["value_encrypted"].encode()).hexdigest(),
                "expires_at": entry["expires_at"],
                "grant_expires_at": valid[-1]["expires_at"],
                "revalidate_after_seconds": 30,
            })
        if denied:
            _audit(conn, "agent", agent["id"], "credential_consumer_denied", entry_id, "blocked")
    if denied:
        raise denied
    return {"entries": result}


MAX_WRITE_BYTES = 65536
MAX_CONFLICT_CANDIDATES = 3


def _write_blocked(entry):
    try:
        metadata = json.loads(entry["metadata_json"] or "{}")
        return not isinstance(metadata, dict) or bool(metadata.get("consumer_write_conflict"))
    except (ValueError, TypeError):
        return True


def _authenticate_writer(conn, api_key, entry_id):
    agent = conn.execute("SELECT a.* FROM agents a JOIN users u ON u.id=a.owner_user_id WHERE a.api_key_hash=? AND u.is_active=1", (hashlib.sha256(api_key.encode()).hexdigest(),)).fetchone()
    if not agent or not agent["is_active"]:
        raise APIError("INVALID_KEY", "Invalid or inactive agent API key", 401)
    try:
        caps = json.loads(agent["capabilities_json"])
    except (ValueError, TypeError):
        caps = []
    grants = conn.execute("SELECT * FROM credential_consumer_grants WHERE consumer_agent_id=? AND entry_id=? AND revoked_at IS NULL AND allow_write=1", (agent["id"], entry_id)).fetchall()
    valid = [g for g in grants if not is_expired({"expires_at": g["expires_at"]})]
    if not isinstance(caps, list) or "credentials_resolve" not in caps or not valid:
        _audit(conn, "agent", agent["id"], "credential_consumer_write_denied", entry_id, "blocked")
        return agent, None
    return agent, valid[-1]


def write(api_key, entry_id, request_id, expected_version, value):
    """CAS an exact granted entry, preserving a conflicting candidate encrypted.

    Reauthenticate before replay, and commit value, result and audit atomically.
    Lock order matches key rotation: key-operation lock before SQLite transaction.
    """
    import re
    from app.branding import CREDENTIAL_PREFIX

    try:
        value_bytes = value.encode("utf-8") if isinstance(value, str) else None
    except UnicodeError:
        value_bytes = None
    if (value_bytes is None or not value.strip() or len(value_bytes) > MAX_WRITE_BYTES
        or not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id)
        or not isinstance(expected_version, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_version)):
        raise APIError("INVALID_WRITE", "Invalid credential write request", 400)
    payload_hash = hashlib.sha256(json.dumps([expected_version, value], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    denied = None
    result = None
    with KEY_OPERATION_LOCK, get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        agent, grant = _authenticate_writer(conn, api_key, entry_id)
        entry = conn.execute("SELECT * FROM credentials WHERE id=?", (entry_id,)).fetchone()
        if not grant:
            denied = APIError("WRITE_DENIED", "No active exact-entry write grant", 403)
        elif not entry or is_expired(dict(entry)):
            denied = APIError("CREDENTIAL_UNAVAILABLE", "Credential is unavailable or expired", 409)
        else:
            replay = conn.execute("SELECT * FROM credential_consumer_writes WHERE consumer_agent_id=? AND entry_id=? AND request_id=?", (agent["id"], entry_id, request_id)).fetchone()
            if replay:
                if replay["payload_sha256"] != payload_hash:
                    denied = APIError("REQUEST_CONFLICT", "Request identity was already used for another write", 409)
                else:
                    result = json.loads(replay["result_json"])
            elif _write_blocked(entry):
                denied = APIError("CREDENTIAL_BLOCKED", "Credential requires owner conflict reconciliation", 409)
            else:
                now = utc_now().isoformat()
                version = hashlib.sha256(entry["value_encrypted"].encode()).hexdigest()
                if version == expected_version:
                    encrypted = encrypt_value(value)
                    conn.execute("UPDATE credentials SET value_encrypted=?, updated_at=? WHERE id=?", (encrypted, now, entry_id))
                    result = {"entry_id": entry_id, "request_id": request_id, "state": "committed", "version": hashlib.sha256(encrypted.encode()).hexdigest(), "status_code": 200}
                    _audit(conn, "agent", agent["id"], "credential_consumer_written", entry_id, "success")
                else:
                    count = conn.execute("SELECT COUNT(*) FROM credentials WHERE CASE WHEN json_valid(metadata_json) THEN json_extract(metadata_json,'$.consumer_conflict_parent') END=?", (entry_id,)).fetchone()[0]
                    if count >= MAX_CONFLICT_CANDIDATES:
                        # A provider may already have invalidated the previous token.
                        # Capacity refusal cannot leave that entry resolvable as current.
                        metadata = json.loads(entry["metadata_json"] or "{}")
                        metadata["consumer_write_conflict"] = {"reason": "conflict_storage_full", "request_id": request_id, "recorded_at": now, "candidate_retained": False}
                        conn.execute("UPDATE credentials SET metadata_json=?,updated_at=? WHERE id=?", (json.dumps(metadata),now,entry_id))
                        denied = APIError("CONFLICT_STORAGE_FULL", "Owner must reconcile retained conflicts and the unretained provider outcome", 409)
                    else:
                        candidate_id = secrets.token_urlsafe(16)
                        reference = CREDENTIAL_PREFIX + "CONFLICT_" + secrets.token_hex(16).upper()
                        metadata = json.loads(entry["metadata_json"] or "{}")
                        candidate_metadata = {"consumer_conflict_parent": entry_id, "request_id": request_id, "writer_consumer": agent["id"]}
                        conn.execute("INSERT INTO credentials(id,scope,name,label,value_encrypted,metadata_json,reference_name,created_by) VALUES(?,?,?,?,?,?,?,?)", (candidate_id,entry["scope"],"Consumer conflict candidate","Owner reconciliation required",encrypt_value(value),json.dumps(candidate_metadata),reference,agent["owner_user_id"]))
                        metadata["consumer_write_conflict"] = {"candidate_entry_id": candidate_id, "request_id": request_id, "recorded_at": now}
                        conn.execute("UPDATE credentials SET metadata_json=?,updated_at=? WHERE id=?", (json.dumps(metadata),now,entry_id))
                        result = {"entry_id": entry_id, "request_id": request_id, "state": "conflict_retained", "candidate_entry_id": candidate_id, "status_code": 409}
                        _audit(conn, "agent", agent["id"], "credential_consumer_write_conflict", entry_id, "blocked")
                if result:
                    conn.execute("INSERT INTO credential_consumer_writes(consumer_agent_id,entry_id,request_id,payload_sha256,result_json,status_code,created_at) VALUES(?,?,?,?,?,?,?)", (agent["id"],entry_id,request_id,payload_hash,json.dumps(result),result["status_code"],now))
        if denied and grant:
            _audit(conn, "agent", agent["id"], "credential_consumer_write_denied", entry_id, "blocked")
    if denied:
        raise denied
    return result
