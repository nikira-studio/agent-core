# Phase 4 — cli Backend + github_cli Reference Adapter

> Design for: `cli` backend engine, `github_cli` reference adapter, and `git:` install source.

---

## 1. Overview

Phase 4 delivers three sub-phases:

| Sub-phase | Description |
|---|---|
| **4a** | `cli` backend engine (`CliEngine`) + `github_cli` adapter manifest |
| **4b** | UI consolidation — collapse three add-buttons into one "+ Add Connector" with method sub-choice |
| **4c** | `git:owner/repo@ref` install source with dangerous-pattern scan for `mcp`/`cli` backends |

**Deferred (stated explicitly):** hosted adapter registry and the Directory registry source.

---

## 2. Sub-phase 4a — cli Backend Engine

### 2.1 Security Model

cli adapters run **never in-process Python** — the `gh` binary (or any cli tool) runs as a subprocess with:

- `shell=False` — no shell interpolation
- `env=` passed explicitly (never inherits full environment)
- argv as a flat list (never a shell string)
- stdin/stdout/stderr captured and bounded

This is sufficient. The `/`-rejection idea from the draft is **removed** — it would break real adapters (e.g. `params.repo = "owner/repo"`).

### 2.2 CliEngine Interface

`sibling to HttpEngine; dispatched by _resolve_executor on `connector_type_id`; same `execute(action, params, credential, config_json, session=None)` call signature; no new dispatch path.

```python
class CliEngine:
    def __init__(self, connector_type: dict): ...
    def execute(self, action: str, params: dict, credential: Credential,
                config_json: Optional[str], session: Optional[dict] = None) -> dict: ...
    def refresh_session(self, credential, config_json, current_session) -> dict: ...
```

### 2.3 Manifest Schema Extensions

The `backend` block schema is extended to support `cli`-specific fields. The `manifest.py` `ADAPTER_MANIFEST_SCHEMA` backend properties are expanded:

```python
"backend": {
    "type": "object",
    "required": ["type"],
    "properties": {
        "type": {"type": "string", "enum": ["http", "mcp", "cli"]},
        # http fields (unchanged)
        "base_url": {...},
        "auth": {"type": "object"},
        "session": {"type": "object"},
        "refresh": {"type": "object"},
        "requests": {"type": "object"},
        # cli fields (new)
        "bin": {"type": "string", "description": "Binary name or absolute path to invoke"},
        "timeout": {"type": "integer", "description": "Default timeout in seconds per command"},
        "env": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Environment variables injected into subprocess env. Values support {{ }} templates."
        },
        "commands": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": ["args"],
                "properties": {
                    "description": {"type": "string"},
                    "args": {
                        "type": "array",
                        "description": "Flat argv array. Elements may be strings or the sentinel '__OMIT__'. "
                                      "Pairs of [flag, value] where value is '__OMIT__' after rendering "
                                      "are dropped entirely from the argv. Supports {{ params.* }}, "
                                      "{{ cred.* }}, {{ config.* }} templates.",
                        "items": {"oneOf": [{"type": "string"}, {"type": "string", "enum": ["__OMIT__"]}]}
                    },
                    "stdin": {
                        "type": "string",
                        "description": "Optional stdin content. Supports {{ params.* }}, {{ cred.* }}, {{ config.* }} templates."
                    },
                    "timeout": {"type": "integer", "description": "Per-command timeout override (seconds)."},
                    "parse": {
                        "type": "object",
                        "required": ["type"],
                        "properties": {
                            "type": {"type": "string", "enum": ["jsonpath", "regex", "text"]},
                            "path": {"type": "string", "description": "jsonpath expression (for type=jsonpath) or regex pattern (for type=regex)"},
                            "group": {"type": "integer", "description": "Regex capture group index. Default 0 (full match)."}
                        }
                    },
                    "success_exit_codes": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Exit codes considered success. Default [0]."
                    }
                }
            }
        }
    }
}
```

### 2.4 Arg Rendering: `__OMIT__` Sentinel

When an arg element renders to empty string (`""`), it is replaced with `__OMIT__`. After full rendering, the engine scans argv left-to-right: any `__OMIT__` element is removed, **and the preceding element is also removed** (assumed to be a flag that takes an argument). This enables conditional flag pairs:

```
# gh repo list --owner myorg --limit 30
["repo", "list", "--json", "name,url,visibility", "--owner", "{{ params.owner | default('', as=str) }}", "--limit", "{{ params.limit | default(30, as=int) }}"]

# When params.owner = "" → renders to ["repo", "list", "--json", "...", "--owner", "__OMIT__", "--limit", "30"]
# Engine drops the --owner/__OMIT__ pair → ["repo", "list", "--json", "...", "--limit", "30"]
# gh uses authenticated user as default — no --owner needed
```

This pattern works for any flag-value pair. It does **not** require a chained-fallback filter.

### 2.5 Output Parsing

| `parse.type` | Description |
|---|---|
| `jsonpath` | `path` is a jsonpath expression evaluated against the parsed JSON stdout. `$` = entire output. |
| `regex` | `path` is a regex with one or more capture groups. Returns the captured group(s) as a list, or the first group as a string. |
| `text` | Returns the raw stdout as a string. Used when CLI has no structured output. |

### 2.6 Error Handling

| Condition | Result |
|---|---|
| `subprocess.TimeoutExpired` | `{"success": False, "error_code": "TIMEOUT", "error": "Command timed out after N seconds"}` |
| Non-zero exit (not in `success_exit_codes`) | `{"success": False, "error_code": "EXEC_ERROR", "error": stderr or "exit N"}` |
| `requires.bins` not satisfied | connector shows as unavailable (existing Phase 2 gating) |
| `stdin` template renders with missing key | flag omitted via `__OMIT__` |

### 2.7 stdin Template

`stdin` is rendered through the same locked-down template resolver, then passed as bytes to `subprocess.run(stdin=...)`. Example use: piping a YAML config to `kubectl`.

---

## 3. github_cli Reference Adapter

**`data/adapters/github_cli/adapter.json`**

Credential: `token` (gh PAT or fine-grained token — `GH_TOKEN` env var). The credential `description` notes that `GITHUB_TOKEN` is also honored by `gh` if `GH_TOKEN` is not set; the adapter sets both env vars so either works.

Key actions:

| Action | gh command | Notes |
|---|---|---|
| `list_repos` | `gh repo list --json ...` | `--owner` omitted when params.owner absent |
| `list_issues` | `gh issue list --json ...` | Requires `repo` param |
| `create_issue` | `gh issue create --json url,number` | Uses `--json` for structured output |

All commands use `--json` for structured output → `parse.type: jsonpath`.

---

## 4. Sub-phase 4b — UI Consolidation

### 4.1 Button Merge

Replace three buttons on `/connectors`:

```
# BEFORE
[+ Import API Spec]  [+ Import MCP Server]  [+ Add HTTP Connector]

# AFTER
[+ Add Connector ▼]   (dropdown: "Import API Spec" / "Import MCP Server" / "Add HTTP Connector")
```

- The dropdown triggers the same modals as today
- No change to `/connectors/directory` (keeps existing apis.guru + MCP sources)
- The "Browse API Directory" button remains unchanged
- No adapter-registry source added to Directory yet

Implementation: change three `<button>` elements into a `<select>` dropdown with JavaScript handler that opens the appropriate modal.

---

## 5. Sub-phase 4c — git: Install Source

### 5.1 Source Format

```
git:owner/repo@ref
git:owner/repo           (ref defaults to main)
```

Examples:
- `git:cli/cli-adapters@1.0.0`
- `git:agent-core/community-adapters`

### 5.2 Discovery Flow

1. User pastes `git:` URL in an "Install from git" input field
2. Engine clones the repo (shallow, depth=1) to a temp directory
3. Scans for `adapter.json` files anywhere in the tree (glob `**/adapter.json`)
4. Validates each found manifest
5. If `backend.type` is `mcp` or `cli`: run dangerous-pattern scan
6. On scan pass: copy each valid adapter to `data/adapters/<id>/`
7. Cleanup temp directory

### 5.3 Dangerous-Pattern Scan

Scans the **manifest** (not the target binary/script) for dangerous patterns in:
- `env` values
- `args` array elements
- `stdin` templates

**Dangerous patterns detected:**
- `{{ cred.raw }}` without an `as=` filter (allows arbitrary credential injection)
- `${` or `$()` (shell interpolation attempts)
- `&&`, `||`, `|` at top level of args (command chaining)
- File path traversal attempts: `{{ params.* }}` where the wire name contains `..` or starts with `/`

If any pattern is found: the adapter is **not** installed; user sees a confirmation prompt listing the flagged patterns before they can proceed.

```python
DANGEROUS_PATTERNS = [
    r"\{\{\s*cred\.raw\s*\}\}",          # raw credential without filter
    r"\$\{",                              # shell interpolation
    r"\$\(",                              # command substitution
    r"\&\&|\|\||^\|$",                   # command chaining in arg
]
```

### 5.4 Adapter Discovery

`adapter_loader.py` already discovers `data/adapters/*/adapter.json`. After a `git:` install copies adapters into `data/adapters/`, they are picked up on the next rescan (or immediately if we call `discover_and_seed_adapters` post-install).

---

## 6. Wire-Level Test Invariants

For cli, the wire-level invariants are:

1. **argv is a flat list** — no shell strings, no `shell=True`
2. **env dict is passed explicitly** — never inherits full process env
3. **stdin is rendered template output** — when `stdin` is declared
4. **Conditional args: `__OMIT__` drops the flag+value pair**
5. **TimeoutExpired → `error_code: TIMEOUT`**
6. **Non-zero exit (not in `success_exit_codes`) → `error_code: EXEC_ERROR`**
7. **`requires.bins` gating** — missing binary → connector unavailable (not a crash)

---

## 7. File Change Map

| File | Change |
|---|---|
| `app/connectors/cli_engine.py` | **new** — `CliEngine` |
| `app/connectors/manifest.py` | extend `backend` schema for cli fields (`bin`, `timeout`, `env`, `commands`) |
| `app/services/connector_service.py` | add `elif backend == "cli":` in `_resolve_executor` |
| `data/adapters/github_cli/adapter.json` | **new** |
| `tests/unit/test_cli_engine.py` | **new** — unit tests |
| `tests/integration/test_github_cli_adapter.py` | **new** — wire-level tests |
| `app/routes/connectors_page.py` | button merge (4b) |
| `app/services/adapter_loader.py` | add `install_from_git(source)` method (4c) |
| `app/security/dangerous_pattern_scanner.py` | **new** — scan manifests for dangerous patterns |
| `tests/integration/test_adapter_git_install.py` | **new** — git install + scan tests |

---

## 8. Deferred

- Hosted adapter registry
- Directory adapter-registry source
- `mcp` backend wiring (already partially exists; formalizing as a backend is a later pass)
