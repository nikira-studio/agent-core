"""Bounded retention for credential-resolution audit events.

Successful resolution events remain individually inspectable for 30 days, then
move atomically into daily counts by consumer, action, and credential entry.
Security-relevant denials, grants, credential changes, and key events remain
individual audit rows for 13 calendar months.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone
import calendar

from app.database import get_db
from app.services import audit_service


RESOLUTION_ACTIONS = ("credential_consumer_resolved", "broker_resolve")
RESOLUTION_RETENTION_DAYS = 30
AUDIT_RETENTION_MONTHS = 13
DEFAULT_BATCH_SIZE = 25_000
DELETE_CHUNK_SIZE = 500

# Keep these event types intact until the 13-month boundary. In particular,
# do not collapse denials, grants/revocations, credential mutations, or key
# lifecycle events into the success rollup.
INDIVIDUAL_CREDENTIAL_ACTIONS = (
    "scope_grant",
    "scope_denied",
    "workspace_collaborator_upserted",
    "workspace_collaborator_removed",
    "credential_consumer_denied",
    "credential_consumer_grant_created",
    "credential_consumer_grant_revoked",
    "credential_consumer_written",
    "credential_entry_created",
    "credential_entry_updated",
    "credential_entry_moved",
    "credential_entry_deleted",
    "credential_reference",
    "credential_reveal",
    "credential_delete",
    "credential_key_rotated",
    "credential_key_restored",
    "credential_resolution_retention_applied",
    "agent_key_rotated",
    "inbound_key_generated",
    "inbound_key_rotated",
    "connector_binding_created",
    "connector_binding_updated",
    "connector_binding_deleted",
)


def _months_before(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def apply_retention(
    *, now: datetime | None = None, batch_size: int = DEFAULT_BATCH_SIZE
) -> dict[str, int | bool]:
    """Compact one bounded batch and expire credential audit data past policy.

    The rollup upserts and source-row deletes share one transaction. A retry
    therefore cannot double-count already deleted source rows.
    """
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    resolution_cutoff = _timestamp(now - timedelta(days=RESOLUTION_RETENTION_DAYS))
    individual_cutoff = _timestamp(_months_before(now, AUDIT_RETENTION_MONTHS))
    aggregate_cutoff_day = individual_cutoff[:10]

    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        marks = ",".join("?" for _ in RESOLUTION_ACTIONS)
        rows = conn.execute(
            f"""SELECT id, substr(timestamp, 1, 10) AS resolution_date,
                       action, actor_type, COALESCE(actor_id, '') AS actor_id,
                       COALESCE(resource_id, '') AS credential_entry_id
                FROM audit_log
                WHERE action IN ({marks}) AND result = 'success' AND timestamp < ?
                ORDER BY timestamp, id LIMIT ?""",
            (*RESOLUTION_ACTIONS, resolution_cutoff, batch_size),
        ).fetchall()

        grouped = Counter(
            (
                row["resolution_date"],
                row["action"],
                row["actor_type"],
                row["actor_id"],
                row["credential_entry_id"],
            )
            for row in rows
        )
        for key, count in grouped.items():
            conn.execute(
                """INSERT INTO credential_resolution_daily
                   (resolution_date, action, actor_type, actor_id,
                    credential_entry_id, success_count)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (resolution_date, action, actor_type, actor_id,
                                credential_entry_id)
                   DO UPDATE SET success_count = success_count + excluded.success_count""",
                (*key, count),
            )

        ids = [row["id"] for row in rows]
        for offset in range(0, len(ids), DELETE_CHUNK_SIZE):
            chunk = ids[offset : offset + DELETE_CHUNK_SIZE]
            conn.execute(
                f"DELETE FROM audit_log WHERE id IN ({','.join('?' for _ in chunk)})",
                chunk,
            )

        protected_marks = ",".join("?" for _ in INDIVIDUAL_CREDENTIAL_ACTIONS)
        protected_rows = conn.execute(
            f"""SELECT id FROM audit_log
                WHERE timestamp < ? AND (
                    action IN ({protected_marks}) OR
                    result IN ('failure', 'blocked') OR
                    (action IN ({marks}) AND result <> 'success')
                )
                ORDER BY timestamp, id LIMIT ?""",
            (
                individual_cutoff,
                *INDIVIDUAL_CREDENTIAL_ACTIONS,
                *RESOLUTION_ACTIONS,
                batch_size,
            ),
        ).fetchall()
        protected_ids = [row["id"] for row in protected_rows]
        for offset in range(0, len(protected_ids), DELETE_CHUNK_SIZE):
            chunk = protected_ids[offset : offset + DELETE_CHUNK_SIZE]
            conn.execute(
                f"DELETE FROM audit_log WHERE id IN ({','.join('?' for _ in chunk)})",
                chunk,
            )

        aggregate_cursor = conn.execute(
            """DELETE FROM credential_resolution_daily
               WHERE resolution_date < ?""",
            (aggregate_cutoff_day,),
        )
        aggregates_pruned = aggregate_cursor.rowcount or 0
        backlog = conn.execute(
            f"""SELECT 1 FROM audit_log
                WHERE action IN ({marks}) AND result = 'success' AND timestamp < ?
                LIMIT 1""",
            (*RESOLUTION_ACTIONS, resolution_cutoff),
        ).fetchone()

        counts = {
            "resolution_rows_compacted": len(rows),
            "credential_audit_events_pruned": len(protected_ids),
            "resolution_aggregate_rows_pruned": aggregates_pruned,
            "batch_size": batch_size,
            "resolution_compaction_backlog": bool(backlog),
        }
        if any(counts[key] for key in (
            "resolution_rows_compacted",
            "credential_audit_events_pruned",
            "resolution_aggregate_rows_pruned",
        )):
            audit_service.write_event_in_connection(
                conn,
                actor_type="system",
                actor_id="maintenance",
                action="credential_resolution_retention_applied",
                result="success",
                details={
                    "resolution_rows_compacted": len(rows),
                    "protected_events_pruned": len(protected_ids),
                    "resolution_aggregate_rows_pruned": aggregates_pruned,
                    "resolution_retention_days": RESOLUTION_RETENTION_DAYS,
                    "individual_audit_retention_months": AUDIT_RETENTION_MONTHS,
                    "batch_size": batch_size,
                },
            )
        conn.commit()

    return counts
