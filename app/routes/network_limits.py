"""Network limits: who may call the local credential helper, and whether private addresses may be reached.

Read by any admin; changed only by a person in the dashboard. A key, an operator
token or a bearer session is refused, so a script cannot loosen or tighten these.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from app.routes.auth import get_client_ip
from app.security.dependencies import get_request_context
from app.security.effective_authority import EffectiveAuthority
from app.security.exceptions import APIError
from app.security.response_helpers import error_response, success_response
from app.services import network_policy_service

router = APIRouter(prefix="/api/network-limits", tags=["network_limits"])


class NetworkLimitsChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    internal_mode: Optional[str] = None
    allowed_internal: Optional[list[str]] = None
    broker_mode: Optional[str] = None
    broker_allowed: Optional[list[str]] = None


def _admin(ctx: EffectiveAuthority) -> None:
    if not ctx.is_admin or ctx.actor_type != "user":
        raise APIError("FORBIDDEN", "Admin access required", 403)


@router.get("")
def get_limits(ctx: EffectiveAuthority = Depends(get_request_context)):
    _admin(ctx)
    return success_response({"network_limits": network_policy_service.status()})


@router.put("")
def change_limits(
    body: NetworkLimitsChange,
    request: Request,
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    if request.headers.get("Authorization"):
        raise APIError("COOKIE_SESSION_REQUIRED", "Network limits are changed by a person in the dashboard", 403)
    _admin(ctx)
    try:
        change = network_policy_service.update(body.model_dump(exclude_none=True), changed_by=ctx.actor_id)
    except ValueError as exc:
        return error_response("INVALID_INPUT", str(exc), 400)
    return success_response({"network_limits": change["after"], "client_ip": get_client_ip(request)})
