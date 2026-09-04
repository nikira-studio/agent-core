import ipaddress
import socket
import urllib.parse
from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedUrl:
    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[tuple[int, tuple], ...]


def _is_blocked_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return bool(
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
    )


def resolve_public_url(url: str) -> ResolvedUrl:
    """Validate a URL and return the exact addresses approved for connection."""
    from app.config import settings  # lazy to avoid circular import

    normalized = url.strip()
    parsed = urllib.parse.urlparse(normalized)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Credentials are not allowed in outbound URLs")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("URL contains an invalid port") from exc

    allow_internal = host.lower() in settings.allowed_internal_host_set
    block_internal = bool(getattr(settings, "BLOCK_INTERNAL_HOSTS", False))

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if block_internal and not allow_internal and _is_blocked_ip(ip):
            raise ValueError(f"Blocked private network host: {host}")
        family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        sockaddr = (
            (str(ip), port, 0, 0) if family == socket.AF_INET6 else (str(ip), port)
        )
        return ResolvedUrl(normalized, parsed.scheme, host, port, ((family, sockaddr),))

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"Failed to resolve host: {host}") from e

    resolved: list[tuple[int, tuple]] = []
    seen: set[str] = set()
    for family, _, _, _, sockaddr in infos:
        if not sockaddr:
            continue
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if block_internal and not allow_internal and _is_blocked_ip(ip):
            raise ValueError(f"Blocked private network host: {host} -> {addr}")
        if addr not in seen:
            seen.add(addr)
            resolved.append((family, sockaddr))

    if not resolved:
        raise ValueError(f"Failed to validate host: {host}")

    return ResolvedUrl(normalized, parsed.scheme, host, port, tuple(resolved))


def validate_public_url(url: str) -> None:
    resolve_public_url(url)
