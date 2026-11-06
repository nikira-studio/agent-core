"""Two network limits that an install turns up in steps: report first, then enforce.

1. Whether services, webhooks and imports may reach private network addresses.
   ``allow`` (what installs did before), ``report`` (records what it would have
   blocked and blocks nothing) or ``block``. A fresh install starts on ``block``;
   an install that already had data keeps ``allow``. Addresses that are always
   fine are listed under *Allowed private addresses* (hosts, addresses or
   ranges), in Settings → Security.

2. Who may call the credential broker (``/internal/credentials/resolve``).
   With no list, any caller holding the broker credential is accepted, as before,
   and Overview says so quietly. With a list (addresses or ranges; loopback
   always may) the mode decides: ``audit`` (the default) records every caller it
   would have refused and refuses nobody, ``enforce`` refuses them.

Modes and lists live in the dashboard. The environment variables
(``AGENT_CORE_INTERNAL_HOSTS_MODE``, ``AGENT_CORE_BLOCK_INTERNAL_HOSTS``,
``AGENT_CORE_ALLOWED_INTERNAL_HOSTS``, ``AGENT_CORE_BROKER_ALLOWED_HOSTS``,
``AGENT_CORE_BROKER_ALLOWLIST_MODE``) still work and win where they are set;
Settings shows which value comes from where. Nothing here turns itself up: a
person changes a mode, and every change is audited.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import threading
import time
from typing import Iterable, Optional

from app.branding import ENV_PREFIX
from app.config import settings

logger = logging.getLogger(__name__)

INTERNAL_MODES = ("allow", "report", "block")
BROKER_MODES = ("audit", "enforce")
KEY_INTERNAL_MODE = "internal_hosts_mode"
KEY_INTERNAL_ALLOWED = "allowed_internal_hosts"
KEY_BROKER_ALLOWED = "broker_allowed_hosts"
KEY_BROKER_MODE = "broker_allowlist_mode"
_CACHE_SECONDS = 15
_cache: dict = {"at": 0.0, "values": {}}
_seen: dict[str, float] = {}
_seen_lock = threading.Lock()
# One record per host (or caller) per hour: enough to count and name them, not a flood.
_DEDUPE_SECONDS = 3600

ALLOW_HINT = "Allow it under Settings → Security → Allowed private addresses."


# --- stored values -----------------------------------------------------------------

def _stored() -> dict:
    now = time.time()
    if _cache["values"] and now - _cache["at"] < _CACHE_SECONDS:
        return _cache["values"]
    values: dict = {}
    try:
        from app.database import get_db

        with get_db() as conn:
            rows = conn.execute(
                "SELECT key, value FROM system_settings WHERE key IN (?, ?, ?, ?)",
                (KEY_INTERNAL_MODE, KEY_INTERNAL_ALLOWED, KEY_BROKER_ALLOWED, KEY_BROKER_MODE),
            ).fetchall()
        values = {row["key"]: row["value"] for row in rows}
    except Exception:
        logger.exception("Could not read the network limits; using the safe defaults")
    _cache.update(at=now, values=values or {"_empty": ""})
    return _cache["values"]


def reset_cache() -> None:
    _cache.update(at=0.0, values={})


def _list_value(raw: Optional[str]) -> list[str]:
    try:
        value = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


def _csv(raw: str) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _network(entry: str):
    try:
        return ipaddress.ip_network(entry, strict=False)
    except ValueError:
        return None


# --- private addresses -------------------------------------------------------------

def _env_set(name: str) -> bool:
    return f"{ENV_PREFIX}{name}" in os.environ


def internal_hosts_mode() -> str:
    """allow, report or block: the environment if it says, else the dashboard setting seeded at install."""
    explicit = (settings.INTERNAL_HOSTS_MODE or "").strip().lower()
    if explicit in INTERNAL_MODES:
        return explicit
    if settings.BLOCK_INTERNAL_HOSTS:
        return "block"  # the older switch
    if _env_set("BLOCK_INTERNAL_HOSTS"):
        return "allow"
    stored = (_stored().get(KEY_INTERNAL_MODE) or "").strip().lower()
    return stored if stored in INTERNAL_MODES else "allow"


def internal_mode_source() -> str:
    explicit = (settings.INTERNAL_HOSTS_MODE or "").strip().lower()
    if explicit in INTERNAL_MODES or settings.BLOCK_INTERNAL_HOSTS or _env_set("BLOCK_INTERNAL_HOSTS"):
        return "environment"
    return "setting"


def allowed_internal_entries() -> list[str]:
    """Hosts, addresses and ranges that are always allowed: the environment's plus the dashboard's."""
    entries = [e.lower() for e in _csv(settings.ALLOWED_INTERNAL_HOSTS)]
    entries += [e.lower() for e in _list_value(_stored().get(KEY_INTERNAL_ALLOWED))]
    return sorted(set(entries))


def host_allowed(host: str, ip=None) -> bool:
    """True for a host the person said is fine: by name, by address, or inside an allowed range."""
    host = (host or "").lower()
    literal = None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass
    for entry in allowed_internal_entries():
        if entry == host:
            return True
        network = _network(entry)
        if network is None:
            continue
        for candidate in (ip, literal):
            if candidate is not None and candidate.version == network.version and candidate in network:
                return True
    return False


def blocked_message(host: str, address: str, mode: str = "block") -> str:
    where = f"{host} is a private address" if host == address else f"{host} points to a private address ({address})"
    return f"Blocked: {where}. {ALLOW_HINT}"


def check_url(url: str) -> Optional[str]:
    """The plain message for a URL that cannot be added, or None. Used when something is being added, not first at run time."""
    from app.security.url_validation import resolve_public_url

    try:
        resolve_public_url(url)
    except ValueError as exc:
        return str(exc)
    return None


def note_would_block(host: str, address: str) -> None:
    """Report mode: record a private host the policy would have blocked, once an hour per host."""
    if not _once(f"internal:{host}"):
        return
    try:
        from app.services import audit_service

        audit_service.write_event(
            actor_type="system", actor_id="network-policy", action="internal_host_would_block",
            resource_type="url", resource_id=host, result="blocked",
            details={"host": host, "address": address},
        )
    except Exception:
        logger.exception("Could not record a private host the policy would block")


def would_block_hosts(days: int = 7) -> list[dict]:
    """Private hosts that report mode saw this week, newest first, so a person can allow the ones they know."""
    from app.database import get_db

    with get_db() as conn:
        rows = conn.execute(
            "SELECT resource_id AS host, MAX(timestamp) AS last_seen, details_json "
            "FROM audit_log WHERE action = 'internal_host_would_block' AND timestamp >= datetime('now', ?) "
            "GROUP BY resource_id ORDER BY last_seen DESC LIMIT 50",
            (f"-{int(days)} days",),
        ).fetchall()
    result = []
    for row in rows:
        try:
            address = json.loads(row["details_json"] or "{}").get("address", "")
        except ValueError:
            address = ""
        result.append({"host": row["host"], "address": address, "last_seen": row["last_seen"]})
    return result


# --- the credential broker ---------------------------------------------------------

def broker_entries() -> list[str]:
    entries = _csv(settings.BROKER_ALLOWED_HOSTS) + _list_value(_stored().get(KEY_BROKER_ALLOWED))
    return sorted(set(entries))


def broker_networks() -> list:
    networks = []
    for entry in broker_entries():
        network = _network(entry)
        if network is None:
            logger.warning("Ignoring a broker caller entry that is not an address or range: %r", entry)
        else:
            networks.append(network)
    return networks


def broker_mode() -> str:
    if _env_set("BROKER_ALLOWLIST_MODE"):
        mode = (settings.BROKER_ALLOWLIST_MODE or "").strip().lower()
    else:
        mode = (_stored().get(KEY_BROKER_MODE) or "audit").strip().lower()
    return mode if mode in BROKER_MODES else "audit"


def _once(key: str) -> bool:
    now = time.time()
    with _seen_lock:
        if now - _seen.get(key, 0) < _DEDUPE_SECONDS:
            return False
        _seen[key] = now
        if len(_seen) > 5000:
            for stale in [k for k, t in _seen.items() if now - t > _DEDUPE_SECONDS]:
                _seen.pop(stale, None)
    return True


def broker_caller_allowed(client_ip: str, agent_id: Optional[str] = None) -> bool:
    """False only when the caller must be refused now. In audit mode a caller that would be refused is recorded and allowed."""
    networks = broker_networks()
    if not networks:
        return True
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        address = None
    if address is not None and (address.is_loopback or any(address.version == n.version and address in n for n in networks)):
        return True
    mode = broker_mode()
    if _once(f"broker:{mode}:{client_ip}"):
        from app.services import audit_service

        audit_service.write_event(
            actor_type="broker", actor_id=agent_id or "unknown",
            action="broker_caller_refused" if mode == "enforce" else "broker_caller_would_refuse",
            resource_type="credential", resource_id=client_ip, result="blocked",
            details={"address": client_ip, "mode": mode},
        )
    return mode != "enforce"


# --- changing it, and showing it ---------------------------------------------------

_HOSTNAME = __import__("re").compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")


def _validate_entries(entries: Iterable[str], *, hostnames: bool) -> list[str]:
    cleaned = []
    for raw in entries:
        entry = str(raw).strip().lower()
        if not entry:
            continue
        if _network(entry) is None and not (hostnames and _HOSTNAME.match(entry)):
            raise ValueError(f"'{entry}' is not {'a host name, ' if hostnames else ''}an address or a range such as 192.168.1.0/24")
        cleaned.append(entry)
    return sorted(set(cleaned))


def _write(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO system_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
        (key, value),
    )


def update(changes: dict, *, changed_by: str) -> dict:
    """Apply a person's change; returns before and after. Raises ValueError for anything not allowed."""
    from app.database import get_db
    from app.services import audit_service

    before = status()
    wanted: dict[str, str] = {}
    if "internal_mode" in changes:
        if internal_mode_source() == "environment":
            raise ValueError("The private-address mode is set by an environment variable on the server; change it there.")
        if changes["internal_mode"] not in INTERNAL_MODES:
            raise ValueError("Private addresses: choose allow, report or block")
        wanted[KEY_INTERNAL_MODE] = changes["internal_mode"]
    if "allowed_internal" in changes:
        wanted[KEY_INTERNAL_ALLOWED] = json.dumps(_validate_entries(changes["allowed_internal"], hostnames=True))
    if "broker_allowed" in changes:
        wanted[KEY_BROKER_ALLOWED] = json.dumps(_validate_entries(changes["broker_allowed"], hostnames=False))
    if "broker_mode" in changes:
        if _env_set("BROKER_ALLOWLIST_MODE"):
            raise ValueError("The broker mode is set by an environment variable on the server; change it there.")
        if changes["broker_mode"] not in BROKER_MODES:
            raise ValueError("Broker callers: choose audit or enforce")
        wanted[KEY_BROKER_MODE] = changes["broker_mode"]
    if not wanted:
        raise ValueError("Nothing to change")
    with get_db() as conn:
        for key, value in wanted.items():
            _write(conn, key, value)
        conn.commit()
    reset_cache()
    after = status()
    audit_service.write_event(
        actor_type="user", actor_id=changed_by, action="network_limits_changed",
        resource_type="settings", resource_id="network-limits", result="success",
        details={"before": _compact(before), "after": _compact(after)},
    )
    return {"before": before, "after": after}


def _compact(state: dict) -> dict:
    return {
        "internal_mode": state["internal"]["mode"],
        "allowed_private": state["internal"]["allowed"],
        "broker_mode": state["broker"]["mode"],
        "broker_allowed": state["broker"]["allowed"],
    }


def status() -> dict:
    """What Settings and Overview show: the modes, the lists and how often each would have refused this week."""
    from app.database import get_db

    with get_db() as conn:
        def count(action: str) -> int:
            return conn.execute(
                "SELECT COUNT(DISTINCT resource_id) FROM audit_log WHERE action = ? AND timestamp >= datetime('now', '-7 days')",
                (action,),
            ).fetchone()[0]

        would_refuse = count("broker_caller_would_refuse")
        refused = count("broker_caller_refused")
        would_block = count("internal_host_would_block")
    broker = broker_entries()
    return {
        "broker": {
            "restricted": bool(broker),
            "allowed": broker,
            "mode": broker_mode(),
            "mode_from_environment": _env_set("BROKER_ALLOWLIST_MODE"),
            "would_refuse": would_refuse,
            "refused": refused,
        },
        "internal": {
            "mode": internal_hosts_mode(),
            "mode_from_environment": internal_mode_source() == "environment",
            "allowed": allowed_internal_entries(),
            "would_block": would_block,
            "would_block_hosts": would_block_hosts() if would_block else [],
        },
    }


def health_lines() -> list[tuple[str, str, bool]]:
    """(label, value, is it fine) for the Overview health strip; quiet unless something is open."""
    state = status()
    lines = []
    internal = state["internal"]
    if internal["mode"] == "report":
        n = internal["would_block"]
        lines.append(("Private addresses", f"report only; {n} would be blocked" if n else "report only; nothing would be blocked", not n))
    elif internal["mode"] == "allow":
        lines.append(("Private addresses", "allowed everywhere (not blocked)", True))
    else:
        lines.append(("Private addresses", "blocked unless allowed", True))
    broker = state["broker"]
    if not broker["restricted"]:
        lines.append(("Credential broker", "any caller with its credential (no allowed callers set)", True))
    elif broker["mode"] == "audit":
        n = broker["would_refuse"]
        lines.append(("Credential broker", f"report only; {n} callers would be refused" if n else "report only; nothing would be refused", not n))
    else:
        lines.append(("Credential broker", "only the allowed callers", True))
    return lines


def startup_warnings() -> list[str]:
    warnings = []
    if not broker_networks():
        warnings.append(
            "The credential broker accepts any caller that holds the broker credential. "
            "Set the allowed callers under Settings → Security."
        )
    if internal_hosts_mode() == "allow":
        warnings.append(
            "Services, webhooks and imports may reach private network addresses. "
            "Set Private addresses to report only under Settings → Security to see which, then block."
        )
    return warnings
