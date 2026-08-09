"""Check facts against the thing they describe, using their subject anchor.

The point of the anchor is that verification can dispatch on it instead of
guessing from prose. Each anchor type has a verifier that returns one of three
answers, and the difference between them matters more than any of them:

- ``verified``   — checked, and the anchor is there. Records evidence.
- ``missing``    — checked, and the anchor is gone. Evidence the record is stale.
- ``unverifiable`` — no verifier, or the check could not run. Says nothing.

The third is not a failure to report as one. A verifier that treats "I could not
reach the host" as "the record is wrong" would retract true records whenever the
network hiccuped, which is precisely the failure that makes automated cleanup
untrustworthy. Only ``missing`` is evidence, and only about the anchor: a file
that still exists does not make the sentence about it true, so a passing check
refreshes confirmation but never asserts the content is correct.

Verifiers are read-only by construction. Nothing here writes to a repo, a host,
or a service; the strongest thing it does is run a connector binding's own
non-destructive test.
"""

import logging
import os
from typing import Callable, Optional

from app.database import get_db
from app.services import memory_service
from app.time_utils import utc_now_iso

logger = logging.getLogger(__name__)

VERIFIED = "verified"
MISSING = "missing"
UNVERIFIABLE = "unverifiable"


def _workspace_roots() -> dict:
    """scope -> filesystem root, for resolving repo: anchors.

    Configured rather than inferred. A wrong root would resolve every path to
    "missing" and manufacture evidence that a whole workspace's records are
    stale, so an unconfigured scope reports unverifiable instead of guessing at
    a convention.
    """
    import json

    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM system_settings WHERE key = 'workspace_repo_roots'"
            ).fetchone()
        roots = json.loads(row["value"]) if row and row["value"] else {}
        return roots if isinstance(roots, dict) else {}
    except Exception:
        logger.debug("Could not read workspace_repo_roots", exc_info=True)
        return {}


def _verify_repo(value: str, record: dict) -> tuple[str, str]:
    roots = _workspace_roots()
    root = roots.get(record.get("scope") or "")
    if not root:
        return (
            UNVERIFIABLE,
            f"no repository root configured for {record.get('scope')}",
        )
    if not os.path.isdir(root):
        return UNVERIFIABLE, f"configured root {root} is not a directory"

    # Contain the lookup inside the configured root: an anchor is data, and a
    # path that escapes its workspace must not be probed just because someone
    # wrote '../' into a memory record.
    candidate = os.path.normpath(os.path.join(root, value))
    if not candidate.startswith(os.path.realpath(root) + os.sep) and candidate != os.path.realpath(root):
        resolved_root = os.path.realpath(root)
        candidate = os.path.normpath(os.path.join(resolved_root, value.lstrip("/")))
        if not candidate.startswith(resolved_root + os.sep):
            return UNVERIFIABLE, f"anchor path escapes the workspace root: {value}"

    if os.path.exists(candidate):
        return VERIFIED, f"{value} exists under {root}"
    return MISSING, f"{value} no longer exists under {root}"


def _verify_service(value: str, record: dict) -> tuple[str, str]:
    from app.services import connector_service

    binding = connector_service.get_binding(value)
    if not binding:
        return MISSING, f"connector binding {value} no longer exists"
    if not binding.get("enabled"):
        return UNVERIFIABLE, f"binding {binding.get('name')} is disabled"
    try:
        result = connector_service.test_binding(value)
    except Exception as exc:
        return UNVERIFIABLE, f"binding test could not run: {exc}"
    if result.get("ok") or result.get("success"):
        return VERIFIED, f"binding {binding.get('name')} passed its health check"
    # A failing service is a broken service, not a false memory. The record may
    # describe it perfectly well while it happens to be down.
    return UNVERIFIABLE, f"binding {binding.get('name')} is currently failing"


def _bindings_for_host(host: str) -> list[dict]:
    """Bindings whose configured target is this host.

    Going through a binding rather than teaching the verifier to reach hosts
    keeps credentials in the connector layer, which is the only place they are
    scoped, audited and swappable. The verifier gains no new way in.
    """
    import json as _json

    from app.services import connector_service

    matches = []
    for binding in connector_service.list_bindings(enabled=True):
        config = binding.get("config_json") or "{}"
        try:
            parsed = _json.loads(config)
        except (TypeError, ValueError):
            continue
        target = " ".join(
            str(parsed.get(key, "")) for key in ("base_url", "url", "host", "endpoint")
        )
        if host and host in target:
            matches.append(binding)
    return matches


def _verify_host(value: str, record: dict) -> tuple[str, str]:
    # Never MISSING. A host that does not answer is a host that does not answer;
    # it is not proof that a memory about it is false, and treating it as such
    # would turn every outage into a wave of bogus staleness evidence.
    from app.services import connector_service

    matches = _bindings_for_host(value)
    if not matches:
        return UNVERIFIABLE, f"no connector binding targets {value}"
    if len(matches) > 1:
        names = ", ".join(sorted(b["name"] for b in matches)[:3])
        return UNVERIFIABLE, f"several bindings target {value} ({names}); ambiguous"

    binding = matches[0]
    try:
        result = connector_service.test_binding(binding["id"])
    except Exception as exc:
        return UNVERIFIABLE, f"binding test could not run: {exc}"
    if result.get("success") or result.get("ok"):
        return VERIFIED, f"{binding['name']} reached {value} successfully"
    return UNVERIFIABLE, f"{binding['name']} could not reach {value} right now"


BUILTIN_VERIFIERS: dict[str, Callable[[str, dict], tuple[str, str]]] = {
    "repo": _verify_repo,
    "service": _verify_service,
    "host": _verify_host,
}


def _configured_verifiers() -> dict:
    """Anchor types an installation has taught this system to check.

    Verification is a capability, and capabilities here are connectors. Rather
    than this module enumerating the kinds of thing a memory can be about — a
    list that would only ever describe whoever wrote it — an installation maps
    an anchor type to a binding it already has:

        {"url": {"binding_id": "...", "action": "GET /scrape"}}

    A record about a policy document, a web page or a customer is then checkable
    by the same machinery that checks a file, without this file knowing what any
    of those are.
    """
    import json

    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM system_settings WHERE key = 'verification_bindings'"
            ).fetchone()
        configured = json.loads(row["value"]) if row and row["value"] else {}
        return configured if isinstance(configured, dict) else {}
    except Exception:
        logger.debug("Could not read verification_bindings", exc_info=True)
        return {}


def available_anchor_types() -> set:
    """Anchor types something can currently check."""
    return set(BUILTIN_VERIFIERS) | set(_configured_verifiers())


def _verify_via_binding(spec: dict, anchor_type: str, value: str) -> tuple[str, str]:
    """Check an anchor by calling the binding an operator mapped to its type.

    A binding that answers successfully verifies the anchor; one that errors
    leaves the record unverifiable rather than condemning it, for the same
    reason an unreachable host does. Only an explicit not-found is evidence, and
    only when the binding says so plainly.
    """
    from app.services import connector_service
    from app.security.effective_authority import system_authority

    binding_id = spec.get("binding_id")
    action = spec.get("action")
    if not binding_id or not action:
        return UNVERIFIABLE, f"the verifier for '{anchor_type}:' is incompletely configured"

    params = dict(spec.get("params") or {})
    params[spec.get("value_param", "target")] = value
    try:
        result = connector_service.execute_authorized_binding_action_with_logging(
            binding_id, action, params,
            system_authority("configured anchored-fact verification"),
        )
    except Exception as exc:
        return UNVERIFIABLE, f"the '{anchor_type}:' verifier could not run: {exc}"

    if result.get("success") or result.get("ok"):
        return VERIFIED, f"the '{anchor_type}:' verifier reached {value}"

    error = str(result.get("error") or "").lower()
    status = result.get("status_code") or result.get("status")
    if status in (404, 410) or "not found" in error or "no such" in error:
        return MISSING, f"the '{anchor_type}:' verifier reported {value} no longer exists"
    return UNVERIFIABLE, f"the '{anchor_type}:' verifier could not confirm {value} right now"


def verify_record(record: dict, verified_by: str = "verification-pass") -> dict:
    """Check one record against its anchor. Writes only on a clean pass."""
    anchor = record.get("subject_anchor")
    outcome = {
        "record_id": record.get("id"),
        "scope": record.get("scope"),
        "topic": record.get("topic"),
        "anchor": anchor,
    }
    if not anchor:
        return {**outcome, "status": UNVERIFIABLE, "detail": "no subject anchor"}
    if record.get("memory_class") != "fact":
        # Only facts make claims the world can contradict.
        return {**outcome, "status": UNVERIFIABLE, "detail": "not a fact"}

    anchor_type, _, value = anchor.partition(":")
    verifier = BUILTIN_VERIFIERS.get(anchor_type)
    configured = None if verifier else _configured_verifiers().get(anchor_type)
    if not verifier and not configured:
        return {
            **outcome,
            "status": UNVERIFIABLE,
            "detail": f"no verifier for {anchor_type}: anchors",
        }

    try:
        if verifier:
            status, detail = verifier(value, record)
        else:
            status, detail = _verify_via_binding(configured, anchor_type, value)
    except Exception as exc:
        logger.debug("Verifier for %s failed", anchor, exc_info=True)
        return {**outcome, "status": UNVERIFIABLE, "detail": f"check failed: {exc}"}

    if status == VERIFIED:
        try:
            memory_service.confirm_memory(
                record["id"],
                evidence=f"anchor check: {detail}",
                verified_by=verified_by,
            )
        except ValueError:
            logger.debug("Could not confirm %s", record.get("id"), exc_info=True)

    return {**outcome, "status": status, "detail": detail}


def _note_attempts(record_ids: list[str]) -> None:
    """Record that these were looked at, whatever the verdict.

    This is what makes a capped sweep fair. Ordering by last_confirmed_at meant
    the records that can never be confirmed — a host with no binding, a scope
    with no configured root — sorted to the front on every run and consumed the
    entire budget, so records that could be re-checked never came up again.
    """
    if not record_ids:
        return
    placeholders = ",".join("?" for _ in record_ids)
    try:
        with get_db() as conn:
            conn.execute(
                f"UPDATE memory_records SET last_verify_attempt_at = ? "
                f"WHERE id IN ({placeholders})",
                [utc_now_iso(), *record_ids],
            )
            conn.commit()
    except Exception:
        logger.debug("Could not record verification attempts", exc_info=True)


def verify_scope(scope: Optional[str] = None, limit: int = 200) -> dict:
    """Verify anchored facts, optionally within one scope.

    Works through the corpus least-recently-attempted first, so a capped nightly
    run rotates across everything rather than re-checking the same head of the
    queue forever.

    Missing anchors become proposals rather than retractions. The file being
    gone is strong evidence and weak proof: code moves, and a record about a
    renamed module is often still true about the system. A person decides;
    the pass supplies the evidence.
    """
    from app.services import memory_proposal_service

    conditions = ["record_status = 'active'", "memory_class = 'fact'", "subject_anchor IS NOT NULL"]
    params: list = []
    if scope:
        conditions.append("scope = ?")
        params.append(scope)
    params.append(max(limit, 0))

    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {memory_service.MEMORY_RECORD_COLUMNS} FROM memory_records "
            f"WHERE {' AND '.join(conditions)} "
            f"ORDER BY last_verify_attempt_at IS NOT NULL, last_verify_attempt_at LIMIT ?",
            params,
        ).fetchall()

    results = [verify_record(dict(row)) for row in rows]
    _note_attempts([r["record_id"] for r in results])
    tally = {VERIFIED: 0, MISSING: 0, UNVERIFIABLE: 0}
    for result in results:
        tally[result["status"]] = tally.get(result["status"], 0) + 1

    queued = 0
    for result in results:
        if result["status"] != MISSING:
            continue
        if memory_proposal_service.queue_proposal(
            rule="anchor_missing",
            action="confirm",
            scope=result["scope"],
            target_ids=[result["record_id"]],
            evidence={
                "anchor": result["anchor"],
                "detail": result["detail"],
                "looks_like_runtime_state": memory_service.anchor_looks_like_runtime_state(
                    result["anchor"]
                ),
            },
        ):
            queued += 1

    return {
        "checked": len(results),
        "verified": tally[VERIFIED],
        "missing": tally[MISSING],
        "unverifiable": tally[UNVERIFIABLE],
        "proposals_queued": queued,
        "results": results,
    }
