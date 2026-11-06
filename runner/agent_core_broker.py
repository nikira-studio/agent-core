#!/usr/bin/env python3
"""
Agent Core Credential Broker

v1: local execution wrapper that resolves AC_SECRET_* references for a trusted agent_id.
Not an invisible MCP interceptor — invoked explicitly by integrations.

Usage:
    python agent_core_broker.py --agent-id <id> --mode env [-- CMD ...]
    python agent_core_broker.py --agent-id <id> --mode header

Environment:
    AGENT_CORE_BROKER_URL   Base URL for Agent Core (default http://localhost:3500)
    AGENT_CORE_BROKER_TOKEN  Broker credential (ac_broker_...)
    AGENT_CORE_BROKER_TOKEN_FILE  File containing broker credential
    AGENT_CORE_TRUSTED_AGENT Override for --agent-id (recommended over passing it in)
    AGENT_CORE_BROKER_PLAIN_HTTP  What to do when the URL is plain http:// to a host other than this
                             machine: warn (default) or refuse. The broker credential and every secret
                             it fetches would cross the network unencrypted. --allow-plain-http (or
                             AGENT_CORE_BROKER_ALLOW_PLAIN_HTTP=1) says the link is trusted and silences it.
"""

import argparse
import os
import re
import subprocess
import sys
import json
import urllib.request
import urllib.error
import ipaddress
from urllib.parse import urlsplit


AC_SECRET_RE = re.compile(r"\b(AC_SECRET_[A-Z0-9_]+)\b")
DEFAULT_TOKEN_FILES = (
    "data/broker.credential",
    "/data/broker.credential",
)


def _is_loopback_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_transport(base_url: str, policy: str = "warn", allow_plain_http: bool = False) -> tuple[bool, str]:
    """(ok to continue, message to print or ""). Plain http:// is fine only to this machine.

    Only http and https are accepted. Plain http:// to any other host, including a private address or
    a Docker network name, sends the broker credential and the secret values in the clear to anything
    that can watch that network, so it warns by default and refuses when the policy says so.
    """
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False, f"ERROR: the broker URL must be http:// or https://, got {base_url!r}"
    if parts.scheme == "https" or _is_loopback_host(parts.hostname) or allow_plain_http:
        return True, ""
    message = (
        f"{'ERROR' if policy == 'refuse' else 'WARNING'}: {base_url} is plain http:// to {parts.hostname}. "
        "The broker credential and every secret it fetches cross the network unencrypted, readable by "
        "anything on that network (a private address or a Docker network name does not change that). "
        "Use https://, or a loopback or tunnelled address. If this link is trusted, pass --allow-plain-http."
    )
    return policy != "refuse", message


def load_broker_token(explicit_token: str = "", token_file: str = "") -> str:
    if explicit_token:
        return explicit_token.strip()

    candidates = []
    if token_file:
        candidates.append(token_file)
    env_file = os.environ.get("AGENT_CORE_BROKER_TOKEN_FILE", "")
    if env_file:
        candidates.append(env_file)
    candidates.extend(DEFAULT_TOKEN_FILES)

    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                token = f.read().strip()
            if token:
                return token
        except OSError:
            continue
    return ""


def resolve_reference(
    variable_name: str, agent_id: str, broker_token: str, base_url: str
) -> str | None:
    req = urllib.request.Request(
        f"{base_url}/internal/credentials/resolve",
        data=json.dumps(
            {"variable_name": variable_name, "agent_id": agent_id}
        ).encode(),
        headers={
            "Authorization": f"Broker {broker_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            if body.get("ok"):
                return body["data"]["value"]
    except (OSError, ValueError, KeyError, TypeError):
        # HTTPError/URLError are OSErrors; a read timeout is a bare TimeoutError
        # (also OSError); JSONDecodeError is a ValueError; a non-dict body is a
        # TypeError. The wrapped command still runs, without the variable.
        pass
    return None


def _scan_for_references(text: str) -> list[str]:
    return list(set(AC_SECRET_RE.findall(text)))


def _resolve_all_refs(
    text: str, agent_id: str, broker_token: str, base_url: str
) -> str:
    refs = _scan_for_references(text)
    if not refs:
        return text
    for ref in refs:
        val = resolve_reference(ref, agent_id, broker_token, base_url)
        if val is not None:
            text = text.replace(ref, val)
    return text


def run_env_mode(
    agent_id: str, cmd: list[str], broker_token: str, base_url: str
) -> int:
    resolved_env = {}
    for key, val in os.environ.items():
        resolved_env[key] = _resolve_all_refs(val, agent_id, broker_token, base_url)

    result = subprocess.run(cmd, env=resolved_env)
    return result.returncode


def run_header_mode(
    agent_id: str, cmd: list[str], broker_token: str, base_url: str
) -> int:
    extra_env = {}
    for key, val in os.environ.items():
        resolved = _resolve_all_refs(val, agent_id, broker_token, base_url)
        if resolved != val:
            safe_key = key.upper().replace("-", "_")
            extra_env[f"AC_HEADER_{safe_key}"] = resolved

    merged_env = {**os.environ, **extra_env}
    result = subprocess.run(cmd, env=merged_env)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Core Credential Broker")
    parser.add_argument("--agent-id", required=True, help="Trusted Agent Core agent_id")
    parser.add_argument(
        "--mode",
        choices=["env", "header"],
        default="env",
        help="Injection mode: env (child process env vars) or header (AC_HEADER_ prefix)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AGENT_CORE_BROKER_URL", "http://localhost:3500"),
        help="Agent Core base URL",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("AGENT_CORE_BROKER_TOKEN", ""),
        help="Broker credential token (or set AGENT_CORE_BROKER_TOKEN)",
    )
    parser.add_argument(
        "--token-file",
        default="",
        help="File containing broker credential (default: data/broker.credential or /data/broker.credential)",
    )
    parser.add_argument(
        "--allow-plain-http",
        action="store_true",
        default=os.environ.get("AGENT_CORE_BROKER_ALLOW_PLAIN_HTTP", "") in ("1", "true", "yes"),
        help="Say that a plain http:// link to another host is trusted (silences the warning)",
    )
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to run")
    args = parser.parse_args()

    broker_token = load_broker_token(args.token, args.token_file)
    if not broker_token:
        print(
            "ERROR: broker token required via --token, AGENT_CORE_BROKER_TOKEN, --token-file, or data/broker.credential",
            file=sys.stderr,
        )
        return 1
    if not args.cmd:
        print("ERROR: -- cmd [...] required", file=sys.stderr)
        return 1

    policy = os.environ.get("AGENT_CORE_BROKER_PLAIN_HTTP", "warn").strip().lower()
    proceed, message = check_transport(args.base_url, "refuse" if policy == "refuse" else "warn", args.allow_plain_http)
    if message:
        print(message, file=sys.stderr)
    if not proceed:
        return 1

    trusted_agent = os.environ.get("AGENT_CORE_TRUSTED_AGENT", args.agent_id)

    if args.mode == "env":
        return run_env_mode(trusted_agent, args.cmd, broker_token, args.base_url)
    else:
        return run_header_mode(trusted_agent, args.cmd, broker_token, args.base_url)


if __name__ == "__main__":
    sys.exit(main() or 0)
