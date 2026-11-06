"""Shared dashboard helpers.

Page rendering, auth, HTML escaping, and small formatting/pagination utilities
used across the dashboard page modules.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse

from app.services.auth_service import validate_session
from app.branding import APP_NAME, JS_WINDOW_EVENT
from app.config import settings


def _hf(s):
    return s.replace("escape_html", "escapeHtml")


def _page_number(raw) -> int:
    """Parse a ?page= query value; anything that is not a positive integer is page 1."""
    try:
        return max(1, int(raw or 1))
    except (TypeError, ValueError):
        return 1


def escape_html(s):
    """Escape a value for interpolation into dashboard HTML.

    Single quotes are escaped too: much of this dashboard uses single-quoted
    attributes (`class='...'`, `title='...'`), and a value that escapes only
    double quotes can still break out of one of those and add an event handler.
    Matches escapeHtml in dashboard.js so both sides of a page agree.
    """
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def local_dt(value, style: str = "datetime", empty: str = "—") -> str:
    """Render a stored UTC timestamp as a client-convertible element.

    All timestamps are stored in UTC; the browser converts the data-utc value to
    the viewer's selected timezone (see applyLocalTimes in dashboard.js). The
    rendered text is a UTC fallback shown until/if JS runs. style='date' shows
    date only.
    """
    if not value:
        return empty
    from app.time_utils import parse_utc_datetime

    try:
        dt = parse_utc_datetime(value)
    except (ValueError, TypeError):
        return escape_html(str(value))
    iso = dt.isoformat()
    if style == "date":
        fallback = dt.strftime("%Y-%m-%d")
        attr = ' data-dt-style="date"'
    elif style == "time":
        fallback = dt.strftime("%H:%M UTC")
        attr = ' data-dt-style="time"'
    else:
        fallback = dt.strftime("%Y-%m-%d %H:%M UTC")
        attr = ""
    return (
        f'<span class="local-dt" data-utc="{escape_html(iso)}"{attr}>'
        f"{escape_html(fallback)}</span>"
    )


def get_icon(name: str, size: int = 16, color: str = "currentColor") -> str:
    icons = {
        "delete": f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18m-2 0v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6m3 0V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2m-6 9l4 4m0-4l-4 4"/></svg>',
        "logout": f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4m7 14l5-5-5-5m5 5H9"/></svg>',
        "edit": f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7m-5-9l5 5L9 20H4v-5L15 3z"/></svg>',
        "view": f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
        "settings": f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    }
    return icons.get(name, "")


def require_auth(request: Request):
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        from app.services.auth_service import validate_operator_token

        # Dashboard routes take a Bearer operator token, never a Bearer login
        # session; see BEARER_SESSION_PATH_PREFIX.
        session = validate_operator_token(authorization[7:])
        if not session:
            raise HTTPException(status_code=302, headers={"Location": "/login"})
        return session
    token = request.cookies.get("session_token") or ""
    if not token:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    session = validate_session(
        token, inactivity_minutes=settings.INACTIVITY_TIMEOUT_MINUTES
    )
    if not session:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return session


def render_page(
    title: str,
    body: str,
    nav_active: str = "",
    extra_js: str = "",
    session: dict | None = None,
    status_code: int = 200,
    show_sidebar: bool = True,
) -> HTMLResponse:
    is_admin = bool(session and session.get("role") == "admin")
    extra_js = extra_js.replace("{{", "{").replace("}}", "}")
    # (heading, [(route, label)]). Routes keep their old paths when a page is renamed.
    nav_groups = [
        ("", [("/", "Overview")]),
        ("Work", [("/activity", "Activity"), ("/approvals", "Approvals"), ("/memory", "Memory")]),
        ("Access", [
            ("/users", "Users"),
            ("/agents", "Agents"),
            ("/workspaces", "Workspaces"),
            ("/integrations", "Connect"),
        ]),
        ("Capabilities", [("/connectors", "Services"), ("/credentials", "Credentials")]),
        ("System", [("/audit", "Audit"), ("/webhooks", "Webhooks"), ("/settings", "Settings")]),
    ]
    admin_only = {"/users", "/audit", "/webhooks"}

    def nav_link(href, label):
        if nav_active == href:
            return f'<a href="{href}" class="active" aria-current="page"><span>{label}</span></a>'
        return f'<a href="{href}" class=""><span>{label}</span></a>'

    nav_parts = []
    for heading, items in nav_groups:
        links = [nav_link(href, label) for href, label in items if is_admin or href not in admin_only]
        if not links:
            continue
        if heading:
            nav_parts.append(f'<div class="nav-heading">{heading}</div>')
        nav_parts.extend(links)
    nav_html = "\n".join(nav_parts)

    sidebar_html = ""
    if show_sidebar:
        sidebar_html = f"""
  <div class="sidebar">
    <a href="/" class="brand-link" aria-label="{APP_NAME} overview">
      <img src="/static/img/logo.png" alt="" class="brand-logo">
      <span>{APP_NAME}</span>
    </a>
    <nav>
      {nav_html}
    </nav>
    <div class="sidebar-footer">
      <button class="theme-toggle" aria-label="Toggle theme"></button>
      <a href="/logout" class="logout-link">{get_icon('logout')} <span>Logout</span></a>
    </div>
  </div>"""

    layout_class = "layout" if show_sidebar else "layout no-sidebar"
    user_tz_js = json.dumps((session or {}).get("timezone") or "")
    events_js = (
        '<script src="/static/js/events.js?v=20260816a"></script>' if session else ""
    )
    # Every state-changing request this page makes with the session cookie must
    # carry the CSRF token; wrapping fetch once covers every page's scripts.
    csrf_js = ""
    if session and session.get("channel") != "operator" and session.get("id"):
        from app.services.auth_service import CSRF_HEADER, csrf_token_for_session_id

        csrf_js = (
            "<script>(function(){var t=" + json.dumps(csrf_token_for_session_id(session["id"]))
            + ",h=" + json.dumps(CSRF_HEADER) + ",f=window.fetch;"
            "window.AC_CSRF_TOKEN=t;"
            "window.fetch=function(input,init){init=init||{};"
            "var m=(init.method||(input&&input.method)||'GET').toUpperCase();"
            "var u=typeof input==='string'?input:(input&&input.url)||'';"
            "var same=u.charAt(0)==='/'||u.indexOf(location.origin)===0;"
            "if(same&&m!=='GET'&&m!=='HEAD'&&m!=='OPTIONS'){var hd=new Headers(init.headers||(input&&input.headers)||{});"
            "if(!hd.has(h))hd.set(h,t);init.headers=hd;}return f.call(this,input,init);};})();</script>"
        )

    response = HTMLResponse(
        f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} - {APP_NAME}</title>
    <script>
    (function() {{
      try {{
        var t = localStorage.getItem('agent_core_theme');
        if (t !== 'dark' && t !== 'light') {{
          t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) ? 'light' : 'dark';
        }}
        document.documentElement.setAttribute('data-theme', t);
      }} catch (e) {{}}
    }})();
    </script>
    <link rel="icon" href="/static/img/favicon/favicon.ico" sizes="any">
    <link rel="icon" type="image/png" sizes="32x32" href="/static/img/favicon/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="16x16" href="/static/img/favicon/favicon-16x16.png">
    <link rel="apple-touch-icon" sizes="180x180" href="/static/img/favicon/apple-touch-icon.png">
    <link rel="manifest" href="/static/img/favicon/site.webmanifest">
    <link rel="stylesheet" href="/static/css/dashboard.css?v=20260828">
    {csrf_js}
</head>
<body>
<div class="{layout_class}">
  {sidebar_html}
  <div class="main">
    {body}
  </div>
</div>
<script>window.AC_USER_TZ = {user_tz_js};</script>
<script>window.AC_AUTHENTICATED = {json.dumps(bool(session))};</script>
<script>window.AGENT_CORE_WINDOW_EVENT = {json.dumps(JS_WINDOW_EVENT)};</script>
<script src="/static/js/dashboard.js?v=20260904"></script>
{events_js}
{extra_js}
</body>
</html>""",
        status_code=status_code,
    )
    # A stylesheet or script that changed must reach the browser that cached the old
    # one: the version in each /static link is the file's own content hash, so no one
    # has to remember to bump it by hand.
    body = _versioned_assets(response.body.decode("utf-8")).encode("utf-8")
    response.body = body
    response.headers["content-length"] = str(len(body))
    return response


_STATIC_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "static"
# Only real stylesheet and script references: a file name with an extension, inside href= or src=.
# Text on a page that merely looks like one (a memory, a credential name) is left alone.
_ASSET_LINK = re.compile(r'((?:href|src)=")(/static/(?:css|js)/[A-Za-z0-9_\-]+\.(?:css|js))\?v=[A-Za-z0-9_.\-]+')
_asset_hashes: dict[str, tuple[int, str]] = {}


def _asset_version(url_path: str) -> str | None:
    """Short content hash of one static file, recomputed only when the file changes."""
    file = _STATIC_DIR / url_path.removeprefix("/static/")
    try:
        if not file.is_file():
            return None
        mtime = file.stat().st_mtime_ns
        cached = _asset_hashes.get(url_path)
        if cached and cached[0] == mtime:
            return cached[1]
        digest = hashlib.sha256(file.read_bytes()).hexdigest()[:10]
    except OSError:
        return None
    _asset_hashes[url_path] = (mtime, digest)
    return digest


def _versioned_assets(html: str) -> str:
    def swap(match):
        version = _asset_version(match.group(2))
        return f"{match.group(1)}{match.group(2)}?v={version}" if version else match.group(0)

    return _ASSET_LINK.sub(swap, html)


def _parse_manual_prune_cutoff(before_date: str) -> tuple[str, str]:
    raw = str(before_date or "").strip().replace("Z", "+00:00")
    if not raw:
        raise ValueError("before_date is required")
    if len(raw) == 10 and "T" not in raw:
        raw = raw + "T00:00:00+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.isoformat(), dt.strftime("%Y-%m-%d %H:%M:%S")


def _build_pagination(page: int, total_pages: int, extra_qs: str = "") -> str:
    """Render a pagination control: Prev, numbered pages with ellipsis, Next."""
    sep = "&amp;" if extra_qs else ""
    qs = f"{sep}{extra_qs}" if extra_qs else ""

    def page_url(p: int) -> str:
        return f"?page={p}{qs}"

    prev_cls = "btn btn-sm btn-secondary" + (" disabled" if page <= 1 else "")
    next_cls = "btn btn-sm btn-secondary" + (" disabled" if page >= total_pages else "")
    prev_href = page_url(max(1, page - 1))
    next_href = page_url(min(total_pages, page + 1))

    # Build the set of page numbers to show: always first, last, and window around current
    show = set()
    show.add(1)
    show.add(total_pages)
    for p in range(max(1, page - 2), min(total_pages, page + 2) + 1):
        show.add(p)

    nums_html = ""
    prev_shown = 0
    for p in sorted(show):
        if prev_shown and p - prev_shown > 1:
            nums_html += "<span style='padding:0 4px;color:var(--text-muted)'>…</span>"
        active_style = "background:var(--accent-fill);color:#fff;" if p == page else ""
        nums_html += f"<a href='{page_url(p)}' class='btn btn-sm btn-secondary' style='min-width:32px;{active_style}'>{p}</a>"
        prev_shown = p

    return (
        f"<div class='pagination' style='margin-top:12px;display:flex;gap:6px;align-items:center;flex-wrap:wrap'>"
        f"<a href='{prev_href}' class='{prev_cls}'>&#8592; Prev</a>"
        f"{nums_html}"
        f"<a href='{next_href}' class='{next_cls}'>Next &#8594;</a>"
        f"</div>"
    )


def _flatten_text(value) -> list[str]:
    texts: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            texts.extend(_flatten_text(item))
    elif isinstance(value, list):
        for item in value:
            texts.extend(_flatten_text(item))
    elif value is not None:
        texts.append(str(value))
    return texts


def _query_in_values(query: str, *values) -> bool:
    q = query.lower()
    for value in values:
        for text in _flatten_text(value):
            if q in text.lower():
                return True
    return False


def _json_loads(value):
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed


def _search_result(kind: str, title: str, summary: str, href: str, meta: str = "") -> dict:
    return {
        "kind": kind,
        "title": title,
        "summary": summary,
        "href": href,
        "meta": meta,
    }
