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
        cursor = conn.execute(
            """
            INSERT INTO workspaces (id, name, description, owner_user_id)
            VALUES (?, ?, ?, ?)
            """,
            (normalized_id, name, description, owner_user_id),
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
    owner_user_id: Optional[str] = None, is_active: Optional[bool] = None
) -> list[dict]:
    with get_db() as conn:
        query = "SELECT id, name, description, owner_user_id, is_active, created_at FROM workspaces WHERE 1=1"
        params = []

        if owner_user_id:
            query += " AND owner_user_id = ?"
            params.append(owner_user_id)
        if is_active is not None:
            query += " AND is_active = ?"
            params.append(is_active)

        query += " ORDER BY created_at DESC"
        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


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
