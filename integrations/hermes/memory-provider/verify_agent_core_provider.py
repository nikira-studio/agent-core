#!/usr/bin/env python3
"""Live verifier for the Agent Core memory provider.

Exercises the real path: Hermes discovery -> load -> is_available() ->
prefetch(query), and prints the raw context block the model would receive.

Run from the Hermes repo root (so `agent` and `plugins` import), with the
provider already dropped at ``$HERMES_HOME/plugins/agent_core/``:

    HERMES_HOME=/opt/data AGENT_CORE_API_KEY=... \\
        python verify_agent_core_provider.py "how often do you check email"

Exit code 0 = provider loaded, available, and prefetch returned content.
"""

from __future__ import annotations

import os
import sys


def _load_via_hermes(name: str):
    """Preferred: use Hermes' own loader so we test real discovery."""
    from plugins.memory import discover_memory_providers, load_memory_provider

    discovered = discover_memory_providers()
    names = [d[0] for d in discovered]
    print(f"[discovery] providers found: {names}")
    if name not in names:
        print(
            f"[discovery] '{name}' NOT discovered. Is it at "
            f"$HERMES_HOME/plugins/{name}/ and is HERMES_HOME set? "
            f"(HERMES_HOME={os.environ.get('HERMES_HOME', '<unset>')})"
        )
    return load_memory_provider(name)


def _load_directly():
    """Fallback: import the provider sitting next to this script."""
    import importlib.util

    here = os.path.dirname(os.path.abspath(__file__))
    init_file = os.path.join(here, "__init__.py")
    spec = importlib.util.spec_from_file_location("_agent_core_provider_probe", init_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.AgentCoreMemoryProvider()


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "how often do you check email"

    provider = None
    try:
        provider = _load_via_hermes("agent_core")
    except Exception as e:
        print(f"[load] Hermes loader unavailable ({e}); falling back to direct import.")
        try:
            provider = _load_directly()
        except Exception as e2:
            print(f"[load] FAILED: {e2}")
            return 2

    if provider is None:
        print("[load] FAILED: provider is None (not discovered or failed to construct).")
        return 2

    print(f"[load] provider.name = {provider.name!r}")

    available = provider.is_available()
    print(f"[is_available] {available}")
    if not available:
        print(
            "[is_available] False — set AGENT_CORE_API_KEY (or AGENT_CORE_BEARER) "
            "in this process's environment. The loader skips the provider when unset."
        )
        return 1

    provider.initialize("verify-session", hermes_home=os.environ.get("HERMES_HOME", ""), platform="cli")

    print(f"[prefetch] query = {query!r}")
    out = provider.prefetch(query)
    if out and out.strip():
        print("[prefetch] OK — raw context the conversation loop would wrap:\n")
        print("-" * 72)
        print(out)
        print("-" * 72)
        return 0

    print(
        "[prefetch] empty result. Either no matching records, the query was too "
        "short (<3 chars), or the search call failed (run with logging at DEBUG to see why)."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
