#!/usr/bin/env python3
"""Read-only history check for a live install: can a fresh agent answer from Core alone?

For every workspace with task activity in the last N days (default 1) it reports, from the database:
  * what each agent is working on now (open tasks),
  * what each agent finished (completed tasks with a result, and how many finished without one),
  * how many memories were written there,
and flags the gaps a fresh agent would hit: finished work with no result, and active workspaces
with no memory written at all. It reads a copy-safe, read-only connection and prints counts and
names only, never memory text or secrets.

    python3 scripts/acceptance_live.py <data folder>/agent-core.db [days]

Then ask a fresh agent the three questions for a workspace both agents work in:
  1. What is each agent working on right now?   2. What did <agent> finish and what is left?
  3. What did <agent> decide? (recall one memory the other wrote)
"""

import sqlite3
import sys


def main(path: str, days: int = 1) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    since = f"-{days} days"
    workspaces = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT memory_scope FROM agent_activity WHERE memory_scope LIKE 'workspace:%' "
            "AND updated_at >= datetime('now', ?)", (since,)
        )
    ]
    gaps = 0
    for ws in sorted(workspaces):
        print(f"\n{ws}")
        for r in conn.execute(
            "SELECT assigned_agent_id AS agent, status, COUNT(*) n, "
            "SUM(task_result IS NOT NULL AND trim(task_result) <> '') with_result "
            "FROM agent_activity WHERE memory_scope = ? AND updated_at >= datetime('now', ?) "
            "GROUP BY 1, 2 ORDER BY 1, 2", (ws, since),
        ):
            print(f"  {r['agent']:<28} {r['status']:<10} {r['n']:>3} tasks, {r['with_result'] or 0} with a result")
            if r["status"] == "completed" and (r["with_result"] or 0) < r["n"]:
                gaps += r["n"] - (r["with_result"] or 0)
        memories = conn.execute(
            "SELECT COUNT(*) FROM memory_records WHERE scope = ? AND created_at >= datetime('now', ?)", (ws, since)
        ).fetchone()[0]
        print(f"  memories written in the window: {memories}")
        if memories == 0:
            gaps += 1
            print("  GAP: active workspace, nothing written to memory")
    print(f"\n{len(workspaces)} active workspaces, {gaps} gap(s)")
    return 0 if gaps == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1))
