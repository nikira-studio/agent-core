import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from app.models.enums import MEMORY_CLASSES, SOURCE_KINDS
from app.security.effective_authority import EffectiveAuthority
from app.security.pii_detector import contains_pii
from app.services import audit_service, memory_service, workspace_sync_service


@dataclass
class MemoryOperationError(Exception):
    code: str
    message: str
    status_code: int


def validate_response_budget(value: Any) -> Optional[int]:
    """Validate caller input once at the search boundary."""
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise MemoryOperationError(
            "INVALID_RESPONSE_BUDGET",
            "max_response_chars must be a positive integer or null",
            400,
        )
    return value


def budget_search_response(data: dict, max_response_chars: Optional[int]) -> dict:
    """Keep a whole-record prefix within the JSON-RPC logical-text budget.

    Projection and pagination have already run. No database writes here: callers
    record recall only after this function selects the delivered response.
    """
    if max_response_chars is None:
        return data

    def size(payload: dict) -> int:
        return len(json.dumps(payload, indent=2, default=str))

    full = {**data, "response_limited": False}
    if size(full) <= max_response_chars:
        return full

    records = data["records"]
    if records:
        # Find the longest prefix, including every metadata field in each check.
        low, high = 1, len(records) - 1
        best = None
        while low <= high:
            count = (low + high) // 2
            candidate = {
                **data,
                "records": records[:count],
                "total": count,
                "response_limited": True,
            }
            if size(candidate) <= max_response_chars:
                best = candidate
                low = count + 1
            else:
                high = count - 1
        if best is not None:
            return best
        empty = {
            **data,
            "records": [],
            "total": 0,
            "response_limited": True,
            "warnings": [
                {
                    "code": "RESPONSE_BUDGET_TOO_SMALL",
                    "message": "Matching memories exist, but the first record does not fit. Raise max_response_chars.",
                }
            ],
        }
        if size(empty) <= max_response_chars:
            return empty
    raise MemoryOperationError(
        "RESPONSE_BUDGET_TOO_SMALL",
        "max_response_chars cannot fit the required response metadata",
        400,
    )


def fetch_memory_by_ids(ids: Any, ctx: EffectiveAuthority, *, view: Optional[str], channel: str) -> dict:
    """The index-then-fetch half of session context: full records by id.

    Records the caller cannot read are reported as not found, the same as ids
    that do not exist, so the call cannot be used to probe other scopes. A fetch
    is the strongest signal that a record reached an agent, so it counts as a
    recall and is audited with the ids.
    """
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise MemoryOperationError("INVALID_IDS", "ids must be a non-empty list of record ids", 400)
    if len(ids) > memory_service.MAX_FETCH_IDS:
        raise MemoryOperationError(
            "TOO_MANY_IDS", f"at most {memory_service.MAX_FETCH_IDS} ids per call", 400
        )
    records = [
        r
        for r in memory_service.get_memory_records_by_ids(ids)
        if ctx.can("memory", "read", scope=r["scope"])
    ]
    found = {r["id"] for r in records}
    if view != "full":
        records = [memory_service.lean_record(r) for r in records]
    memory_service.record_recall(list(found))
    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="memory_fetched",
        resource_type="memory",
        resource_id=None,
        result="success",
        details={"ids": [i for i in ids if i in found], "missing": len(set(ids) - found), "channel": channel},
    )
    return {
        "records": records,
        "total": len(records),
        "not_found": [i for i in dict.fromkeys(ids) if i not in found],
    }


# The two source_kind values that assert human authorship. Restricted to genuine
# human sessions by validate_source_kind_authority — anything else claiming one
# of these is the kind of input the ranking cannot reason about without
# impersonation. See plan.md Workstream 1.
HUMAN_SOURCE_KINDS = frozenset({"operator_authored", "human_direct"})


def validate_source_kind_authority(
    source_kind: str, authority: EffectiveAuthority
) -> Optional[tuple[str, str]]:
    """Gate caller-supplied source_kind on something the system can actually check.

    Returns ``(code, message)`` on a denial, ``None`` when the value is allowed
    for this caller. Anything claiming human provenance must come from an
    authenticated human session (`actor_type == "user"`); `external_import` only
    labels records that came in through the retired import paths, so it is
    never allowed through this validator.

    Other tiers (`agent_inference`, `tool_output`, `episodic_inference`,
    `semantic_inference`) are agent-self-reported and have no restriction here
    — they are still caller-settable because that is what they mean, and
    _provenance_penalty in the ranker is what stops them from claiming trust.
    """
    if source_kind in HUMAN_SOURCE_KINDS and authority.actor_type != "user":
        return (
            "SOURCE_KIND_DENIED",
            "This source_kind requires an authenticated human session",
        )
    if source_kind == "external_import":
        return (
            "SOURCE_KIND_DENIED",
            "external_import labels records from the retired import paths and cannot be written",
        )
    return None


def validate_search_query(query: str) -> str:
    text = query.strip()
    if len(text) <= 2:
        raise MemoryOperationError(
            "QUERY_TOO_SHORT", "Query must be at least 3 characters", 400
        )
    if re.match(r"^(the|a|an|is|are|was|were|i|you|he|she|it|we|they)\s*$", text, re.I):
        raise MemoryOperationError("QUERY_NOISE", "Query is too trivial", 400)
    if re.match(r"^[.,;:!?]+$", text) or contains_pii(text):
        raise MemoryOperationError(
            "QUERY_NOISE",
            "Query is too trivial or contains a credential-like pattern",
            400,
        )
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
        raise MemoryOperationError(
            "INVALID_CLASS", f"memory_class must be one of {MEMORY_CLASSES}", 400
        )
    if source_kind not in SOURCE_KINDS:
        raise MemoryOperationError(
            "INVALID_SOURCE_KIND", f"source_kind must be one of {SOURCE_KINDS}", 400
        )
    # Restrict caller-supplied source_kind by actor_type — the ranking cannot
    # reason about human provenance unless the system can check who claimed it.
    # See validate_source_kind_authority and plan.md Workstream 1.
    denied = validate_source_kind_authority(source_kind, authority)
    if denied is not None:
        raise MemoryOperationError(denied[0], denied[1], 403)
    # last_confirmed_at is fully server-owned. No caller — human or agent —
    # may set it through the ordinary write path; the only legitimate setter is
    # confirm_memory, with evidence. Writing a record is asserting it, not
    # checking it. This is a structural rule (400), not an authority gap (403):
    # no caller identity makes the value acceptable.
    if values.get("last_confirmed_at"):
        raise MemoryOperationError(
            "LAST_CONFIRMED_AT_READ_ONLY",
            "last_confirmed_at cannot be set directly; call memory_confirm with evidence after writing",
            400,
        )
    if not 0.0 <= confidence <= 1.0:
        raise MemoryOperationError(
            "INVALID_CONFIDENCE", "confidence must be between 0.0 and 1.0", 400
        )
    if not 0.0 <= importance <= 1.0:
        raise MemoryOperationError(
            "INVALID_IMPORTANCE", "importance must be between 0.0 and 1.0", 400
        )

    supersedes_id = values.get("supersedes_id")
    if supersedes_id:
        old = memory_service.get_memory_record(supersedes_id)
        if not old:
            raise MemoryOperationError(
                "NOT_FOUND", "Record to supersede not found", 404
            )
        if old["record_status"] != "active":
            raise MemoryOperationError(
                "INVALID_SUPERSESSION", "Cannot supersede non-active record", 400
            )
        if not authority.can("memory", "write", scope=old["scope"]):
            raise MemoryOperationError(
                "SCOPE_DENIED", "Access denied to scope of record being superseded", 403
            )

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
            raise MemoryOperationError(
                "EXECUTION_OWNERSHIP", "Execution belongs to another agent", 403
            ) from exc
        except ValueError as exc:
            raise MemoryOperationError(str(exc), "Invalid execution", 400) from exc

    provenance = memory_service.provenance_for_write(
        actor_type=authority.actor_type,
        actor_id=authority.actor_id,
        channel=channel,
        route=route,
        source_kind=source_kind,
        scope=scope,
        user_id=authority.user_id,
        agent_id=authority.agent_id,
        extras=authority.safe_attribution(),
    )
    try:
        record, pii_flag = await asyncio.to_thread(
            memory_service.write_memory,
            content=values["content"],
            memory_class=memory_class,
            scope=scope,
            topic=values.get("topic"),
            confidence=confidence,
            importance=importance,
            source_kind=source_kind,
            supersedes_id=supersedes_id,
            provenance_json=provenance,
            subject_anchor=values.get("subject_anchor"),
            slot_key=values.get("slot_key"),
            valid_from=values.get("valid_from"),
            valid_to=values.get("valid_to"),
            expires_at=values.get("expires_at"),
            source_execution_id=execution_id,
        )
    except ValueError as exc:
        raise MemoryOperationError("INVALID_INPUT", str(exc), 400) from exc
    if pii_flag == "PII_DETECTED":
        raise MemoryOperationError(
            "PII_DETECTED",
            "Content contains PII and cannot be written to shared scope",
            422,
        )

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
        actor_type=authority.actor_type,
        actor_id=authority.actor_id,
        action="memory_write",
        resource_type="memory_record",
        resource_id=record["id"],
        result="success",
        details=audit_details,
    )
    payload = {"record": record}
    warnings = await asyncio.to_thread(
        memory_service.assess_memory_write,
        content=values["content"],
        scope=scope,
        memory_class=memory_class,
        topic=values.get("topic"),
        exclude_id=record["id"],
        subject_anchor=values.get("subject_anchor"),
    )
    if warnings:
        payload["warnings"] = warnings
    return payload


async def confirm_memory(
    record_id: str,
    evidence: str,
    authority: EffectiveAuthority,
    *,
    channel: str,
    route: str,
) -> dict[str, Any]:
    """Confirm one record with evidence, callable from any transport.

    Shared operation backing both the MCP `memory_confirm` tool and the REST
    `POST /api/memory/confirm` route. Authorization, the evidence-required
    check, the call into `memory_service.confirm_memory`, and the audit event
    all live here so the two transports cannot drift apart.

    A blank/missing evidence text is rejected with EVIDENCE_REQUIRED — the same
    rule `memory_service.confirm_memory` enforces at the service level, raised
    here so the transport returns a structured error rather than an unhandled
    exception.
    """
    detail = (evidence or "").strip()
    if not detail:
        raise MemoryOperationError(
            "EVIDENCE_REQUIRED",
            "evidence is required: say what you checked, e.g. "
            "'adapter.json reports version 1.0.1'",
            400,
        )

    record = memory_service.get_memory_record(record_id)
    if not record:
        raise MemoryOperationError("NOT_FOUND", "Record not found", 404)
    if not authority.can("memory", "write", scope=record["scope"]):
        raise MemoryOperationError("SCOPE_DENIED", "Access denied to this scope", 403)

    # Attribute the confirmation to whoever actually did the checking, not
    # to the human who happens to own the agent. build_agent_context
    # populates RequestContext.user_id from the agent's default_user_id/
    # owner_user_id for every agent call, so a `user_id or agent_id`
    # priority would silently tag every agent confirmation with the
    # human owner's name. The actor_type is the only signal that
    # distinguishes a human session from an agent session.
    if authority.actor_type == "user":
        verified_by = authority.user_id or authority.actor_id or "unknown"
    else:
        verified_by = authority.agent_id or authority.actor_id or "unknown"
    try:
        confirmed = memory_service.confirm_memory(
            record_id, evidence=detail, verified_by=verified_by
        )
    except ValueError as exc:
        raise MemoryOperationError("EVIDENCE_REQUIRED", str(exc), 400) from exc
    if not confirmed:
        raise MemoryOperationError(
            "NOT_ACTIVE", "Only an active record can be confirmed", 400
        )

    audit_service.write_event(
        actor_type=authority.actor_type,
        actor_id=authority.actor_id,
        action="memory_confirmed",
        resource_type="memory_record",
        resource_id=record_id,
        result="success",
        details={
            "scope": record["scope"],
            "evidence": detail,
            "channel": channel,
            "route": route,
        },
    )
    return {"record": memory_service.lean_record(confirmed)}
