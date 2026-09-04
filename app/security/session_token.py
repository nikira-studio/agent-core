from fastapi import Request


def get_session_token(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        return authorization[7:]
    return request.cookies.get("session_token") or ""
