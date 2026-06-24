# Code Review Fixes — remaining

Original review 2026-06-17. Items 1–4 resolved (verified 2026-06-24):
1. `search_memory` double-`offset` — fixed (FTS SQL offset bound to 0; `merged[start:end]` paginates).
2. `_build_request` malformed loop — fixed (`for loc in ("query_params", "header_params")`).
3. Redundant duplicate `refresh_token` `.get` — removed.
4. Bare token wrong-dict fallback — fixed in `_render` (no-key `cred` now resolves via `_cred_get`, not `params`); `_render_value` and `_resolve_token_raw` were already correct.

Remaining (cleanup / structure, do opportunistically):

---

## 5. Triplicated template engine — duplication

**File:** `app/connectors/http_engine.py`

Three near-identical filter blocks re-implement the same `default` / `omit` / `as=` / `rfc822_base64url` logic:
- `_render`'s `replacer` (~line 219)
- `_resolve_token_raw` (~line 671)
- `_render_value` (~line 800)

The credential resolver is already consolidated (`_cred_get` delegates to `_cred_get_impl`), and item 4's fix had to be reasoned across all three copies — exactly the cost of the duplication. Collapse the filter logic to one shared filter-applier.

**Verify:** existing connector tests stay green; add coverage for the native-type (`_resolve_token_raw`) vs stringified (`_render_value`) paths so consolidation can't silently change type coercion.

---

## 6. `dashboard.py` monolith — structure

**File:** `app/routes/dashboard.py` — ~6600 lines.

Maintainability smell. Split by feature area, matching how `connectors_page.py` etc. are already separated. Lower priority; do opportunistically.

---

## Not in scope (reviewed, looked solid)
- `auth_service.py` — bcrypt cost 12, JWT keyring rotation on decode, constant-time OTP.
- `inbound_webhook_service.py` — `secrets.compare_digest` key verify, SHA-256 at rest, workspace scope check.
- No bare `except:`, mutable default args, `shell=True`, `verify=False`, or `md5` in `app/`.
