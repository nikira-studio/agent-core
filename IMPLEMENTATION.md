# Adapter Module System — Implementation Plan

> **Status: internal build plan for an unbuilt feature. Not user documentation, and not yet implemented.**
>
> **Read `plan.md` first** — it is the canonical design (the what/why). This file is the second of two planning docs: the file-by-file *build detail* (changes, code drafts, schema migration, tests, ordered checklist) that follows `plan.md` and must stay in sync with it. Both live at the repo root; **neither is in `docs/`** or shipped to users. **Do not change `docs/`, the DB schema docs, or the live MCP/API surface to describe this system until it is actually built.** Code drafts are written against current repo signatures (verified); treat every block as a proposed diff. Nothing is committed yet.

This supersedes the earlier draft of this file, which described "code adapters in `app/connectors/`." Per `plan.md`, the model is now: **one connector system, one execution path, multiple authoring modes** — a declarative `http` engine driven by **data manifests**, with `mcp`/`cli` wrappers for code, and `app/connectors/` reserved for engines + maintainer-shipped built-ins.

---

## 0. Build order (matches plan.md §14)

- **Phase 0 — dispatch refactor.** Keep `connector_type_id` as the runtime key; fold the dead/ad-hoc branches into one load-time engine binding. No behavior change.
- **Phase 1 — `http` engine + module core.** The declarative engine, manifest JSON Schema + loader/validator, `data/adapters/` discovery, the three primitives (Credential, opt-in session cache + refresh loop, typed errors), and per-action schemas in `connectors_actions_list`.
- **Phase 2 — Transmission** as a `data/adapters/` manifest (dogfoods the engine).
- **Phase 3 — Gmail** as a manifest (OAuth refresh).
- **Phase 4 — `cli`/`mcp` backends, install sources, UI consolidation.**

What **carries over verbatim** from the prior draft (now framed as `http`-engine internals, not per-adapter code): `errors.py`, the `Credential` value object, `connector_session_service.py`, and the `credential_service` additions. What **changes**: Transmission/Gmail become **JSON manifests**, not Python classes; a new **declarative `http` engine** interprets manifests; a new **manifest loader + `data/adapters/` discovery**; the session loop is **opt-in**.

---

## 1. File change map

| File | Change |
|---|---|
| `app/connectors/errors.py` | **new** — `ProviderError`, `SessionExpiredError`, `AuthExpiredError`, `RateLimitedError` (carryover) |
| `app/connectors/base.py` | **new** — `Credential` value object (carryover) |
| `app/connectors/manifest.py` | **new** — manifest dataclass + JSON Schema + `load_and_validate()` |
| `app/connectors/http_engine.py` | **new** — `HttpEngine`: the declarative interpreter (auth / session / refresh / request templates) |
| `app/connectors/__init__.py` | extend `BaseConnector` (the executor interface); registry; engine binding helpers; re-exports |
| `app/services/adapter_loader.py` | **new** — discover `data/adapters/*/adapter.json`, validate, seed connector_types, bind engines, gate on `requires` |
| `app/services/connector_session_service.py` | **new** — encrypted session cache + per-binding lock (carryover) |
| `app/services/credential_service.py` | add `resolve_credential()` and `update_credential_value()` (carryover) |
| `app/services/connector_service.py` | `get_binding_with_credential` → `Credential`; `_resolve_executor` returns the bound engine/executor by `connector_type_id`; `execute_binding_action` opt-in session loop; manifest-actions branch in `generate_connector_type_tools`; remove dead `provider_type=="generic_http"` branches |
| `app/connectors/generic_http.py` | accept `Credential` (`.raw`), add `session=None`, add a `manifest()`-style descriptor (it becomes a built-in connector under the unified seeder) |
| `app/connectors/openapi_executor.py` | accept `Credential` (`.raw`), add `session=None` |
| `app/services/mcp_provider_service.py` call site | pass `credential.raw` |
| `app/schema.py` | `connector_session_cache` table + `_ensure_*`; wire adapter discovery after init |
| `data/adapters/transmission/adapter.json` | **new** (Phase 2) — data manifest, not Python |
| `data/adapters/google_gmail/adapter.json` | **new** (Phase 3) — data manifest |
| `tests/...` | new unit + integration tests (§8) |

> Note the relocation: Transmission/Gmail are **`data/adapters/` JSON**, outside the source tree, surviving upgrades. `app/connectors/` holds only engines + built-ins (maintainer-owned).

---

## 2. Primitives (carry over verbatim — now `http`-engine internals)

### 2.1 `app/connectors/errors.py`

```python
class ProviderError(Exception):
    def __init__(self, message: str, error_code: str = "PROVIDER_ERROR", retryable: bool = False):
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


class SessionExpiredError(ProviderError):
    """Session handshake token stale (e.g. Transmission 409). Triggers refresh + retry-once."""
    def __init__(self, message: str = "Session expired", error_code: str = "SESSION_EXPIRED"):
        super().__init__(message, error_code=error_code, retryable=True)


class AuthExpiredError(ProviderError):
    """Credential/access token expired (e.g. OAuth 401). Triggers refresh + retry-once."""
    def __init__(self, message: str = "Auth expired", error_code: str = "AUTH_EXPIRED"):
        super().__init__(message, error_code=error_code, retryable=True)


class RateLimitedError(ProviderError):
    """Provider 429. retry_after seconds honored by the transient-retry loop."""
    def __init__(self, retry_after: float | None = None, message: str = "Rate limited"):
        super().__init__(message, error_code="RATE_LIMITED", retryable=True)
        self.retry_after = retry_after
```

### 2.2 `app/connectors/base.py` — `Credential`

```python
import json
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Credential:
    """Resolved credential. `.raw` = decrypted secret string (single-secret connectors).
    `.fields` = parsed object when the stored secret is a JSON blob (oauth/basic/cookie)."""
    raw: Optional[str]
    fields: dict = field(default_factory=dict)
    reference_name: Optional[str] = None
    entry_id: Optional[str] = None

    @classmethod
    def from_resolved(cls, raw: Optional[str], entry: Optional[dict]) -> "Credential":
        parsed: dict = {}
        if raw:
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    parsed = obj
            except (json.JSONDecodeError, TypeError):
                parsed = {}
        return cls(raw=raw, fields=parsed,
                   reference_name=(entry or {}).get("reference_name"),
                   entry_id=(entry or {}).get("id"))

    def get(self, key: str, default: Any = None) -> Any:
        return self.fields.get(key, default)

    def __bool__(self) -> bool:
        return bool(self.raw)
```

### 2.3 `app/services/connector_session_service.py`

```python
import json
import threading
from typing import Optional

from app.database import get_db
from app.security.encryption import encrypt_value, decrypt_value

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def binding_lock(binding_id: str) -> threading.Lock:
    """Per-binding refresh mutex. In-process by design (single-core; see plan.md §1/§8.3).
    Swap for a DB `locked_until` compare-and-set only if Agent Core ever goes multi-process."""
    with _locks_guard:
        lk = _locks.get(binding_id)
        if lk is None:
            lk = threading.Lock()
            _locks[binding_id] = lk
        return lk


def load_session(binding_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT session_data_encrypted FROM connector_session_cache WHERE binding_id = ?",
            (binding_id,),
        ).fetchone()
    if not row or not row["session_data_encrypted"]:
        return None
    try:
        return json.loads(decrypt_value(row["session_data_encrypted"]))
    except Exception:
        return None


def save_session(binding_id: str, session: Optional[dict], expires_at: Optional[str] = None) -> None:
    if session is None:
        clear_session(binding_id)
        return
    blob = encrypt_value(json.dumps(session))
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO connector_session_cache (binding_id, session_data_encrypted, expires_at, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(binding_id) DO UPDATE SET
                session_data_encrypted = excluded.session_data_encrypted,
                expires_at = excluded.expires_at, updated_at = CURRENT_TIMESTAMP
            """,
            (binding_id, blob, expires_at),
        )
        conn.commit()


def clear_session(binding_id: str) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM connector_session_cache WHERE binding_id = ?", (binding_id,))
        conn.commit()
```

### 2.4 `app/services/credential_service.py` additions

```python
def resolve_credential(reference_name: str) -> Optional["Credential"]:
    from app.connectors.base import Credential
    entry = get_credential_by_reference(reference_name)
    if not entry:
        return None
    if entry.get("expires_at") and utc_now() > parse_utc_datetime(entry["expires_at"]):
        return None  # NOTE: this is the *credential entry* lifetime, NOT an OAuth access-token expiry
    return Credential.from_resolved(decrypt_value(entry["value_encrypted"]), entry)


def update_credential_value(reference_name: str, new_plaintext: str, actor: Optional[str] = None) -> bool:
    """Re-encrypt a credential's secret in place — used by OAuth refresh to persist rotated tokens."""
    entry = get_credential_by_reference(reference_name)
    if not entry:
        return False
    return update_credential(entry["id"], value_encrypted=encrypt_value(new_plaintext))
```

> **Hard rule (plan.md §8.1):** never store an OAuth access-token expiry in the credential row's `expires_at`; access-token expiry lives inside the JSON blob. Otherwise `resolve_credential` returns `None` once the access token lapses and the still-valid refresh token becomes unreachable.

---

## 3. Backend model & dispatch (Phase 0)

`connector_type_id` stays the **runtime dispatch key**. `_resolve_executor(connector_type)` returns the executor bound to that id; `backend.type` only chose *which* executor at load time. All executors share one interface:

```python
# app/connectors/__init__.py
class BaseConnector:
    """Executor interface. Implementations: HttpEngine (declarative), the MCP path,
    OpenApiExecutor, generic_http, and any maintainer-shipped built-in."""
    connector_type_id: str = ""

    def test_connection(self, credential: "Credential", config_json) -> dict: ...
    def execute(self, action, params, credential: "Credential", config_json, session=None) -> dict: ...
    def refresh_session(self, credential, config_json, current_session) -> dict:  # optional
        raise NotImplementedError

    # Capability flag read once at load/bind time — drives the opt-in session loop (§6).
    needs_session: bool = False
```

`_resolve_executor` resolution order (revised, registry-first, no dead branches):

```python
def _resolve_executor(connector_type: dict):
    from app.connectors import get_connector
    impl = get_connector(connector_type["id"])      # registered code connector / built-in
    if impl:
        return impl
    backend = (connector_type.get("backend_type")
               or _infer_backend(connector_type))   # "http" | "mcp" | "openapi" | "generic_http"
    if backend == "http":
        from app.connectors.http_engine import HttpEngine
        return HttpEngine(connector_type)            # shared engine, configured by this manifest
    if backend == "openapi" or connector_type.get("operations_json"):
        from app.connectors.openapi_executor import OpenApiExecutor
        return OpenApiExecutor()
    return None  # mcp handled in execute_binding_action as today
```

(The `mcp` path stays where it is for now — it can become a `McpBackend` executor in Phase 4 so the inline branch disappears too. Phase 0 only removes the dead `provider_type=="generic_http"` branches at connector_service.py:326/344/691; `generic_http` already resolves via the registry, `generic_http.py:147`.)

`backend_type` is a new nullable column on `connector_types` (additive migration), set when an adapter is loaded. For existing rows it's inferred (`mcp`/`operations_json`/`generic_http`).

---

## 4. The declarative `http` engine (`app/connectors/http_engine.py`, NEW — core of Phase 1)

`HttpEngine` is one class, instantiated per connector_type with its manifest. It interprets the `backend.http` block; **no per-adapter code.**

Responsibilities:
- **Build the request** for an action from its `requests[action]` template: method, path interpolation, param→location mapping (query/path/header/body), body template.
- **Apply auth** from the `auth` block: `api_key`/`bearer`/`basic`/`custom_header`/`oauth2`, reading `credential.raw` or `credential.fields`.
- **Session**: if a `session` block (e.g. `challenge_retry`) is present, apply the cached session token to the request; on the trigger (e.g. HTTP 409) raise `SessionExpiredError` (and implement `refresh_session` to capture the new token).
- **Refresh**: if a `refresh` block is present (OAuth), detect expiry/401 and raise `AuthExpiredError`; implement `refresh_session` to perform the token exchange and return `{session, expires_at, credential_update}`.
- **Response**: apply `response.success_when` / `response.extract`; map 429 → `RateLimitedError(retry_after)`.
- Set `needs_session = bool(manifest has session or refresh)`.

Skeleton:

```python
class HttpEngine(BaseConnector):
    def __init__(self, connector_type: dict):
        self.ct = connector_type
        self.spec = json.loads(connector_type["backend_json"])   # the backend.http block
        self.needs_session = bool(self.spec.get("session") or self.spec.get("refresh"))

    def execute(self, action, params, credential, config_json, session=None) -> dict:
        req = self._render_request(action, params, config_json)   # method/url/headers/body
        self._apply_auth(req, credential, session)
        resp = self._send(req)                                    # safe_urlopen
        if resp.status == 429:
            raise RateLimitedError(_retry_after(resp))
        if self._is_session_challenge(resp):                     # e.g. 409 per session.trigger
            raise SessionExpiredError()
        if self._is_auth_expired(resp, session, credential):     # 401 / expires_at past
            raise AuthExpiredError()
        return self._extract(resp, action)

    def refresh_session(self, credential, config_json, current_session) -> dict:
        if "session" in self.spec:   # challenge_retry: re-trigger, capture token from header
            return self._capture_session_token(credential, config_json)
        if "refresh" in self.spec:   # oauth: token exchange, map response, persist rotation
            return self._oauth_refresh(credential)
        raise NotImplementedError
```

**Templating security (decision):** manifests are shareable *data*. The `{{ params.x }}` / `{{ cred.x }}` / `{{ config.x }}` substitution must be a **locked-down resolver** (whitelisted `params.`/`cred.`/`config.` paths only) — **not** raw Jinja or `eval`, which would let a shared manifest run code. This keeps `http` adapters safe-by-construction.

---

## 5. Manifest schema, loader, and `data/adapters/` discovery (Phase 1)

### 5.1 `app/connectors/manifest.py`
- A published **JSON Schema** for the manifest envelope + each backend block (`http` first; `mcp`/`cli` in Phase 4).
- `load_and_validate(path) -> Manifest | error`: parse JSON, validate against the schema, return a typed object or a structured error (never raise into startup).

### 5.2 `app/services/adapter_loader.py`
```python
def discover_and_seed_adapters() -> None:
    """Glob data/adapters/*/adapter.json, validate each, seed/refresh its connector_type,
    record backend_type, gate on `requires`. Invalid or unmet-requirements adapters are
    logged and skipped — never crash startup."""
    for path in _glob(settings.data_dir / "adapters" / "*" / "adapter.json"):
        m, err = manifest.load_and_validate(path)
        if err:
            logger.warning("skipping adapter %s: %s", path, err); continue
        if not _requirements_met(m):     # requires.bins/env/config
            _seed_unavailable(m); continue
        _seed_connector_type(m)          # upsert connector_types incl. backend_json, backend_type
```

- `data/adapters/` lives under the existing `settings.data_dir` (persistent, beside the DB) — survives upgrades.
- Wire it at startup after `init_db()`, alongside seeding built-ins. Add a rescan endpoint later.
- `_seed_connector_type` upserts the row (idempotent), storing the manifest envelope fields + the `backend.http` block as `backend_json` and `backend_type`.

### 5.3 Per-action schemas in `generate_connector_type_tools` (connector_service.py:417)
Add a branch before the bare `supported_actions` fallback: if the connector has manifest actions (from `backend_json`/registered impl), emit each with full `input_schema` (and `parameters`, `side_effect`), so `connectors_actions_list` surfaces adapter actions identically to MCP tools. (mcp.py:1062 passes `tools` through untouched; `connectors_run` is unchanged.)

---

## 6. `execute_binding_action` — opt-in session loop (connector_service.py:710)

Keep all existing validation, rate-limit, and transient-retry behavior. The session/refresh machinery is **entered only when the executor opted in** (`needs_session`); stateless connectors keep today's exact path with zero new overhead.

```python
def execute_binding_action(binding_id, action, params=None) -> dict:
    from app.connectors.errors import ProviderError, SessionExpiredError, AuthExpiredError, RateLimitedError
    from app.services import connector_session_service as sessions
    # ... existing: load binding, connector_type, validate action, resolve Credential, rate limit ...

    executor = _resolve_executor(connector_type)            # bound by connector_type_id
    executor_config = _build_executor_config(binding, connector_type)
    max_retries, retry_base_delay = _retry_params(binding)
    uses_session = bool(getattr(executor, "needs_session", False))

    def _invoke(session):
        # ... mcp branch unchanged (credential.raw) ...
        return executor.execute(action, params or {}, credential, executor_config, session=session)

    def _run_with_session():
        nonlocal credential
        if not uses_session:
            return _invoke(None)                             # STATELESS FAST PATH: no cache, no lock
        session = sessions.load_session(binding_id)
        try:
            return _invoke(session)
        except (SessionExpiredError, AuthExpiredError):
            with sessions.binding_lock(binding_id):
                session = sessions.load_session(binding_id)  # re-read: a sibling may have refreshed
                try:
                    return _invoke(session)
                except (SessionExpiredError, AuthExpiredError):
                    refreshed = executor.refresh_session(credential, executor_config, session)
                    sessions.save_session(binding_id, refreshed.get("session"), refreshed.get("expires_at"))
                    upd = refreshed.get("credential_update")
                    if upd and credential and credential.reference_name:
                        from app.services import credential_service
                        credential_service.update_credential_value(
                            credential.reference_name, json.dumps({**credential.fields, **upd}),
                            actor="connector_refresh")
                        credential = credential_service.resolve_credential(credential.reference_name)
                    return _invoke(refreshed.get("session"))

    def _run_once():
        try:
            return _run_with_session()
        except RateLimitedError as e:
            return {"success": False, "error": str(e), "error_code": "RATE_LIMITED", "retry_after": e.retry_after}
        except ProviderError as e:
            return {"success": False, "error": str(e), "error_code": e.error_code}
        except Exception as e:
            return {"success": False, "error": str(e), "error_code": "EXECUTION_ERROR"}

    # ... existing transient-retry loop over _run_once(), honoring retry_after when present ...
```

Properties: stateless connectors never touch the cache/lock (Codex's requirement); the lock + re-read kills the OAuth rotation race; refresh-and-retry happens once; `credential_update` persists rotated tokens. `delete_binding` calls `clear_session(binding_id)`.

---

## 7. Worked adapters — as `data/adapters/` manifests (not Python)

### 7.1 `data/adapters/transmission/adapter.json` (Phase 2)
```json
{
  "spec_version": "1.0",
  "id": "transmission",
  "display_name": "Transmission",
  "version": "1.0.0",
  "description": "Transmission BitTorrent RPC — manage torrents and session stats.",
  "credential_schema": { "fields": [
    { "name": "username", "secret": false, "required": true },
    { "name": "password", "secret": true,  "required": true } ]},
  "requires": { "config": ["base_url"] },
  "actions": [
    { "name": "list_torrents", "side_effect": "read",
      "input_schema": { "type": "object", "properties": {
        "ids": { "type": "array", "items": { "type": "integer" } } } } },
    { "name": "remove_torrent", "side_effect": "destructive",
      "input_schema": { "type": "object", "properties": {
        "ids": { "type": "array", "items": { "type": "integer" } },
        "delete_data": { "type": "boolean" } }, "required": ["ids"] } }
  ],
  "backend": {
    "type": "http",
    "base_url": { "from": "config", "field": "base_url" },
    "auth": { "type": "basic" },
    "session": { "type": "challenge_retry", "trigger": { "http_status": 409 },
      "capture": { "source": "response_header", "name": "X-Transmission-Session-Id", "as": "session_id" },
      "apply":   { "target": "request_header",  "name": "X-Transmission-Session-Id", "from": "session_id" },
      "max_retries": 1 },
    "requests": {
      "list_torrents": { "method": "POST", "path": "",
        "body": { "template": { "method": "torrent-get",
          "arguments": { "fields": ["id","name","status","percentDone","uploadRatio"], "ids": "{{ params.ids }}" } } },
        "response": { "success_when": "$.result == 'success'", "extract": "$.arguments.torrents" } },
      "remove_torrent": { "method": "POST", "path": "",
        "body": { "template": { "method": "torrent-remove",
          "arguments": { "ids": "{{ params.ids }}", "delete-local-data": "{{ params.delete_data }}" } } },
        "response": { "success_when": "$.result == 'success'" } }
    }
  }
}
```
Proves: the `http` engine handles a session handshake purely from data. (Other actions — add/start/stop/get_session_stats — follow the same shape.)

### 7.2 `data/adapters/google_gmail/adapter.json` (Phase 3, sketch)
Envelope: `credential_schema` = client_id, client_secret, refresh_token, access_token, expires_at. `backend.http.auth` = `{ "type": "oauth2", "apply": { "target": "request_header", "name": "Authorization", "template": "Bearer {{ cred.access_token }}" } }`. `backend.http.refresh` = `{ "trigger": { "http_status": 401, "or_expired": "cred.expires_at" }, "token_url": "https://oauth2.googleapis.com/token", "grant": "refresh_token", "response_map": { "access_token": "$.access_token", "expires_in": "$.expires_in" }, "persist": { "access_token": "session", "refresh_token": "credential_if_present" } }`. Actions: `send_email`, `list_messages`, `get_message`. Proves: multi-field credentials + OAuth refresh + rotation persistence, all from data, under the per-binding lock.

> Both ship as bundled manifests (the project can place them in a default `data/adapters/` seed) or are installed by the user. Neither is Python; neither is overwritten on upgrade once in the user's `data/adapters/`.

---

## 8. Test plan

### Unit (`tests/unit/`)
- `test_credential_value.py` — JSON blob → `.fields`; plain string → `.raw` only; bad JSON → empty fields.
- `test_connector_session_service.py` — encrypted round-trip (ciphertext ≠ plaintext in DB); clear; `binding_lock` identity per id.
- `test_connector_errors.py` — codes/retryable/`retry_after`.
- `test_credential_update_value.py` — re-encrypt; reference resolves to new value.
- `test_http_engine.py` — request templating (param→location), auth application, response extraction, the locked-down resolver rejects non-whitelisted paths (security).
- `test_manifest_validation.py` — valid manifest loads; invalid is rejected with a structured error (no raise).

### Integration (`tests/integration/`)
- `test_adapter_discovery.py` — drop a manifest in a temp `data/adapters/`, assert it seeds a connector_type and appears in the catalog; a bad manifest is skipped, others still load.
- `test_connector_session_refresh.py` — **core**: an `http` adapter that 409s once then succeeds; `refresh_session` called once, session cached, retry succeeds.
- `test_connector_refresh_race.py` — two threads, one expiry; `refresh_session` invoked exactly once (double-check), both succeed.
- `test_connector_oauth_rotation.py` — rotated refresh_token persisted via `update_credential_value`; retry uses new fields.
- `test_stateless_no_session_overhead.py` — a stateless connector never reads the session cache / takes the lock (assert via spy).
- `test_connector_actions_schema.py` — `connectors_actions_list` returns per-action `input_schema`.
- `test_transmission_adapter.py` (Phase 2) — mock 409→200; action RPC mapping from the manifest; destructive flag surfaced.
- Regression: existing generic_http/openapi/mcp tests pass unchanged (proves the Credential migration + opt-in session loop are transparent to stateless connectors).

### Gate
`python3 -m pytest tests/ -q` green; `ruff check app/ tests/` clean.

---

## 9. Ordered checklist

**Phase 0**
1. Add nullable `backend_type` column (+`backend_json`) to `connector_types`; infer for existing rows.
2. `_resolve_executor` registry-first + engine binding; remove dead `provider_type=="generic_http"` branches. Full suite green.

**Phase 1**
3. `errors.py`, `base.py` (Credential).
4. `connector_session_service.py`.
5. `credential_service.resolve_credential()` + `update_credential_value()`.
6. Migrate 3 executor call sites to `Credential` + `session=None` (generic_http, openapi_executor, mcp branch).
7. `get_binding_with_credential` → `Credential`; update readers (connector_service.py:488, :734).
8. `http_engine.py` (declarative interpreter + locked-down resolver).
9. `manifest.py` (schema + `load_and_validate`) and `adapter_loader.py` (`data/adapters/` discovery, gating, seeding).
10. `connector_session_cache` table + `_ensure_*`; startup wiring (`discover_and_seed_adapters` after `init_db`).
11. `execute_binding_action` opt-in session loop; extract `_retry_params`; honor `retry_after`.
12. Manifest-actions branch in `generate_connector_type_tools`.
13. `generic_http` as a built-in under the unified seeder; drop from the count-gated `_seed_connector_types`.
14. Unit + integration tests (§8). Full suite + ruff green.

**Phase 2** — `data/adapters/transmission/adapter.json` + tests.
**Phase 3** — `data/adapters/google_gmail/adapter.json` + OAuth tests.
**Phase 4** — `cli`/`mcp` backends; install sources (git → registry) + scan; UI consolidation (one "Add Connector" entry; adapter source in the Directory).

---

## 10. Open decisions (the rest live in plan.md §16)

1. **Manifest format** — JSON (with a published JSON Schema) vs YAML. Lean JSON.
2. **Templating resolver** — confirm the whitelisted `{{ params./cred./config. }}` resolver is sufficient for the request/body templates Phase 1 needs (it is for Transmission/Gmail); extend the whitelist only as concrete adapters require.
3. **Proactive vs reactive refresh** — reactive (401/expiry) only in Phase 1; add `expires_at` lookahead later.
4. **Credential blob vs columns** — multi-field secrets as a JSON blob in `value_encrypted` (no migration). Confirm acceptable.
5. **Session encryption key** — session cache reuses the credential Fernet; confirm that's fine vs a distinct key.

*(Resolved and removed from this list: the multi-process lock question — Agent Core is single-core/single-process by design; the lock is an in-process `threading.Lock` behind a `binding_lock()` seam. See plan.md §8.3.)*
```
