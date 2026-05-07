import json
import sqlite3


def remove_scope_from_agent_access(conn: sqlite3.Connection, scope: str) -> int:
    rows = conn.execute(
        "SELECT id, read_scopes_json, write_scopes_json FROM agents"
    ).fetchall()
    changed = 0
    for row in rows:
        read_scopes = _remove_scope(row["read_scopes_json"], scope)
        write_scopes = _remove_scope(row["write_scopes_json"], scope)
        if read_scopes is None and write_scopes is None:
            continue

        updates = []
        params = []
        if read_scopes is not None:
            updates.append("read_scopes_json = ?")
            params.append(json.dumps(read_scopes))
        if write_scopes is not None:
            updates.append("write_scopes_json = ?")
            params.append(json.dumps(write_scopes))
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(row["id"])

        conn.execute(f"UPDATE agents SET {', '.join(updates)} WHERE id = ?", params)
        changed += 1
    return changed


def delete_scope_data(conn: sqlite3.Connection, scope: str) -> dict:
    embedding_rows = conn.execute(
        "SELECT id FROM memory_records WHERE scope = ?", (scope,)
    ).fetchall()
    memory_ids = [row["id"] for row in embedding_rows]
    if memory_ids:
        placeholders = ",".join("?" for _ in memory_ids)
        conn.execute(
            f"DELETE FROM memory_embeddings WHERE record_id IN ({placeholders})",
            memory_ids,
        )

    memory_deleted = conn.execute(
        "DELETE FROM memory_records WHERE scope = ?", (scope,)
    ).rowcount
    vault_deleted = conn.execute(
        "DELETE FROM vault_entries WHERE scope = ?", (scope,)
    ).rowcount
    activities_unlinked = conn.execute(
        "UPDATE agent_activity SET memory_scope = NULL, updated_at = CURRENT_TIMESTAMP WHERE memory_scope = ?",
        (scope,),
    ).rowcount
    agents_updated = remove_scope_from_agent_access(conn, scope)

    return {
        "memory_records_deleted": memory_deleted,
        "vault_entries_deleted": vault_deleted,
        "activities_unlinked": activities_unlinked,
        "agents_updated": agents_updated,
    }


def _remove_scope(scopes_json: str, scope: str) -> list[str] | None:
    try:
        scopes = json.loads(scopes_json or "[]")
    except json.JSONDecodeError:
        scopes = []
    if not isinstance(scopes, list) or scope not in scopes:
        return None
    return [item for item in scopes if item != scope]
