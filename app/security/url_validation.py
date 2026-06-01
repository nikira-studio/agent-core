import ipaddress
import socket
import urllib.parse


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


def validate_public_url(url: str) -> None:
    from app.config import settings  # lazy to avoid circular import

    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("URL must include a host")
    if host.lower() in settings.allowed_internal_host_set:
        return
    block_internal = bool(getattr(settings, "BLOCK_INTERNAL_HOSTS", False))

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if block_internal and _is_blocked_ip(ip):
            raise ValueError(f"Blocked private network host: {host}")
        return

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"Failed to resolve host: {host}") from e

    resolved = set()
    blocked_resolved = []
    for family, _, _, _, sockaddr in infos:
        if not sockaddr:
            continue
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            blocked_resolved.append(addr)
            if block_internal:
                raise ValueError(f"Blocked private network host: {host} -> {addr}")
        resolved.add(addr)

    if not resolved:
        raise ValueError(f"Failed to validate host: {host}")

    if block_internal and blocked_resolved:
        raise ValueError(f"Blocked private network host: {host} -> {blocked_resolved[0]}")
