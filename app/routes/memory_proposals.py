"""Operator review queue for memory consolidation proposals.

Admin-only and human-driven by design: these endpoints suggest and record
decisions about what to retract, and that judgement stays with the operator.
Agents write memory; they do not get to prune each other's.
"""

import asyncio
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from typing import Optional

from app.security.dependencies import require_admin
from app.security.response_helpers import success_response, error_response
from app.services import audit_service, memory_proposal_service

router = APIRouter(prefix="/api/memory/proposals", tags=["memory"])


class GenerateProposalsRequest(BaseModel):
    scope: Optional[str] = None
    rules: Optional[list[str]] = None


class ReanchorProposalRequest(BaseModel):
    subject_anchor: str


class DecideProposalRequest(BaseModel):
    verdict: str
    # Confirm proposals only: what the reviewer found. "still_current" refreshes
    # the record, "no_longer_current" retracts it. Both are accepts — the rule
    # was right to ask either way.
    outcome: Optional[str] = None


def _client_ip(request: Request) -> Optional[str]:
    from app.routes.auth import get_client_ip

    return get_client_ip(request)


@router.get("")
async def list_proposals(
    status: str = "pending",
    scope: Optional[str] = None,
    rule: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    session: dict = Depends(require_admin),
):
    proposals = memory_proposal_service.list_proposals(
        status=status,
        scope=scope,
        rule=rule,
        limit=min(max(limit, 0), 200),
        offset=max(offset, 0),
    )
    return success_response({"proposals": proposals, "total": len(proposals)})


class ReviewUsefulnessRequest(BaseModel):
    scope: Optional[str] = None
    limit: Optional[int] = None


@router.post("/review-usefulness")
async def review_usefulness(
    body: ReviewUsefulnessRequest,
    request: Request,
    session: dict = Depends(require_admin),
):
    """Ask the configured model which records a future session could not act on.

    Off unless an operator has configured a reviewer binding: this sends record
    content to whatever model that binding points at, which on a local-first
    system is the operator's call to make, not a default.
    """
    from app.services import usefulness_service

    result = await asyncio.to_thread(
        usefulness_service.review_scope, scope=body.scope, limit=body.limit
    )
    if not result["ready"]:
        return error_response("REVIEWER_NOT_CONFIGURED", result["reason"], 400)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="memory_usefulness_reviewed",
        resource_type="memory_proposal",
        result="success",
        details={k: v for k, v in result.items() if k != "ready"},
        ip_address=_client_ip(request),
    )
    return success_response(result)


@router.get("/stats")
async def proposal_stats(session: dict = Depends(require_admin)):
    """Per-rule verdict history: how often each rule has been right so far."""
    return success_response({"rules": memory_proposal_service.rule_stats()})


@router.post("/generate")
async def generate_proposals(
    body: GenerateProposalsRequest,
    request: Request,
    session: dict = Depends(require_admin),
):
    unknown = set(body.rules or []) - set(memory_proposal_service.RULES)
    if unknown:
        return error_response(
            "UNKNOWN_RULE",
            f"Unknown proposal rule(s): {', '.join(sorted(unknown))}",
            400,
        )

    result = await asyncio.to_thread(
        memory_proposal_service.generate_proposals,
        scope=body.scope, rules=body.rules
    )

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="memory_proposals_generated",
        resource_type="memory_proposal",
        result="success",
        details={
            "created": result["created"],
            "skipped_already_known": result["skipped_already_known"],
            "scope": body.scope,
            "rules": body.rules,
        },
        ip_address=_client_ip(request),
    )
    return success_response(result)


@router.post("/{proposal_id}/reanchor")
async def reanchor_proposal(
    proposal_id: str,
    body: ReanchorProposalRequest,
    request: Request,
    session: dict = Depends(require_admin),
):
    try:
        proposal = memory_proposal_service.reanchor_proposal(
            proposal_id, body.subject_anchor, decided_by=session["user_id"]
        )
    except LookupError:
        return error_response("NOT_FOUND", "Proposal not found", 404)
    except ValueError as e:
        return error_response("INVALID_ANCHOR", str(e), 400)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="memory_proposal_decided",
        resource_type="memory_proposal",
        resource_id=proposal_id,
        result="success",
        details={
            "rule": proposal["rule"],
            "verdict": "accepted",
            "outcome": "anchor_fixed",
            "subject_anchor": body.subject_anchor,
            "records_affected": proposal["applied_count"],
            "target_ids": proposal["target_ids"],
        },
        ip_address=_client_ip(request),
    )
    return success_response({"proposal": proposal})


@router.post("/{proposal_id}/decide")
async def decide_proposal(
    proposal_id: str,
    body: DecideProposalRequest,
    request: Request,
    session: dict = Depends(require_admin),
):
    try:
        proposal = memory_proposal_service.decide_proposal(
            proposal_id,
            body.verdict,
            decided_by=session["user_id"],
            outcome=body.outcome,
        )
    except LookupError:
        return error_response("NOT_FOUND", "Proposal not found", 404)
    except ValueError as e:
        return error_response("INVALID_VERDICT", str(e), 400)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="memory_proposal_decided",
        resource_type="memory_proposal",
        resource_id=proposal_id,
        result="success",
        details={
            "rule": proposal["rule"],
            "action": proposal["action"],
            "verdict": body.verdict,
            "outcome": body.outcome,
            "records_affected": proposal["applied_count"],
            "target_ids": proposal["target_ids"],
        },
        ip_address=_client_ip(request),
    )
    return success_response({"proposal": proposal})
