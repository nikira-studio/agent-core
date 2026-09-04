"""Outbound HTTP helpers with redirect and DNS-rebinding protection."""

import http.client
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import httpx

from app.security.url_validation import ResolvedUrl, resolve_public_url

_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _connect_address(
    family: int,
    sockaddr: tuple,
    timeout: float | None,
    source_address=None,
):
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
            sock.settimeout(timeout)
        if source_address:
            sock.bind(source_address)
        sock.connect(sockaddr)
        return sock
    except Exception:
        sock.close()
        raise


def _connection(target: ResolvedUrl, family: int, sockaddr: tuple, timeout: float):
    if target.scheme == "https":
        conn = http.client.HTTPSConnection(
            target.host,
            target.port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    else:
        conn = http.client.HTTPConnection(target.host, target.port, timeout=timeout)
    conn._create_connection = (  # type: ignore[method-assign]
        lambda _address, connect_timeout=timeout, source_address=None: _connect_address(
            family, sockaddr, connect_timeout, source_address
        )
    )
    return conn


def _request_parts(req: urllib.request.Request | str):
    if isinstance(req, urllib.request.Request):
        return req.full_url, req.get_method(), req.data, dict(req.header_items())
    return str(req), "GET", None, {}


def safe_urlopen(req: urllib.request.Request | str, timeout: float = 30):
    """Open an HTTP(S) URL using the exact IP address that passed validation."""
    url, method, data, headers = _request_parts(req)
    for redirect_count in range(_MAX_REDIRECTS + 1):
        target = resolve_public_url(url)
        parsed = urllib.parse.urlsplit(url)
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        last_error: Exception | None = None
        response = None
        for family, sockaddr in target.addresses:
            conn = _connection(target, family, sockaddr, timeout)
            try:
                conn.request(method, path, body=data, headers=headers)
                response = conn.getresponse()
                break
            except OSError as exc:
                last_error = exc
                conn.close()
        if response is None:
            raise urllib.error.URLError(last_error or "Could not connect")

        if response.status in _REDIRECT_STATUSES:
            location = response.headers.get("Location")
            if location and redirect_count < _MAX_REDIRECTS:
                redirect_status = response.status
                response.close()
                next_url = urllib.parse.urljoin(url, location)
                current = urllib.parse.urlsplit(url)
                redirected = urllib.parse.urlsplit(next_url)
                if current.scheme == "https" and redirected.scheme != "https":
                    raise ValueError("Blocked HTTPS-to-HTTP redirect")
                if (current.scheme, current.hostname, current.port) != (
                    redirected.scheme,
                    redirected.hostname,
                    redirected.port,
                ):
                    headers = {
                        key: value
                        for key, value in headers.items()
                        if key.lower()
                        not in {"authorization", "cookie", "proxy-authorization"}
                    }
                url = next_url
                if redirect_status in {301, 302, 303} and method != "HEAD":
                    method, data = "GET", None
                    headers = {
                        key: value
                        for key, value in headers.items()
                        if key.lower() not in {"content-length", "content-type"}
                    }
                continue

        if response.status >= 400 or response.status in _REDIRECT_STATUSES:
            raise urllib.error.HTTPError(
                url, response.status, response.reason, response.headers, response
            )
        return response
    raise urllib.error.HTTPError(url, 310, "Too many redirects", {}, None)


def _pinned_url(target: ResolvedUrl, family: int, sockaddr: tuple) -> str:
    ip = sockaddr[0]
    literal = f"[{ip}]" if family == socket.AF_INET6 else ip
    default_port = 443 if target.scheme == "https" else 80
    authority = literal if target.port == default_port else f"{literal}:{target.port}"
    parsed = urllib.parse.urlsplit(target.url)
    return urllib.parse.urlunsplit(
        (target.scheme, authority, parsed.path or "/", parsed.query, "")
    )


def safe_httpx_request(
    client: httpx.Client,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    """Send with httpx while connecting only to an address validated now."""
    target = resolve_public_url(url)
    host_header = target.host
    default_port = 443 if target.scheme == "https" else 80
    if target.port != default_port:
        host_header = f"{host_header}:{target.port}"

    last_error: httpx.RequestError | None = None
    for family, sockaddr in target.addresses:
        request = client.build_request(
            method, _pinned_url(target, family, sockaddr), **kwargs
        )
        request.headers["Host"] = host_header
        request.extensions["sni_hostname"] = target.host.encode("idna")
        try:
            return client.send(request)
        except httpx.RequestError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def safe_httpx_post(client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
    return safe_httpx_request(client, "POST", url, **kwargs)
