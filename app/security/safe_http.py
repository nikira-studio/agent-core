"""Outbound HTTP that re-validates every redirect hop against the SSRF guard.

validate_public_url only inspects the URL it is handed, but urllib follows 30x
redirects by default — so a public URL can bounce to an internal host or the cloud
metadata endpoint. safe_urlopen revalidates the initial URL and every redirect target.
"""
import urllib.request

from app.security.url_validation import validate_public_url

_MAX_REDIRECTS = 5


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_repeats = _MAX_REDIRECTS
    max_redirections = _MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_ValidatingRedirectHandler)


def safe_urlopen(req, timeout: float = 30):
    """Drop-in replacement for urllib.request.urlopen that enforces validate_public_url
    on the initial URL and on every redirect target. Raises ValueError if a hop is
    blocked."""
    url = req.full_url if isinstance(req, urllib.request.Request) else req
    validate_public_url(url)
    return _opener.open(req, timeout=timeout)
