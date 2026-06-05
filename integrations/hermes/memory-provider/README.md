# Agent Core memory provider (Hermes)

Prefetch-only memory provider that recalls from an **Agent Core** instance's
`/api/memory/search` endpoint **before every turn** and injects the top hits as
a `<memory-context>` block. This is the deterministic fix for "answered from
cold recall" — the recall is in the model's context before it drafts a reply,
so it can't forget to check.

## Install (upgrade-safe — lives outside the Hermes repo)

Drop this directory at:

```
$HERMES_HOME/plugins/agent_core/
```

For this deployment `HERMES_HOME=/opt/data`, so:

```
/opt/data/plugins/agent_core/
  ├── __init__.py
  ├── plugin.yaml
  ├── README.md
  └── verify_agent_core_provider.py
```

This is the **user-installed** provider location Hermes' loader scans
(`plugins/memory/__init__.py` → bundled *and* `$HERMES_HOME/plugins/`). It is
**not** in the Hermes source tree, so `hermes` updates never touch it. Do **not**
copy it into `plugins/memory/` inside the repo, and do **not** patch
`agent/memory_manager.py`, `agent/conversation_loop.py`, or the gateway — none
of that is needed, and all of it would be overwritten on update.

## Activate

In `$HERMES_HOME/config.yaml`:

```yaml
memory:
  provider: agent_core
```

Only one external memory provider runs at a time; this becomes it.

## Configure (environment variables — keep the token out of config.yaml)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `AGENT_CORE_API_KEY` | **yes** | – | Bearer token for Agent Core. `AGENT_CORE_BEARER` is accepted as an alias. |
| `AGENT_CORE_URL` | no | `http://core.veditz.com` | Base URL of the Agent Core instance. |
| `AGENT_CORE_SCOPE` | no | `agent:clawdia` | **Reserved for v2 write-back only.** Not used for recall — prefetch searches every scope the token can read. |
| `AGENT_CORE_LIMIT` | no | `5` | Max records injected per turn. |
| `AGENT_CORE_TIMEOUT` | no | `4` | HTTP timeout (seconds). |

The token must be present **in the environment the Hermes agent process sees**
(the same place the gateway injects the Agent Core MCP bearer). If it is unset,
`is_available()` returns `False` and the loader skips this provider — Hermes runs
exactly as before.

No pip dependencies — uses only the Python standard library (`urllib`).

## Verify

After dropping the files in and exporting `AGENT_CORE_API_KEY`, run from the
Hermes repo root (where the `agent` and `plugins` packages import):

```bash
HERMES_HOME=/opt/data AGENT_CORE_API_KEY=... \
  python verify_agent_core_provider.py "how often do you check email"
```

It exercises the real discovery → load → `is_available()` → `prefetch()` path and
prints the raw context block the model would receive. Success = the relevant
record (e.g. the inbox-cron record) appears in the output.

## Design notes

- **Prefetch-only, no tools.** `get_tool_schemas()` returns `[]`. Recall is
  automatic; explicit search/write already exist as Agent Core MCP tools. Adding
  tools would just bloat the prompt. *If you later want an on-demand
  `agent_core_search` tool, that's a v2 addition — ask.*
- **Returns raw text, not a wrapped block.** `agent/conversation_loop.py` calls
  `build_memory_context_block()` once on the merged provider output, so the
  provider must return raw text (wrapping here would double-fence).
- **Read path only (v1).** `sync_turn()` is a no-op; durable writes go through
  the Agent Core MCP `memory_write` tool, so there's no recall gap. Write-back
  is a clean v2 follow-up.
- **Fails safe.** Any error in `prefetch()` returns `""` and logs at debug. A
  broken/unreachable Agent Core never breaks the agent loop.

## Optional upstream contribution

If you'd rather not maintain a provider per backend, the most-mergeable upstream
PR into `nousresearch/hermes-agent` is a **generic `mcp` memory provider** that
fronts any MCP server exposing `memory_search` / `memory_write`. That would make
this config-only for everyone. This provider is the zero-wait local path; the
generic one is the give-back.
