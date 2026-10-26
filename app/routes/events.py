import asyncio
import json
import queue as queue_module
import time

from fastapi import APIRouter, Depends, Request
from starlette.responses import StreamingResponse

from app.security.dependencies import get_current_session
from app.security.context import build_user_context
from app.security.scope_enforcer import ScopeEnforcer
from app.services import auth_service
from app.services.event_stream_service import event_hub
from app.time_utils import parse_utc_datetime, utc_now

router = APIRouter(prefix="/api/events", tags=["events"])

_HEARTBEAT_INTERVAL = 15.0
_POLL_INTERVAL = 0.1


def _sse_event(event_type: str, data: str) -> str:
    return f"event: {event_type}\ndata: {data}\n\n"


def _sse_comment(comment: str) -> str:
    return f": {comment}\n\n"


def _event_visible(payload: dict, session: dict) -> bool:
    data = payload["data"]
    # Match the dashboard activity list's owner filter, including agent-private
    # activities belonging to this user. Shared workspace access alone does not
    # grant visibility into another user's activity stream.
    if payload["type"].startswith("activity_"):
        return (
            session.get("role") == "admin" or data.get("user_id") == session["user_id"]
        )
    ctx = build_user_context(session)
    if ctx.is_admin:
        return True
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if payload["type"] == "connector_executed":
        scope = data.get("scope")
        return bool(scope and enforcer.can_read(scope))
    return False


@router.get("")
async def stream_events(
    request: Request,
    session: dict = Depends(get_current_session),
):
    client_id, q = event_hub.register()

    async def generator():
        last_heartbeat = time.monotonic()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = q.get_nowait()
                    # Permissions and roles may change while a stream is open.
                    current_session = auth_service.get_session(
                        session.get("id") or session["db_session_id"]
                    )
                    if not current_session or utc_now() > parse_utc_datetime(
                        current_session["expires_at"]
                    ):
                        break
                    if _event_visible(payload, current_session):
                        yield _sse_event(payload["type"], json.dumps(payload))
                except queue_module.Empty:
                    now = time.monotonic()
                    if now - last_heartbeat >= _HEARTBEAT_INTERVAL:
                        yield _sse_comment("heartbeat")
                        last_heartbeat = now
                    await asyncio.sleep(_POLL_INTERVAL)
        finally:
            event_hub.unregister(client_id)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
