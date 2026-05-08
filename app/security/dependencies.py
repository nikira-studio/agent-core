from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.services.auth_service import validate_session, get_session
from app.security.context import (
    RequestContext,
    build_user_context,
    build_user_context_for_connectors,
)
from app.security.exceptions import APIError
from app.config import settings


security = HTTPBearer(auto_error=False)


async def get_current_session(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    session_token = None
    if credentials:
        session_token = credentials.credentials
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


async def get_current_user(
    session: dict = Depends(get_current_session),
) -> dict:
    return session


async def get_current_user_optional(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict | None:
    session_token = None
    if credentials:
        session_token = credentials.credentials
    elif request.cookies.get("session_token"):
        session_token = request.cookies.get("session_token")

    if not session_token:
        return None

    session = validate_session(
        session_token,
        inactivity_minutes=settings.INACTIVITY_TIMEOUT_MINUTES,
    )

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
) -> RequestContext:
    if not credentials:
        session = await get_current_session(request, credentials)
        return build_user_context(session)

    token = credentials.credentials
    if token.startswith("ac_sk_"):
        from app.services.agent_service import get_agent_by_api_key

        agent = get_agent_by_api_key(token)
        if not agent or not agent["is_active"]:
            raise APIError("INVALID_KEY", "Invalid or inactive agent API key", 401)
        from app.security.scope_enforcer import build_agent_context

        return build_agent_context(agent)
    else:
        session = await get_current_session(request, credentials)
        return build_user_context(session)


async def get_mcp_request_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> RequestContext:
    if not credentials:
        session = await get_current_session(request, credentials)
        return build_user_context_for_connectors(session)

    token = credentials.credentials
    if token.startswith("ac_sk_"):
        from app.services.agent_service import get_agent_by_api_key

        agent = get_agent_by_api_key(token)
        if not agent or not agent["is_active"]:
            raise APIError("INVALID_KEY", "Invalid or inactive agent API key", 401)
        from app.security.scope_enforcer import build_agent_context

        return build_agent_context(agent)
    else:
        session = await get_current_session(request, credentials)
        return build_user_context_for_connectors(session)
