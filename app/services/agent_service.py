import secrets
import hashlib
import json
from datetime import datetime, timezone
from app.time_utils import utc_now_iso
from typing import Optional

from app.database import get_db
from app.models.enums import normalize_id
from app.security.scope_utils import normalize_scope_string
from app.config import settings
from app.services import agent_kind, cleanup_service


def _normalize_scopes(scopes: list[str]) -> list[str]:
    normalized = []
    for scope in scopes:
        normalized_scope = normalize_scope_string(scope)
        if normalized_scope not in normalized:
            normalized.append(normalized_scope)
    return normalized


def _with_own_scope(agent_id: str, scopes: list[str]) -> list[str]:
    own_scope = f"agent:{normalize_id(agent_id)}"
    normalized = _normalize_scopes(scopes)
    if own_scope not in normalized:
        normalized.insert(0, own_scope)
    return normalized


# Sentinel distinguishing "leave default_recall_scopes unchanged" (default) from
# "clear it back to NULL / Option A" (explicit None) in update_agent.
_UNSET = object()
LEGACY_CAPABILITIES = ["memory", "coordination", "credentials", "connectors_read", "connectors_execute"]
ALL_CAPABILITIES = [*LEGACY_CAPABILITIES, "credentials_resolve", "admin_propose"]


def parse_capabilities(value: Optional[str]) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        parsed = []
    if not isinstance(parsed, list):  # a malformed row grants nothing (a bare string would match by substring)
        return []
    return [capability for capability in ALL_CAPABILITIES if capability in parsed]


def normalize_capabilities(capabilities: Optional[list[str]]) -> list[str]:
    if capabilities is None:
        return list(LEGACY_CAPABILITIES)
    invalid = set(capabilities) - set(ALL_CAPABILITIES)
    if invalid:
        raise ValueError(f"Unknown capabilities: {', '.join(sorted(invalid))}")
    normalized = [capability for capability in ALL_CAPABILITIES if capability in capabilities]
    if "connectors_execute" in normalized and "connectors_read" not in normalized:
        normalized.insert(normalized.index("connectors_execute"), "connectors_read")
    return normalized


def _constrain_recall(
    agent_id: str, default_recall_scopes: list[str], read_scopes: list[str]
) -> list[str]:
    """Constrain a default-recall set to the subset invariant: only scopes the
    agent can read, always including its own scope. Returned set is what gets
    stored, so the two lists can never drift out of sync."""
    own_scope = f"agent:{normalize_id(agent_id)}"
    read_set = set(read_scopes)
    constrained = [
        s for s in _normalize_scopes(default_recall_scopes) if s in read_set
    ]
    if own_scope not in constrained:
        constrained.insert(0, own_scope)
    return constrained


def generate_api_key() -> tuple[str, str]:
    plaintext = f"ac_sk_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    return plaintext, key_hash


def create_agent(
    agent_id: str,
    display_name: str,
    owner_user_id: str,
    description: str = "",
    default_user_id: Optional[str] = None,
    read_scopes: Optional[list[str]] = None,
    write_scopes: Optional[list[str]] = None,
    default_recall_scopes: Optional[list[str]] = None,
    capabilities: Optional[list[str]] = None,
    inherit_user_bindings: bool = False,
    issue_key: bool = True,
    key_label: str = "First key",
    kind: Optional[str] = None,
) -> tuple[dict, str]:
    """Create an agent. With ``issue_key`` (the default) it also gets a first key, returned once; without it the agent
    has no key until a person adds one (the dashboard does this: creating an agent shows no key)."""
    normalized_id = normalize_id(agent_id)
    if kind is None:
        # How a fetch-only entry has always been created (the "Service agent" role): exactly this one permission.
        only_own = not any(s_.startswith("workspace:") for s_ in (read_scopes or []) + (write_scopes or []))
        kind = "service" if (
            capabilities is not None and sorted(set(capabilities)) == agent_kind.SERVICE_PERMISSIONS and only_own
        ) else "agent"
    kind = agent_kind.normalize_kind(kind)
    if kind == "service":
        # A service fetches secrets and does nothing else: one permission, its own scope only.
        agent_kind.check_service_shape(capabilities, read_scopes, write_scopes, normalized_id, inherit_user_bindings)
        capabilities = list(agent_kind.SERVICE_PERMISSIONS)
        read_scopes = [f"agent:{normalized_id}"]
        write_scopes = [f"agent:{normalized_id}"]
        default_recall_scopes = None
    api_key_plaintext, api_key_hash = generate_api_key()

    if default_user_id is None:
        default_user_id = owner_user_id

    if read_scopes is None:
        read_scopes = [f"agent:{normalized_id}", "shared"]
        read_scopes.insert(1, f"user:{owner_user_id}")
    else:
        read_scopes = _with_own_scope(normalized_id, read_scopes)

    if write_scopes is None:
        write_scopes = [f"agent:{normalized_id}"]
        if settings.shared_scope_agent_list and normalized_id in settings.shared_scope_agent_list:
            write_scopes.append("shared")
    else:
        write_scopes = _with_own_scope(normalized_id, write_scopes)

    read_scopes_json = json.dumps(read_scopes)
    write_scopes_json = json.dumps(write_scopes)
    # NULL = Option A (unscoped recall fans all read_scopes). A provided set is
    # constrained to the subset invariant before storage.
    default_recall_scopes_json = (
        json.dumps(_constrain_recall(normalized_id, default_recall_scopes, read_scopes))
        if default_recall_scopes is not None
        else None
    )

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agents (id, display_name, description, owner_user_id, default_user_id,
                               read_scopes_json, write_scopes_json, default_recall_scopes_json,
                               capabilities_json, api_key_hash, inherit_user_bindings, kind)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (normalized_id, display_name, description, owner_user_id, default_user_id,
             read_scopes_json, write_scopes_json, default_recall_scopes_json,
             json.dumps(normalize_capabilities(capabilities)), api_key_hash, int(bool(inherit_user_bindings)), kind),
        )
        if issue_key:
            conn.execute(
                "INSERT INTO agent_keys (id, agent_id, label, key_hash, key_hint, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (secrets.token_urlsafe(9), normalized_id, _clean_label(key_label), api_key_hash,
                 api_key_plaintext[-4:], utc_now_iso(), owner_user_id),
            )
        else:
            # Nobody holds the key generated above: the old column gets a hash nothing can match, and no key row exists.
            conn.execute(
                "UPDATE agents SET api_key_hash = ? WHERE id = ?",
                (hashlib.sha256(secrets.token_bytes(32)).hexdigest(), normalized_id),
            )
        conn.commit()

        agent = {
            "id": normalized_id,
            "display_name": display_name,
            "description": description,
            "owner_user_id": owner_user_id,
            "default_user_id": default_user_id,
            "read_scopes_json": read_scopes_json,
            "write_scopes_json": write_scopes_json,
            "default_recall_scopes_json": default_recall_scopes_json,
            "capabilities_json": json.dumps(normalize_capabilities(capabilities)),
            "is_active": True,
            "inherit_user_bindings": int(bool(inherit_user_bindings)),
            "kind": kind,
        }

    return agent, (api_key_plaintext if issue_key else "")


def get_agent_by_id(agent_id: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.execute(
            """
            SELECT id, display_name, description, owner_user_id, default_user_id,
                   read_scopes_json, write_scopes_json, default_recall_scopes_json, capabilities_json, is_active, created_at,
                   inherit_user_bindings, kind
            FROM agents WHERE id = ?
            """,
            (agent_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


LAST_USED_REFRESH_SECONDS = 600


def get_agent_by_api_key(plaintext_key: str) -> Optional[dict]:
    """The agent a live key belongs to, or None. A revoked key matches nothing."""
    key_hash = hashlib.sha256(plaintext_key.encode()).hexdigest()
    with get_db() as conn:
        cursor = conn.execute(
            """
            SELECT a.id, a.display_name, a.description, a.owner_user_id, a.default_user_id,
                   a.read_scopes_json, a.write_scopes_json, a.default_recall_scopes_json, a.capabilities_json,
                   a.is_active, a.created_at, a.inherit_user_bindings,
                   k.id AS _key_id, k.last_used_at AS _last_used_at
            FROM agent_keys k JOIN agents a ON a.id = k.agent_id
            WHERE k.key_hash = ? AND k.revoked_at IS NULL
            """,
            (key_hash,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        agent = dict(row)
        key_id, last_used = agent.pop("_key_id"), agent.pop("_last_used_at")
        if agent["is_active"]:
            # Never wait for a lock for this note: the caller is the event loop.
            note_key_use(conn, key_id, last_used, wait=False)
        return agent


def note_key_use(conn, key_id: str, last_used: Optional[str], wait: bool = True, commit: bool = True) -> None:
    """Record that a key was used, at most once per LAST_USED_REFRESH_SECONDS. A note for the operator, never a reason
    to refuse a request. ``wait`` False gives up at once when the database is busy; ``commit`` False leaves the caller's
    transaction open (the credential paths authenticate inside one and must not end it early)."""
    if not _stale(last_used):
        return
    try:
        if not wait:
            conn.execute("PRAGMA busy_timeout=0")
        conn.execute("UPDATE agent_keys SET last_used_at = ? WHERE id = ?", (utc_now_iso(), key_id))
        if commit:
            conn.commit()
    except Exception:
        pass


def _stale(last_used: Optional[str]) -> bool:
    if not last_used:
        return True
    try:
        then = datetime.fromisoformat(last_used)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - then).total_seconds() > LAST_USED_REFRESH_SECONDS
    except ValueError:
        return True


# Where a service that authenticates a key itself (not through the request dependencies) finds the agent: one place,
# so a revoked key cannot slip through any of them. Bind the key's sha256 hex digest.
AGENT_ROW_BY_KEY_SQL = (
    "SELECT a.*, k.id AS key_id, k.last_used_at AS key_last_used "
    "FROM agent_keys k JOIN agents a ON a.id = k.agent_id JOIN users u ON u.id = a.owner_user_id "
    "WHERE k.key_hash = ? AND k.revoked_at IS NULL AND u.is_active = 1"
)


MAX_ACTIVE_KEYS = 20
MAX_LABEL_CHARS = 60


class AgentKeyError(ValueError):
    """A key request that cannot be honoured; ``code`` is the API error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _clean_label(label: Optional[str]) -> str:
    text = "".join(ch for ch in (label or "") if ch.isprintable()).strip()
    return text[:MAX_LABEL_CHARS] or "Unnamed key"


def _sync_legacy_hash(conn, agent_id: str) -> None:
    """Keep agents.api_key_hash equal to the newest live key (or a hash nothing matches), so a build from before
    several keys, started against this database, never accepts a key that was revoked."""
    row = conn.execute(
        "SELECT key_hash FROM agent_keys WHERE agent_id = ? AND revoked_at IS NULL ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    value = row["key_hash"] if row else hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    conn.execute("UPDATE agents SET api_key_hash = ? WHERE id = ?", (value, agent_id))


def _insert_key(conn, agent_id: str, label: str, created_by: Optional[str]) -> tuple[str, str]:
    plaintext, key_hash = generate_api_key()
    key_id = secrets.token_urlsafe(9)
    conn.execute(
        "INSERT INTO agent_keys (id, agent_id, label, key_hash, key_hint, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (key_id, agent_id, _clean_label(label), key_hash, plaintext[-4:], utc_now_iso(), created_by),
    )
    return key_id, plaintext


def add_agent_key(agent_id: str, label: str, created_by: Optional[str] = None) -> tuple[dict, str]:
    """Give an active agent one more key. Its other keys are untouched. Returns (key record, plaintext shown once)."""
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        agent = conn.execute("SELECT is_active FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if not agent:
            raise AgentKeyError("NOT_FOUND", "Agent not found")
        if not agent["is_active"]:
            raise AgentKeyError("AGENT_INACTIVE", "Cannot add a key for an inactive agent")
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_keys WHERE agent_id = ? AND revoked_at IS NULL", (agent_id,)
        ).fetchone()["n"]
        if active >= MAX_ACTIVE_KEYS:
            raise AgentKeyError("TOO_MANY_KEYS", f"An agent can hold {MAX_ACTIVE_KEYS} keys. Revoke one you no longer use.")
        key_id, plaintext = _insert_key(conn, agent_id, label, created_by)
        _sync_legacy_hash(conn, agent_id)
        conn.commit()
    return get_agent_key(agent_id, key_id), plaintext


def get_agent_key(agent_id: str, key_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, agent_id, label, key_hint, created_at, created_by, last_used_at, revoked_at, revoked_by "
            "FROM agent_keys WHERE agent_id = ? AND id = ?",
            (agent_id, key_id),
        ).fetchone()
    return dict(row) if row else None


def list_agent_keys(agent_id: str, include_revoked: bool = False) -> list[dict]:
    """The agent's keys, oldest first. Never the key or its hash."""
    query = (
        "SELECT id, agent_id, label, key_hint, created_at, created_by, last_used_at, revoked_at, revoked_by "
        "FROM agent_keys WHERE agent_id = ?"
    )
    if not include_revoked:
        query += " AND revoked_at IS NULL"
    with get_db() as conn:
        rows = conn.execute(query + " ORDER BY created_at, rowid", (agent_id,)).fetchall()
    return [dict(r) for r in rows]


def revoke_agent_key(agent_id: str, key_id: str, revoked_by: Optional[str] = None) -> bool:
    """Stop one key working. The agent's other keys are untouched. False when there is no such live key."""
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE agent_keys SET revoked_at = ?, revoked_by = ? WHERE agent_id = ? AND id = ? AND revoked_at IS NULL",
            (utc_now_iso(), revoked_by, agent_id, key_id),
        )
        if cursor.rowcount:
            _sync_legacy_hash(conn, agent_id)
        conn.commit()
        return cursor.rowcount > 0


def rename_agent_key(agent_id: str, key_id: str, label: str) -> Optional[dict]:
    """Change the label of a live key: the note about what the key is for. The key itself is untouched.
    None when there is no such live key."""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE agent_keys SET label = ? WHERE agent_id = ? AND id = ? AND revoked_at IS NULL",
            (_clean_label(label), agent_id, key_id),
        )
        conn.commit()
        if not cursor.rowcount:
            return None
    return get_agent_key(agent_id, key_id)


def is_agent_shared(agent: dict) -> bool:
    read_scopes = parse_scopes(agent.get("read_scopes_json", "[]"))
    write_scopes = parse_scopes(agent.get("write_scopes_json", "[]"))
    return "shared" in read_scopes or "shared" in write_scopes


def list_agents(
    owner_user_id: Optional[str] = None, is_active: Optional[bool] = None, kind: Optional[str] = None
) -> list[dict]:
    """Entries on the Agents page. ``kind`` ('agent' or 'service') narrows the list; left out, everything is listed."""
    with get_db() as conn:
        query = "SELECT id, display_name, description, owner_user_id, default_user_id, read_scopes_json, write_scopes_json, default_recall_scopes_json, capabilities_json, is_active, created_at, inherit_user_bindings, kind FROM agents WHERE 1=1"
        params = []

        if kind is not None:
            query += " AND kind = ?"
            params.append(agent_kind.normalize_kind(kind))
        if owner_user_id:
            query += " AND owner_user_id = ?"
            params.append(owner_user_id)
        if is_active is not None:
            query += " AND is_active = ?"
            params.append(is_active)

        query += " ORDER BY created_at DESC"
        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def list_visible_agents(user_id: str, is_active: Optional[bool] = None, kind: Optional[str] = None) -> list[dict]:
    agents = list_agents(is_active=is_active, kind=kind)
    visible_agents = []
    for agent in agents:
        if agent.get("owner_user_id") == user_id:
            visible_agents.append(agent)
            continue
        if agent.get("is_active") and is_agent_shared(agent):
            visible_agents.append(agent)
    return visible_agents


def update_agent(
    agent_id: str,
    display_name: Optional[str] = None,
    description: Optional[str] = None,
    read_scopes: Optional[list[str]] = None,
    write_scopes: Optional[list[str]] = None,
    default_recall_scopes=_UNSET,
    capabilities: Optional[list[str]] = None,
    inherit_user_bindings: Optional[bool] = None,
    kind: Optional[str] = None,
) -> bool:
    current = get_agent_by_id(agent_id)
    if current is None:
        return False

    updates = []
    params = []

    # The kind rules. A service keeps one permission and no scope beyond its own unless it is first changed to an
    # agent; an agent becomes a service only when nothing says it does agent work, and the change itself writes the
    # service's fixed permission and scope (an explicit, audited change by a person, never a side effect).
    current_kind = current.get("kind") or "agent"
    target_kind = agent_kind.normalize_kind(kind) if kind is not None else current_kind
    if target_kind == "service":
        if current_kind == "agent":
            with get_db() as conn:
                blockers = agent_kind.conflicts(conn, current)
            if blockers:
                raise agent_kind.AgentKindError(
                    "This entry cannot become a service: " + "; ".join(blockers) + "."
                )
            capabilities = list(agent_kind.SERVICE_PERMISSIONS)
            read_scopes = [f"agent:{normalize_id(agent_id)}"]
            write_scopes = [f"agent:{normalize_id(agent_id)}"]
            default_recall_scopes = None
            inherit_user_bindings = False
        else:
            agent_kind.check_service_shape(capabilities, read_scopes, write_scopes, agent_id, inherit_user_bindings)
    if target_kind != current_kind:
        updates.append("kind = ?")
        params.append(target_kind)

    if display_name is not None:
        updates.append("display_name = ?")
        params.append(display_name)
    if description is not None:
        updates.append("description = ?")
        params.append(description)
    if capabilities is not None:
        updates.append("capabilities_json = ?")
        params.append(json.dumps(normalize_capabilities(capabilities)))
    if inherit_user_bindings is not None:
        updates.append("inherit_user_bindings = ?")
        params.append(int(bool(inherit_user_bindings)))

    new_read = _with_own_scope(agent_id, read_scopes) if read_scopes is not None else None
    if new_read is not None:
        updates.append("read_scopes_json = ?")
        params.append(json.dumps(new_read))
    if write_scopes is not None:
        updates.append("write_scopes_json = ?")
        params.append(json.dumps(_with_own_scope(agent_id, write_scopes)))

    # Effective read set the default-recall subset is validated against.
    effective_read = new_read if new_read is not None else parse_scopes(
        current.get("read_scopes_json", "[]")
    )
    if default_recall_scopes is not _UNSET:
        # Explicit change: None clears back to NULL (Option A); a list is stored
        # constrained to the subset invariant.
        if default_recall_scopes is None:
            updates.append("default_recall_scopes_json = ?")
            params.append(None)
        else:
            updates.append("default_recall_scopes_json = ?")
            params.append(
                json.dumps(_constrain_recall(agent_id, default_recall_scopes, effective_read))
            )
    elif new_read is not None and current.get("default_recall_scopes_json"):
        # read_scopes changed and a default set exists: prune it to the new read
        # set so the subset invariant can't drift.
        stored = parse_scopes(current["default_recall_scopes_json"])
        updates.append("default_recall_scopes_json = ?")
        params.append(json.dumps(_constrain_recall(agent_id, stored, new_read)))

    if not updates:
        return False

    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(agent_id)

    with get_db() as conn:
        cursor = conn.execute(
            f"UPDATE agents SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        conn.commit()
        return cursor.rowcount > 0


def deactivate_agent(agent_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE agents SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (agent_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def reactivate_agent(agent_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE agents SET is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (agent_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def rotate_agent_key(agent_id: str, created_by: Optional[str] = None, label: str = "Rotated key") -> Optional[str]:
    """Replace every live key of an active agent with one new key (returned once). Use add_agent_key to keep the others."""
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        agent = conn.execute("SELECT is_active FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if not agent or not agent["is_active"]:
            return None
        conn.execute(
            "UPDATE agent_keys SET revoked_at = ?, revoked_by = ? WHERE agent_id = ? AND revoked_at IS NULL",
            (utc_now_iso(), created_by, agent_id),
        )
        _, plaintext = _insert_key(conn, agent_id, label, created_by)
        _sync_legacy_hash(conn, agent_id)
        conn.commit()
    return plaintext


def delete_agent_hard(agent_id: str) -> bool:
    normalized_id = normalize_id(agent_id)
    scope = f"agent:{normalized_id}"
    with get_db() as conn:
        cleanup_service.delete_scope_data(conn, scope)
        conn.execute(
            """
            DELETE FROM agent_activity
            WHERE agent_id = ? OR assigned_agent_id = ? OR reassigned_from_agent_id = ?
            """,
            (normalized_id, normalized_id, normalized_id),
        )
        # What the deleted agent left waiting must not keep waiting on a person: its credential requests are
        # cancelled (nobody is left to receive the credential), and its session records go with it.
        conn.execute(
            "UPDATE credential_requests SET status = 'cancelled', resolved_at = ?, resolved_by = 'agent deleted' "
            "WHERE requested_by_agent_id = ? AND status = 'pending'",
            (utc_now_iso(), normalized_id),
        )
        conn.execute("DELETE FROM agent_executions WHERE agent_id = ?", (normalized_id,))
        conn.execute("DELETE FROM agent_keys WHERE agent_id = ?", (normalized_id,))
        cursor = conn.execute("DELETE FROM agents WHERE id = ?", (normalized_id,))
        conn.commit()
        return cursor.rowcount > 0


def parse_scopes(scopes_json: str) -> list[str]:
    try:
        raw = json.loads(scopes_json)
        return _normalize_scopes(raw) if isinstance(raw, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
