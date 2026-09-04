"""ASGI request-body size enforcement.

The HTTP boundary cannot trust Content-Length: HTTP/1.1 chunked bodies and
HTTP/2 streams commonly omit it. This middleware counts the bytes actually
received before any JSON parser or route handler can allocate from the body.
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any


ASGIApp = Callable[[dict[str, Any], Callable, Callable], Awaitable[None]]


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        declared = headers.get(b"content-length")
        declared_size = None
        if declared is not None:
            try:
                declared_size = int(declared)
                if declared_size < 0:
                    raise ValueError
            except ValueError:
                await self._reject(
                    send,
                    400,
                    "INVALID_CONTENT_LENGTH",
                    "Malformed Content-Length header",
                )
                return
            if declared_size > self.max_bytes:
                await self._reject(
                    send, 413, "PAYLOAD_TOO_LARGE", "Request body too large"
                )
                return

        # Do not replace the receive channel for ordinary bodyless requests.
        # Long-lived GET responses such as SSE need the server's original
        # disconnect signal, and there is no request payload to limit here.
        if (
            scope.get("method") in {"GET", "HEAD", "OPTIONS"}
            and declared_size in {None, 0}
            and b"transfer-encoding" not in headers
        ):
            await self.app(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":

                async def disconnected() -> dict:
                    return {"type": "http.disconnect"}

                await self.app(scope, disconnected, send)
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                await self._reject(
                    send, 413, "PAYLOAD_TOO_LARGE", "Request body too large"
                )
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay() -> dict:
            nonlocal delivered
            if delivered:
                # The downstream application may keep listening for the real
                # client disconnect after it has consumed the request body.
                # StreamingResponse does this while it sends CSV, backup, and
                # SSE data.  Inventing a disconnect here races with and can
                # cancel that response before all chunks are sent.
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(send: Callable, status: int, code: str, message: str) -> None:
        payload = json.dumps(
            {"ok": False, "error": {"code": code, "message": message}},
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
