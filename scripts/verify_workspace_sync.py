"""Rerunnable smoke check for the workspace synchronization persistence path."""

import tempfile
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.database as database
from app.database import get_db, init_db
from app.services import memory_service, workspace_sync_service


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database.DB_PATH_OVERRIDE = str(Path(directory) / "sync-smoke.db")
        init_db()
        memory_service.write_memory("sync smoke", "fact", "workspace:smoke")
        with get_db() as conn:
            change = conn.execute(
                "SELECT change_type, resource_type FROM workspace_changes"
            ).fetchone()
        assert dict(change) == {"change_type": "memory_written", "resource_type": "memory"}
        first = workspace_sync_service.sync_workspace(
            agent_id="smoke", user_id="owner", memory_scope="workspace:smoke"
        )
        assert first["memory_changes"]
        workspace_sync_service.acknowledge(
            agent_id="smoke", user_id="owner", execution_id=first["execution_id"],
            memory_scope="workspace:smoke", cursor=first["next_cursor"],
        )
        second = workspace_sync_service.sync_workspace(
            agent_id="smoke", user_id="owner", memory_scope="workspace:smoke",
            execution_id=first["execution_id"],
        )
        assert not second["memory_changes"]
        print("workspace sync smoke check passed")


if __name__ == "__main__":
    main()
