"""Memory hygiene: records that are plainly noise retract themselves.

Four kinds of record are retracted without asking a person, because a person answering for them was
right every time it was measured (13 of 13 answered, and 33 of 33 for a definitively missing anchor):

* ``episodic_log``       per-occurrence logs (a heartbeat, a tick, an "idle run") that predate automatic expiry
* ``ticket_closeout``    ticket-closeout narration ("PROJ-594 closed done ...")
* ``duplicate_cluster``  the same text written twice (not merely an alike opening); the newest is kept.
                         Never a decision, and never a record written in the last 24 hours
* ``anchor_missing``     a fact whose anchor was definitively gone in two checks at least an hour apart
                         (decided in verification_service, which calls ``retract_for_rule`` below)

A retraction is soft: the record stays restorable for the retention period (``retracted_retention_days``),
it is audited as ``memory_auto_retracted`` with the rule name as the actor, and each run is capped. Nothing
here asks anyone anything, and nothing is retracted by a judgement call about whether a memory is useful.
"""

import logging
import re
from typing import Optional

from app.database import get_db
from app.services import audit_service, memory_service
from app.time_utils import parse_utc_datetime, utc_now

logger = logging.getLogger(__name__)

RULES = ("episodic_log", "ticket_closeout", "duplicate_cluster", "anchor_missing")
# How many records one run may retract in all (the scheduled run is hourly).
CAP_PER_RUN = 20
# A burst of related writes is normal, so a duplicate is only retracted once it is a day old.
DUPLICATE_MIN_AGE_HOURS = 24

# A closeout announces itself in its opening words: "PROJ-594 closed done 2026-07-21".
# Anchored to the lead of the record: a record that merely mentions a closed ticket is about work.
TICKET_CLOSEOUT = re.compile(
    r"^\s*(?:#+\s*)?[A-Z]{2,6}-\d+\b[^.\n]{0,60}\bclosed\s+(?:done|in_review)\b", re.I
)
CLOSEOUT_LEAD_CHARS = 200


def _active_records() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {memory_service.MEMORY_RECORD_COLUMNS} FROM memory_records "
            "WHERE record_status = 'active' ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def _episodic_log(records: list[dict]) -> list[tuple[str, str]]:
    found = []
    for record in records:
        if record.get("expires_at"):
            continue
        if memory_service.detect_expiring_episodic_shape(record.get("content") or "", record.get("topic")):
            found.append(("episodic_log", record["id"]))
    return found


def _ticket_closeout(records: list[dict]) -> list[tuple[str, str]]:
    found = []
    for record in records:
        if record.get("expires_at"):
            continue
        if memory_service.detect_expiring_episodic_shape(record.get("content") or "", record.get("topic")):
            continue  # already an episodic_log
        if memory_service.is_bare_ticket_closeout(record.get("content") or ""):
            found.append(("ticket_closeout", record["id"]))
    return found


def _older_than_a_day(record: dict) -> bool:
    try:
        created = parse_utc_datetime(record["created_at"])
    except Exception:
        return False
    return (utc_now() - created).total_seconds() >= DUPLICATE_MIN_AGE_HOURS * 3600


def _duplicate_cluster(records: list[dict]) -> list[tuple]:
    """The same statement written twice within one scope: all but the newest. A decision is never retracted by a rule
    (only a person revises one), records that merely open alike are not duplicates, and neither are records whose
    difference is a number, identifier, path or name. Each result names the record it duplicates."""
    clusters: dict[tuple, list[dict]] = {}
    for record in records:
        if record.get("memory_class") == "decision":
            continue
        normalized = " ".join((record.get("content") or "").split()).lower()
        if len(normalized) < memory_service.DUPLICATE_PREFIX_CHARS:
            continue
        clusters.setdefault((record["scope"], normalized[: memory_service.DUPLICATE_PREFIX_CHARS]), []).append(record)
    found = []
    for group in clusters.values():
        if len(group) < 2:
            continue
        kept: list[dict] = []
        for r in sorted(group, key=lambda r: r.get("created_at") or "", reverse=True):
            twin = next((k for k in kept if memory_service.is_duplicate_text(r.get("content") or "", k.get("content") or "")), None)
            if twin is None:
                kept.append(r)
            elif _older_than_a_day(r):
                found.append(("duplicate_cluster", r["id"], {"duplicate_of": twin["id"]}))
    return found


def find_candidates() -> list[tuple]:
    """(rule, record id[, detail]) for every record the scan rules would retract, without retracting. A record a person
    wrote, restored or confirmed is not a candidate."""
    records = _active_records()
    found = _episodic_log(records) + _ticket_closeout(records) + _duplicate_cluster(records)
    protected = memory_service.touched_by_a_person([item[1] for item in found])
    return [item for item in found if item[1] not in protected]


def retract_for_rule(rule: str, record_id: str, *, detail: Optional[dict] = None) -> bool:
    """Retract one record on behalf of a rule: the rule is the actor, and the audit entry names it. Never a record a
    person wrote, restored or confirmed."""
    if rule not in RULES:
        raise ValueError(f"unknown hygiene rule: {rule}")
    record = memory_service.get_memory_record(record_id)
    if not record or record.get("record_status") != "active":
        return False
    if memory_service.touched_by_a_person([record_id]):
        return False
    if not memory_service.retract_memory(record_id, retracted_by=f"rule:{rule}"):
        return False
    audit_service.write_event(
        actor_type="system",
        actor_id=f"rule:{rule}",
        action="memory_auto_retracted",
        resource_type="memory_record",
        resource_id=record_id,
        result="success",
        details={"rule": rule, "scope": record.get("scope"), "restorable": True, **(detail or {})},
    )
    return True


def run(cap: int = CAP_PER_RUN) -> dict:
    """Retract what the three scan rules find, at most ``cap`` records. Safe to run any time."""
    retracted: dict[str, int] = {}
    skipped = 0
    for rule, record_id, *extra in find_candidates():
        if sum(retracted.values()) >= cap:
            skipped += 1
            continue
        if retract_for_rule(rule, record_id, detail=extra[0] if extra else None):
            retracted[rule] = retracted.get(rule, 0) + 1
    return {"retracted": retracted, "total": sum(retracted.values()), "left_for_next_run": skipped}
