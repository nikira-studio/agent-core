"""Safe public URL generation for routes that publish installation links."""

from fastapi import Request

from app.config import settings


def _first_header_value(request: Request, name: str) -> str:
    return (request.headers.get(name) or "").split(",", 1)[0].strip()


def _valid_host(value: str) -> bool:
    return bool(value) and "://" not in value and "/" not in value and "@" not in value and not any(char.isspace() for char in value)


def public_base_url(request: Request) -> str:
    """Return the public origin, honoring forwarded origin headers only from a trusted proxy."""
    scheme = request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    trusted = bool(request.client and request.client.host in settings.trusted_proxy_list)
    if trusted:
        forwarded_proto = _first_header_value(request, "x-forwarded-proto").lower()
        forwarded_host = _first_header_value(request, "x-forwarded-host")
        if forwarded_proto in {"http", "https"}:
            scheme = forwarded_proto
        if _valid_host(forwarded_host):
            host = forwarded_host
    return f"{scheme}://{host}".rstrip("/")
