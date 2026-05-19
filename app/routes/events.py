import asyncio
import json
import queue as queue_module
import time

from fastapi import APIRouter, Depends, Request
from starlette.responses import StreamingResponse

from app.security.dependencies import get_current_session
from app.services.event_stream_service import event_hub

router = APIRouter(prefix="/api/events", tags=["events"])

_HEARTBEAT_INTERVAL = 15.0
_POLL_INTERVAL = 0.1


def _sse_event(event_type: str, data: str) -> str:
    return f"event: {event_type}\ndata: {data}\n\n"


def _sse_comment(comment: str) -> str:
    return f": {comment}\n\n"


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
