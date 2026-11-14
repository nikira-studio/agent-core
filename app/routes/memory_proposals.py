"""The Memory Clean-up queue was removed in 1.3.0; old clients get a clear answer instead of a 404."""

from fastapi import APIRouter, Request

from app.security.response_helpers import error_response

router = APIRouter(prefix="/api/memory/proposals", tags=["memory"])

_MESSAGE = (
    "The Memory Clean-up queue was removed in Agent Core 1.3.0. Per-occurrence logs, ticket-closeout "
    "records, duplicates and facts whose anchor is gone now retract themselves (restore one with "
    "POST /api/memory/restore); nothing is left for a person to review."
)


@router.api_route("", methods=["GET", "POST", "PUT", "DELETE"])
@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
def removed(request: Request, path: str = ""):
    return error_response("REMOVED", _MESSAGE, 410)
