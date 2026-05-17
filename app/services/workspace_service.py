from typing import Optional, Iterable

from app.database import get_db
from app.models.enums import normalize_id
from app.services import cleanup_service


def create_workspace(
    workspace_id: str,
    name: str,
    owner_user_id: str,
    description: str = "",
) -> dict:
    normalized_id = normalize_id(workspace_id)

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO workspaces (id, name, description, owner_user_id)
            VALUES (?, ?, ?, ?)
            """,
            (normalized_id, name, description, owner_user_id),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO workspace_collaborators
            (workspace_id, user_id, role, can_read, can_write, created_by)
            VALUES (?, ?, 'owner', 1, 1, ?)
            """,
            (normalized_id, owner_user_id, owner_user_id),
        )
        conn.commit()

        return {
            "id": normalized_id,
            "name": name,
            "description": description,
            "owner_user_id": owner_user_id,
            "is_active": True,
        }


def get_workspace_by_id(workspace_id: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.execute(
            """
            SELECT id, name, description, owner_user_id, is_active, created_at, updated_at
            FROM workspaces WHERE id = ?
            """,
            (workspace_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def list_workspaces(
    owner_user_id: Optional[str] = None,
    is_active: Optional[bool] = None,
    user_id: Optional[str] = None,
) -> list[dict]:
    with get_db() as conn:
        query = """
            SELECT DISTINCT w.id, w.name, w.description, w.owner_user_id, w.is_active, w.created_at
            FROM workspaces w
        """
        params = []
        subject_user_id = user_id or owner_user_id
        if subject_user_id:
            query += """
                LEFT JOIN workspace_collaborators wc
                  ON wc.workspace_id = w.id AND wc.user_id = ?
            """
            params.append(subject_user_id)
        query += " WHERE 1=1"
        if owner_user_id is not None and user_id is None:
            query += " AND (w.owner_user_id = ? OR wc.user_id IS NOT NULL)"
            params.append(owner_user_id)
        elif owner_user_id is not None:
            query += " AND w.owner_user_id = ?"
            params.append(owner_user_id)
        if is_active is not None:
            query += " AND w.is_active = ?"
            params.append(is_active)

        query += " ORDER BY w.created_at DESC"
        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def list_accessible_workspaces(user_id: str, is_active: Optional[bool] = None) -> list[dict]:
    return list_workspaces(is_active=is_active, user_id=user_id)


def list_workspace_collaborators(workspace_id: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT workspace_id, user_id, role, can_read, can_write, created_by, created_at, updated_at
            FROM workspace_collaborators
            WHERE workspace_id = ?
            ORDER BY role = 'owner' DESC, user_id ASC
            """,
            (normalize_id(workspace_id),),
        ).fetchall()
        return [dict(row) for row in rows]


def can_user_read_workspace(user_id: str, workspace_id: str) -> bool:
    workspace = get_workspace_by_id(workspace_id)
    if not workspace or not workspace.get("is_active", False):
        return False
    if workspace.get("owner_user_id") == user_id:
        return True
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM workspace_collaborators
            WHERE workspace_id = ? AND user_id = ? AND can_read = 1
            LIMIT 1
            """,
            (normalize_id(workspace_id), user_id),
        ).fetchone()
    return bool(row)


def can_user_write_workspace(user_id: str, workspace_id: str) -> bool:
    workspace = get_workspace_by_id(workspace_id)
    if not workspace or not workspace.get("is_active", False):
        return False
    if workspace.get("owner_user_id") == user_id:
        return True
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM workspace_collaborators
            WHERE workspace_id = ? AND user_id = ? AND can_write = 1
            LIMIT 1
            """,
            (normalize_id(workspace_id), user_id),
        ).fetchone()
    return bool(row)


def upsert_workspace_collaborator(
    workspace_id: str,
    user_id: str,
    can_read: bool = True,
    can_write: bool = False,
    created_by: Optional[str] = None,
) -> None:
    workspace_id = normalize_id(workspace_id)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO workspace_collaborators
            (workspace_id, user_id, role, can_read, can_write, created_by)
            VALUES (?, ?, 'editor', ?, ?, ?)
            ON CONFLICT(workspace_id, user_id) DO UPDATE SET
              can_read = excluded.can_read,
              can_write = excluded.can_write,
              created_by = COALESCE(excluded.created_by, workspace_collaborators.created_by),
              updated_at = CURRENT_TIMESTAMP
            """,
            (
                workspace_id,
                user_id,
                1 if can_read else 0,
                1 if can_write else 0,
                created_by,
            ),
        )
        conn.commit()


def remove_workspace_collaborator(workspace_id: str, user_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            """
            DELETE FROM workspace_collaborators
            WHERE workspace_id = ? AND user_id = ? AND role != 'owner'
            """,
            (normalize_id(workspace_id), user_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def get_accessible_workspace_ids(user_id: str, workspace_ids: Iterable[str]) -> frozenset[str]:
    normalized_ids = [normalize_id(workspace_id) for workspace_id in workspace_ids if workspace_id]
    if not normalized_ids:
        return frozenset()

    placeholders = ",".join("?" for _ in normalized_ids)
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT w.id
            FROM workspaces w
            LEFT JOIN workspace_collaborators wc
              ON wc.workspace_id = w.id AND wc.user_id = ?
            WHERE w.id IN ({placeholders})
              AND w.is_active = 1
              AND (w.owner_user_id = ? OR wc.user_id IS NOT NULL)
            """,
            [user_id, *normalized_ids, user_id],
        ).fetchall()
        return frozenset(row["id"] for row in rows)


def get_workspace_ids_with_bindings() -> frozenset[str]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT scope FROM connector_bindings WHERE scope LIKE 'workspace:%'",
        ).fetchall()
        return frozenset(
            scope.split(":", 1)[1]
            for row in rows
            if (scope := row["scope"]).startswith("workspace:")
        )


def get_active_workspace_ids(workspace_ids: Iterable[str]) -> frozenset[str]:
    normalized_ids = [
        normalize_id(workspace_id) for workspace_id in workspace_ids if workspace_id
    ]
    if not normalized_ids:
        return frozenset()

    placeholders = ",".join("?" for _ in normalized_ids)
    query = f"SELECT id FROM workspaces WHERE id IN ({placeholders}) AND is_active = 1"
    with get_db() as conn:
        rows = conn.execute(query, normalized_ids).fetchall()
        return frozenset(row["id"] for row in rows)


def update_workspace(
    workspace_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> bool:
    updates = []
    params = []

    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if description is not None:
        updates.append("description = ?")
        params.append(description)
    if is_active is not None:
        updates.append("is_active = ?")
        params.append(int(is_active))

    if not updates:
        return False

    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(workspace_id)

    with get_db() as conn:
        cursor = conn.execute(
            f"UPDATE workspaces SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        conn.commit()
        return cursor.rowcount > 0


def deactivate_workspace(workspace_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE workspaces SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (workspace_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def reactivate_workspace(workspace_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE workspaces SET is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (workspace_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_workspace_hard(workspace_id: str) -> bool:
    normalized_id = normalize_id(workspace_id)
    scope = f"workspace:{normalized_id}"
    with get_db() as conn:
        cleanup_service.delete_scope_data(conn, scope)
        cursor = conn.execute("DELETE FROM workspaces WHERE id = ?", (normalized_id,))
        conn.commit()
        return cursor.rowcount > 0
