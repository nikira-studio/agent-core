import asyncio
import re
from dataclasses import dataclass
from typing import Any

from app.models.enums import MEMORY_CLASSES, SOURCE_KINDS
from app.security.effective_authority import EffectiveAuthority
from app.security.pii_detector import contains_pii
from app.services import audit_service, memory_service, workspace_sync_service


@dataclass(frozen=True)
class MemoryOperationError(Exception):
    code: str
    message: str
    status_code: int


def validate_search_query(query: str) -> str:
    text = query.strip()
    if len(text) <= 2:
        raise MemoryOperationError("QUERY_TOO_SHORT", "Query must be at least 3 characters", 400)
    if re.match(r"^(the|a|an|is|are|was|were|i|you|he|she|it|we|they)\s*$", text, re.I):
        raise MemoryOperationError("QUERY_NOISE", "Query is too trivial", 400)
    if re.match(r"^[.,;:!?]+$", text) or contains_pii(text):
        raise MemoryOperationError("QUERY_NOISE", "Query is too trivial or contains a credential-like pattern", 400)
    return text


async def write_memory(
    values: dict[str, Any],
    authority: EffectiveAuthority,
    *,
    channel: str,
    route: str,
) -> dict[str, Any]:
    scope = values["scope"]
    memory_class = values["memory_class"]
    source_kind = values.get("source_kind", "agent_inference")
    confidence = values.get("confidence", 0.5)
    importance = values.get("importance", 0.5)
    if not authority.can("memory", "write", scope=scope):
        raise MemoryOperationError("SCOPE_DENIED", "Access denied to this scope", 403)
    if memory_class not in MEMORY_CLASSES:
        raise MemoryOperationError("INVALID_CLASS", f"memory_class must be one of {MEMORY_CLASSES}", 400)
    if source_kind not in SOURCE_KINDS:
        raise MemoryOperationError("INVALID_SOURCE_KIND", f"source_kind must be one of {SOURCE_KINDS}", 400)
    if not 0.0 <= confidence <= 1.0:
        raise MemoryOperationError("INVALID_CONFIDENCE", "confidence must be between 0.0 and 1.0", 400)
    if not 0.0 <= importance <= 1.0:
        raise MemoryOperationError("INVALID_IMPORTANCE", "importance must be between 0.0 and 1.0", 400)

    supersedes_id = values.get("supersedes_id")
    if supersedes_id:
        old = memory_service.get_memory_record(supersedes_id)
        if not old:
            raise MemoryOperationError("NOT_FOUND", "Record to supersede not found", 404)
        if old["record_status"] != "active":
            raise MemoryOperationError("INVALID_SUPERSESSION", "Cannot supersede non-active record", 400)
        if not authority.can("memory", "write", scope=old["scope"]):
            raise MemoryOperationError("SCOPE_DENIED", "Access denied to scope of record being superseded", 403)

    execution_id = values.get("execution_id")
    if execution_id:
        try:
            workspace_sync_service.validate_execution(
                execution_id=execution_id,
                agent_id=authority.agent_id or "",
                user_id=authority.user_id or "",
                memory_scope=scope,
            )
        except PermissionError as exc:
            raise MemoryOperationError("EXECUTION_OWNERSHIP", "Execution belongs to another agent", 403) from exc
        except ValueError as exc:
            raise MemoryOperationError(str(exc), "Invalid execution", 400) from exc

    provenance = memory_service.provenance_for_write(
        actor_type=authority.actor_type, actor_id=authority.actor_id,
        channel=channel, route=route, source_kind=source_kind, scope=scope,
        user_id=authority.user_id, agent_id=authority.agent_id,
        extras=authority.safe_attribution(),
    )
    try:
        record, pii_flag = await asyncio.to_thread(
            memory_service.write_memory,
            content=values["content"], memory_class=memory_class, scope=scope,
            topic=values.get("topic"), confidence=confidence, importance=importance,
            source_kind=source_kind, supersedes_id=supersedes_id,
            provenance_json=provenance, subject_anchor=values.get("subject_anchor"),
            slot_key=values.get("slot_key"), valid_from=values.get("valid_from"),
            valid_to=values.get("valid_to"), last_confirmed_at=values.get("last_confirmed_at"),
            expires_at=values.get("expires_at"), source_execution_id=execution_id,
        )
    except ValueError as exc:
        raise MemoryOperationError("INVALID_INPUT", str(exc), 400) from exc
    if pii_flag == "PII_DETECTED":
        raise MemoryOperationError("PII_DETECTED", "Content contains PII and cannot be written to shared scope", 422)

    audit_details = {
        "record_id": record["id"],
        "memory_class": memory_class,
        "scope": scope,
        "action": "create",
        "source_kind": source_kind,
    }
    if record.get("topic"):
        audit_details["topic"] = record["topic"]
    if record.get("slot_key"):
        audit_details["slot_key"] = record["slot_key"]
    audit_service.write_event(
        actor_type=authority.actor_type, actor_id=authority.actor_id,
        action="memory_write", resource_type="memory_record",
        resource_id=record["id"], result="success",
        details=audit_details,
    )
    payload = {"record": record}
    warnings = await asyncio.to_thread(
        memory_service.assess_memory_write, content=values["content"], scope=scope,
        memory_class=memory_class, topic=values.get("topic"), exclude_id=record["id"],
        subject_anchor=values.get("subject_anchor"),
    )
    if warnings:
        payload["warnings"] = warnings
    return payload
