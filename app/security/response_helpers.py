from typing import Any

from fastapi.responses import JSONResponse


def success_response(data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content={"ok": True, "data": data}, status_code=status_code)


def success_response_with_headers(data: Any, headers: dict, status_code: int = 200) -> JSONResponse:
    resp = JSONResponse(content={"ok": True, "data": data}, status_code=status_code)
    for k, v in headers.items():
        resp.headers[k] = v
    return resp


def error_response(code: str, message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        content={"ok": False, "error": {"code": code, "message": message}},
        status_code=status_code,
    )


def rate_limit_headers(limit: int, remaining: int, reset: int) -> dict:
    return {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(reset),
    }


def rate_limited_response(code: str, message: str, limit: int, remaining: int, reset: int) -> JSONResponse:
    resp = error_response(code, message, 429)
    resp.headers["X-RateLimit-Limit"] = str(limit)
    resp.headers["X-RateLimit-Remaining"] = str(remaining)
    resp.headers["X-RateLimit-Reset"] = str(reset)
    return resp


def SCOPE_DENIED(msg="Agent does not have access to this scope."):
    return error_response("SCOPE_DENIED", msg, 403)


def NOT_FOUND(msg="Resource not found."):
    return error_response("NOT_FOUND", msg, 404)


def BAD_REQUEST(msg="Bad request."):
    return error_response("BAD_REQUEST", msg, 400)


def UNAUTHORIZED(msg="Authentication required."):
    return error_response("UNAUTHORIZED", msg, 401)


def FORBIDDEN(msg="Action not permitted."):
    return error_response("FORBIDDEN", msg, 403)


def INTERNAL_ERROR(msg="Internal server error."):
    return error_response("INTERNAL_ERROR", msg, 500)