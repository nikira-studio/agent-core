from fastapi import Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.services.auth_service import (
    OPERATOR_TOKEN_PREFIX,
    validate_operator_token,
    validate_session,
)
from app.security.context import (
    build_user_context,
    build_user_context_for_connectors,
)
from app.security.effective_authority import EffectiveAuthority, permanent_authority
from app.security.exceptions import APIError
from app.config import settings


security = HTTPBearer(auto_error=False)

# Apps that use this server as their sign-in send the login session token as a
# Bearer key, but only to these routes (identity, effective authority, logout).
BEARER_SESSION_PATH_PREFIX = "/api/auth/"


def bearer_session_allowed(path: str) -> bool:
    return path.startswith(BEARER_SESSION_PATH_PREFIX)


async def get_current_session(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    session_token = None
    if credentials:
        bearer = credentials.credentials
        if bearer.startswith(OPERATOR_TOKEN_PREFIX):
            session = validate_operator_token(bearer)
            if not session:
                raise APIError("SESSION_INVALID", "Operator token expired, revoked or invalid", 401)
            return session
        if not bearer_session_allowed(request.url.path):
            raise APIError(
                "SESSION_BEARER_NOT_ALLOWED",
                "A login session token is accepted as a Bearer key only on /api/auth/*. "
                "Scripts that act as a person should use an operator token "
                "(POST /api/auth/operator-tokens while signed in); agents use their agent key.",
                401,
            )
        session_token = bearer
    elif request.cookies.get("session_token"):
        session_token = request.cookies.get("session_token")

    if not session_token:
        raise APIError("UNAUTHORIZED", "Authentication required", 401)

    session = validate_session(
        session_token,
        inactivity_minutes=settings.INACTIVITY_TIMEOUT_MINUTES,
    )

    if not session:
        raise APIError("SESSION_INVALID", "Session expired or invalid", 401)

    return session


async def require_admin(
    session: dict = Depends(get_current_session),
) -> dict:
    if session.get("role") != "admin":
        raise APIError("FORBIDDEN", "Admin access required", 403)
    return session


async def get_current_agent(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    if not credentials:
        raise APIError("UNAUTHORIZED", "Authentication required", 401)

    token = credentials.credentials
    if not token.startswith("ac_sk_"):
        raise APIError("INVALID_KEY", "Invalid API key format", 401)

    from app.services.agent_service import get_agent_by_api_key

    agent = get_agent_by_api_key(token)

    if not agent or not agent["is_active"]:
        raise APIError("INVALID_KEY", "Invalid or inactive agent API key", 401)

    return agent


async def get_request_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> EffectiveAuthority:
    if not credentials:
        session = await get_current_session(request, credentials)
        return permanent_authority(build_user_context(session))

    token = credentials.credentials
    if token.startswith("ac_sk_"):
        from app.services.agent_service import get_agent_by_api_key

        agent = get_agent_by_api_key(token)
        if not agent or not agent["is_active"]:
            raise APIError("INVALID_KEY", "Invalid or inactive agent API key", 401)
        from app.security.scope_enforcer import build_agent_context

        return permanent_authority(build_agent_context(agent))
    else:
        session = await get_current_session(request, credentials)
        return permanent_authority(build_user_context(session))


async def get_mcp_request_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> EffectiveAuthority:
    if not credentials:
        session = await get_current_session(request, credentials)
        return permanent_authority(build_user_context_for_connectors(session))

    token = credentials.credentials
    if token.startswith("ac_sk_"):
        from app.services.agent_service import get_agent_by_api_key

        agent = get_agent_by_api_key(token)
        if not agent or not agent["is_active"]:
            raise APIError("INVALID_KEY", "Invalid or inactive agent API key", 401)
        from app.security.scope_enforcer import build_agent_context

        return permanent_authority(build_agent_context(agent))
    session = await get_current_session(request, credentials)
    return permanent_authority(build_user_context_for_connectors(session))


def require_capability(capability: str):
    """FastAPI dependency for REST route families with one capability owner."""
    async def dependency(
        authority: EffectiveAuthority = Depends(get_request_context),
    ) -> EffectiveAuthority:
        if not authority.has_capability(capability):
            raise APIError(
                "CAPABILITY_DENIED",
                f"This operation requires the {capability} capability",
                403,
            )
        return authority
    return dependency

