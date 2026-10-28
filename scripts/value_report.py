"""Report whether stored Agent Core data reaches agents and gets used.

Reads a database read-only and answers whether the memory an installation stores is worth storing:
does what we store get delivered, does it get read, and which agents get
nothing back. Run it against the live database or a backup:

    python scripts/value_report.py /data/agent-core.db --days 30
    python scripts/value_report.py /data/agent-core.db --json > report.json

Read-through needs the tool_result_fetched audit event, so offloads written
before that event existed are reported as untracked rather than unread.
"""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else None


def delivery(conn, since: str) -> dict:
    """Offloaded tool results, and how many the agent came back to read."""
    tracking_since = conn.execute(
        "SELECT MIN(timestamp) FROM audit_log WHERE action = 'tool_result_fetched'"
    ).fetchone()[0]
    rows = conn.execute(
        """
        SELECT json_extract(o.details_json, '$.tool') AS tool,
               o.resource_id AS handle,
               o.timestamp AS offloaded_at,
               json_extract(o.details_json, '$.total_chars') AS total_chars,
               (SELECT MAX(json_extract(f.details_json, '$.offset')
                         + json_extract(f.details_json, '$.returned_chars'))
                  FROM audit_log f
                 WHERE f.action = 'tool_result_fetched'
                   AND f.resource_id = o.resource_id) AS read_to
          FROM audit_log o
         WHERE o.action = 'tool_result_offloaded' AND o.timestamp >= ?
        """,
        (since,),
    ).fetchall()
    tools: dict = {}
    for row in rows:
        entry = tools.setdefault(
            row["tool"] or "unknown",
            {"offloaded": 0, "chars": 0, "tracked": 0, "read_any": 0, "read_all": 0},
        )
        entry["offloaded"] += 1
        entry["chars"] += row["total_chars"] or 0
        if tracking_since is None or row["offloaded_at"] < tracking_since:
            continue
        entry["tracked"] += 1
        if row["read_to"] is not None:
            entry["read_any"] += 1
            if row["read_to"] >= (row["total_chars"] or 0):
                entry["read_all"] += 1
    for entry in tools.values():
        entry["read_through_pct"] = _pct(entry["read_any"], entry["tracked"])
        entry["fully_read_pct"] = _pct(entry["read_all"], entry["tracked"])
    return {
        "fetch_tracking_since": tracking_since,
        "tools": dict(sorted(tools.items(), key=lambda kv: -kv[1]["offloaded"])),
    }


def search(conn, since: str) -> dict:
    """Per agent: how often a search returns nothing, or nothing inline."""
    rows = conn.execute(
        """
        SELECT actor_id,
               SUM(action = 'memory_search') AS searches,
               SUM(action = 'memory_search'
                   AND json_extract(details_json, '$.results') = 0) AS empty,
               SUM(action = 'tool_result_offloaded'
                   AND json_extract(details_json, '$.tool') = 'memory_search') AS offloaded,
               SUM(action = 'memory_write') AS writes,
               SUM(action = 'memory_feedback') AS feedback
          FROM audit_log
         WHERE timestamp >= ?
           AND action IN ('memory_search', 'tool_result_offloaded',
                          'memory_write', 'memory_feedback')
         GROUP BY actor_id
        HAVING searches > 0 OR writes > 0
         ORDER BY searches DESC
        """,
        (since,),
    ).fetchall()
    agents = []
    for row in rows:
        agents.append(
            {
                "agent": row["actor_id"],
                "searches": row["searches"],
                "empty_pct": _pct(row["empty"], row["searches"]),
                "offloaded_pct": _pct(row["offloaded"], row["searches"]),
                "writes": row["writes"],
                "searches_per_write": round(row["searches"] / row["writes"], 1)
                if row["writes"]
                else None,
                "feedback": row["feedback"],
            }
        )
    return {
        "agents": agents,
        # An agent whose every search comes back empty pays the cost of the
        # calls and gets nothing for it.
        "no_value_agents": [
            a["agent"] for a in agents if a["searches"] >= 5 and a["empty_pct"] == 100.0
        ],
    }


def corpus(conn, since: str) -> dict:
    """Per scope: how much of what is stored has reached an agent at all."""
    rows = conn.execute(
        """
        SELECT scope,
               COUNT(*) AS active,
               SUM(COALESCE(recall_count, 0) = 0) AS never_recalled,
               SUM(last_recalled_at >= ?) AS recalled_in_window,
               SUM(COALESCE(helpful_count, 0) > 0) AS marked_helpful,
               SUM(COALESCE(unhelpful_count, 0) > 0) AS marked_unhelpful
          FROM memory_records
         WHERE record_status = 'active'
         GROUP BY scope
         ORDER BY active DESC
        """,
        (since,),
    ).fetchall()
    scopes = []
    for row in rows:
        scopes.append(
            {
                "scope": row["scope"],
                "active": row["active"],
                "never_recalled_pct": _pct(row["never_recalled"], row["active"]),
                "recalled_in_window_pct": _pct(row["recalled_in_window"] or 0, row["active"]),
                "marked_helpful": row["marked_helpful"],
                "marked_unhelpful": row["marked_unhelpful"],
            }
        )
    return {"scopes": scopes}


def injection(conn, since: str) -> dict:
    """Session context injected at start, and whether its records were fetched later.

    A fetch (memory_get by id) after an injection that listed the record is the
    evidence that the index pointed the agent at something it then used.
    """
    rows = conn.execute(
        """SELECT actor_id, timestamp, details_json FROM audit_log
        WHERE action = 'context_injected' AND timestamp >= ? ORDER BY timestamp""",
        (since,),
    ).fetchall()
    fetches = conn.execute(
        """SELECT actor_id, timestamp, details_json FROM audit_log
        WHERE action = 'memory_fetched' AND timestamp >= ?""",
        (since,),
    ).fetchall()
    fetched_after: dict = {}
    for row in fetches:
        for record_id in json.loads(row["details_json"] or "{}").get("ids", []):
            fetched_after.setdefault((row["actor_id"], record_id), []).append(row["timestamp"])
    agents: dict = {}
    for row in rows:
        details = json.loads(row["details_json"] or "{}")
        entry = agents.setdefault(
            row["actor_id"], {"sessions": 0, "chars": 0, "records_listed": 0, "records_fetched": 0}
        )
        entry["sessions"] += 1
        entry["chars"] += details.get("chars", 0)
        for record_id in details.get("memory_ids", []):
            entry["records_listed"] += 1
            if any(ts >= row["timestamp"] for ts in fetched_after.get((row["actor_id"], record_id), [])):
                entry["records_fetched"] += 1
    for entry in agents.values():
        entry["avg_chars"] = round(entry["chars"] / entry["sessions"]) if entry["sessions"] else 0
        entry["fetched_pct"] = _pct(entry["records_fetched"], entry["records_listed"])
    return {"agents": agents}


def sync_noise(conn, since: str) -> dict:
    """Workspace changes by type, and how often one resource repeats."""
    rows = conn.execute(
        """
        SELECT change_type, COUNT(*) AS n
          FROM workspace_changes WHERE created_at >= ?
         GROUP BY change_type ORDER BY n DESC
        """,
        (since,),
    ).fetchall()
    total = sum(row["n"] for row in rows)
    distinct = conn.execute(
        "SELECT COUNT(DISTINCT resource_type || ':' || resource_id) "
        "FROM workspace_changes WHERE created_at >= ?",
        (since,),
    ).fetchone()[0]
    return {
        "changes": total,
        "distinct_resources": distinct,
        "changes_per_resource": round(total / distinct, 1) if distinct else None,
        "by_type": {row["change_type"]: _pct(row["n"], total) for row in rows},
    }


def build_report(path: Path, days: int) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    with _connect(path) as conn:
        return {
            "window_days": days,
            "since": since,
            "delivery": delivery(conn, since),
            "search": search(conn, since),
            "injection": injection(conn, since),
            "corpus": corpus(conn, since),
            "sync": sync_noise(conn, since),
        }


def _table(headers, rows):
    widths = [
        max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h))
        for i, h in enumerate(headers)
    ]
    lines = ["  ".join(str(h).ljust(w) for h, w in zip(headers, widths))]
    lines += ["  ".join(str(v).ljust(w) for v, w in zip(r, widths)) for r in rows]
    return "\n".join(lines)


def _show(value):
    return "-" if value is None else value


def render(report: dict) -> str:
    out = [f"Agent Core value report: last {report['window_days']} days (since {report['since']})"]

    delivery_ = report["delivery"]
    out.append("\nOffloaded results and read-through")
    if delivery_["fetch_tracking_since"] is None:
        out.append("  result_fetch is not tracked yet; read-through shows '-'.")
    else:
        out.append(f"  read-through tracked since {delivery_['fetch_tracking_since']}")
    out.append(
        _table(
            ["tool", "offloaded", "chars", "tracked", "read %", "fully read %"],
            [
                [
                    tool,
                    e["offloaded"],
                    e["chars"],
                    e["tracked"],
                    _show(e["read_through_pct"]),
                    _show(e["fully_read_pct"]),
                ]
                for tool, e in delivery_["tools"].items()
            ],
        )
    )

    search_ = report["search"]
    out.append("\nSearch outcomes by agent")
    out.append(
        _table(
            ["agent", "searches", "empty %", "offloaded %", "writes", "searches/write", "feedback"],
            [
                [
                    a["agent"],
                    a["searches"],
                    _show(a["empty_pct"]),
                    _show(a["offloaded_pct"]),
                    a["writes"],
                    _show(a["searches_per_write"]),
                    a["feedback"],
                ]
                for a in search_["agents"]
            ],
        )
    )
    if search_["no_value_agents"]:
        out.append("  Every search came back empty for: " + ", ".join(search_["no_value_agents"]))

    out.append("\nSession context injected at start")
    agents = report["injection"]["agents"]
    if not agents:
        out.append("  none yet")
    else:
        out.append(
            _table(
                ["agent", "sessions", "avg chars", "records listed", "later fetched %"],
                [
                    [name, e["sessions"], e["avg_chars"], e["records_listed"], _show(e["fetched_pct"])]
                    for name, e in sorted(agents.items(), key=lambda kv: -kv[1]["sessions"])
                ],
            )
        )

    out.append("\nStored memory that reaches agents (active records)")
    out.append(
        _table(
            ["scope", "active", "never recalled %", "recalled in window %", "helpful", "unhelpful"],
            [
                [
                    s["scope"],
                    s["active"],
                    _show(s["never_recalled_pct"]),
                    _show(s["recalled_in_window_pct"]),
                    s["marked_helpful"],
                    s["marked_unhelpful"],
                ]
                for s in report["corpus"]["scopes"]
            ],
        )
    )

    sync = report["sync"]
    out.append("\nWorkspace sync stream")
    out.append(
        f"  {sync['changes']} changes across {sync['distinct_resources']} resources "
        f"({_show(sync['changes_per_resource'])} per resource)"
    )
    for change_type, pct in sync["by_type"].items():
        out.append(f"  {change_type}: {pct}%")
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("database", type=Path)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be positive")
    if not args.database.exists():
        parser.error(f"{args.database} does not exist")
    report = build_report(args.database, args.days)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
