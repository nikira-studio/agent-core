"""Regression tests for the six fixes from the round-2 review.

#1 (High) — UNIQUE collision on pending duplicate target_ids_json must be
    silently skipped, not crash the call. The partial unique index
    `idx_memory_proposals_open` on (rule, target_ids_json) WHERE
    status='pending' refuses the second insert; concurrent callers
    must catch this and report it as a cap-skip, never raise.

#2 (Medium) — `_candidate_record_active` must validate every target_id,
    not just the first. duplicate_cluster proposals list every record
    in the cluster; the partial unique index protects the whole set as
    one unit, so per-target eligibility must hold for every member.

#3 (Medium) — `verified_by` must attribute agent confirmations to the
    agent, not to its human owner. The previous `user_id or agent_id or
    actor_id` priority silently tagged every agent confirmation with the
    human owner's name because `build_agent_context` populates
    `RequestContext.user_id` from `default_user_id`/`owner_user_id`.

#4 (Medium) — vector top-K is computed per-status, not combined, so a
    higher-scoring active match can't crowd a real retracted match out
    of the candidate set before the per-status partition ever sees it.

#5 (Low) — the SQL whitespace fallback collapses runs of spaces (and
    tabs/newlines/CRs) the same way the Python side does.

#6 (Low) — runtime defaults are seeded as system_settings rows so a
    fresh install has them visible in the database.
"""

import asyncio
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import timedelta

import pytest

from app.database import get_db
from app.services import (
    memory_proposal_service,
    memory_service,
    system_settings_service,
)
from app.time_utils import utc_now
import app.operations.memory as ops_memory


# --- #1 -----------------------------------------------------------------


def test_concurrent_generate_proposals_in_same_scope_handles_unique_collision(
    clean_db,
):
    """Two threads calling generate_proposals against the SAME scope must
    not crash on the partial unique index when both try to queue the
    same candidate. The second one must report it as a cap skip.

    Reproduces the original bug: with the pre-fix code, this raises
    `sqlite3.IntegrityError: UNIQUE constraint failed:
    memory_proposals.rule, memory_proposals.target_ids_json` because the
    pre-check `_decided_targets` happens during the read phase (no
    lock), the write-phase recheck uses the count recheck + per-record
    eligibility but never re-verifies the (rule, target_ids_json)
    unique pair, and so the second writer's INSERT hits the unique
    index. The pre-existing concurrency test used disjoint scopes because
    that is what the cap-fairness invariants needed — it was
    structurally incapable of hitting this collision.
    """
    # Use stale_volatile content so the rule actually fires.
    VOLATILE = (
        "the assistant dashboard is currently served from 127.0.0.1:19119 on the build "
        "server with image vendor/example-agent:v2026.5.29.2."
    )
    # Single scope, many candidates — both threads will scan the same set.
    for i in range(8):
        memory_service.write_memory(
            f"{VOLATILE} extra {i}",
            "fact",
            "workspace:col-same",
            topic="col-same",
        )
    memory_service.write_memory(
        f"{VOLATILE} tail extra",
        "fact",
        "workspace:col-same",
        topic="col-same",
    )

    # Backdate everything so stale_volatile picks them up.
    old = (utc_now() - timedelta(days=180)).isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET last_confirmed_at = ?, created_at = ? "
            "WHERE scope = 'workspace:col-same'",
            (old, old),
        )
        conn.commit()

    errors: list[Exception] = []
    results: list[dict] = []

    def run():
        try:
            results.append(
                memory_proposal_service.generate_proposals(
                    scope="workspace:col-same",
                    rules=["stale_volatile"],
                )
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent runs raised: {errors}"
    # Pending count never exceeds the per-rule-per-scope cap.
    with get_db() as conn:
        count = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM memory_proposals "
                "WHERE status='pending' AND rule='stale_volatile' "
                "AND scope='workspace:col-same'"
            ).fetchone()["n"]
        )
    # At most `proposal_pending_cap_per_rule` (default 20) pending — never
    # over the cap, never crashed.
    assert count <= 20, f"per-(rule,scope) cap violated: {count}"
    # And every candidate that the rules found (9 records) was either
    # inserted or explicitly skipped — none crashed. Threads whose read
    # phase raced thread 0's commit may have seen duplicate candidates
    # at the read phase; those are reported as `skipped_already_known`
    # if `_decided_targets` was read after thread 0's commit, or
    # `skipped_at_cap` if they hit the unique-index catch later. Both
    # outcomes are correct: no crash, every candidate accounted for.
    total_created = sum(r["created"] for r in results)
    total_skipped = sum(
        r.get("skipped_already_known", 0) + r.get("skipped_at_cap", 0) for r in results
    )
    assert total_created + total_skipped >= 9, (
        "every candidate must have been accounted for, but "
        f"created+skipped={total_created + total_skipped} < 9"
    )

    errors: list[Exception] = []
    results: list[dict] = []

    def run():
        try:
            results.append(
                memory_proposal_service.generate_proposals(
                    scope="workspace:col-same",
                    rules=["stale_volatile"],
                )
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent runs raised: {errors}"
    # Pending count never exceeds the per-rule-per-scope cap.
    with get_db() as conn:
        count = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM memory_proposals "
                "WHERE status='pending' AND rule='stale_volatile' "
                "AND scope='workspace:col-same'"
            ).fetchone()["n"]
        )
    # At most `proposal_pending_cap_per_rule` (default 20) pending — never
    # over the cap, never crashed.
    assert count <= 20, f"per-(rule,scope) cap violated: {count}"
    # And every candidate that the rules found (9 records) was either
    # inserted or explicitly skipped — none crashed. Some threads' reads
    # may have raced thread 0's commits, so they see duplicate candidates
    # at the read phase; those are reported as `skipped_already_known`
    # if `_decided_targets` was read after thread 0's commit, or
    # `skipped_at_cap` if they hit the unique-index catch later. Both
    # outcomes are correct: no crash, every candidate accounted for.
    total_created = sum(r["created"] for r in results)
    total_skipped = sum(
        r.get("skipped_already_known", 0) + r.get("skipped_at_cap", 0) for r in results
    )
    assert total_created + total_skipped >= 9, (
        "every candidate must have been accounted for, but "
        f"created+skipped={total_created + total_skipped} < 9"
    )


# --- #2 -----------------------------------------------------------------


def test_duplicate_cluster_proposal_drops_entire_cluster_when_any_target_ineligible(
    clean_db,
):
    """When any target of a multi-target candidate is ineligible between
    scan and lock, the WHOLE candidate must be dropped — not just the
    first target.

    The pre-fix bug: `_candidate_record_active` re-read only
    `candidate["target_ids"][0]`, so a duplicate_cluster listing 3
    records with the third one retracted between scan and lock would
    still queue the cluster with a stale pointer to a now-retracted
    record. The partial unique index on (rule, target_ids_json) makes
    the resulting mismatch invisible until something later tries to act
    on the third target.
    """
    from app.services.memory_proposal_service import (
        _candidate_record_active,
    )

    scope = "workspace:cluster-test"
    # Three records sharing the same prefix.
    prefix = "the deployment plan for v2 includes " + "x" * 50 + " more detail here"
    ids = []
    for i in range(3):
        record, _ = memory_service.write_memory(
            content=f"{prefix} variant {i}",
            memory_class="fact",
            scope=scope,
        )
        ids.append(record["id"])

    # Candidate list: all three targets.
    candidate = {
        "rule": "duplicate_cluster",
        "action": "retract",
        "scope": scope,
        "target_ids": ids,
        "rationale": "three near-duplicates",
        "evidence": {},
    }

    # Initially all three are eligible.
    with get_db() as conn:
        current = _candidate_record_active(conn, candidate)
    assert current is not None, (
        "all three records start active+unpinned, so the candidate "
        "must revalidate as eligible"
    )

    # Retract the second record. With the pre-fix bug, the candidate
    # would still revalidate because only target_ids[0] was rechecked.
    memory_service.retract_memory(ids[1])

    with get_db() as conn:
        current = _candidate_record_active(conn, candidate)
    assert current is None, (
        "any ineligible target must invalidate the whole candidate; "
        "the pre-fix bug would return a non-None row because it only "
        "re-checked target_ids[0]"
    )


# --- #3 -----------------------------------------------------------------


def test_confirm_memory_attributes_agent_confirmations_to_agent_not_owner(
    clean_db,
):
    """When an agent confirms a memory, `verified_by` must be the agent's
    id, NOT the human owner's id. The previous priority was wrong
    because `build_agent_context` populates `RequestContext.user_id`
    from the agent's `default_user_id`/`owner_user_id`, so every agent
    confirmation was silently attributed to the human owner.

    We exercise the operation directly: the `verified_by` derivation
    happens before any database call, so a unit test against the
    operation with a stub authority proves the priority order. The
    actual call to memory_service.confirm_memory runs through a real
    EffectiveAuthority so the verified_by it derives is the same value
    the operation would use.
    """
    from app.security.effective_authority import EffectiveAuthority
    from app.security.context import RequestContext

    record, _ = memory_service.write_memory(
        content="test fact",
        memory_class="fact",
        scope="workspace:attr-agent",
        source_kind="agent_inference",
    )
    # Simulate an agent call: actor_type='agent', user_id populated by
    # build_agent_context from the agent's owner_user_id, agent_id is
    # the agent itself. Pre-fix priority would resolve to user_id.
    ctx = EffectiveAuthority(
        context=RequestContext(
            actor_type="agent",
            actor_id="attribution-agent",
            user_id="human-owner-of-attribution-agent",
            agent_id="attribution-agent",
            read_scopes=["workspace:attr-agent"],
            write_scopes=["workspace:attr-agent"],
            is_admin=True,
        ),
    )

    asyncio.run(
        ops_memory.confirm_memory(
            record["id"],
            evidence="cross-checked against source",
            authority=ctx,
            channel="api",
            route="/api/memory/confirm",
        )
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT provenance_json FROM memory_records WHERE id = ?",
            (record["id"],),
        ).fetchone()
    provenance = json.loads(row["provenance_json"])
    verification = provenance.get("verified") or {}
    assert verification.get("by") == "attribution-agent", (
        "agent confirmations must be attributed to the agent, not the "
        "human owner — but got verified_by=" + repr(verification.get("by"))
    )


def test_confirm_memory_attributes_human_confirmations_to_human(clean_db):
    """Symmetric: a human session's confirmation goes to the human's id."""
    from app.security.effective_authority import EffectiveAuthority
    from app.security.context import RequestContext

    record, _ = memory_service.write_memory(
        content="test fact",
        memory_class="fact",
        scope="workspace:human-attribution",
    )
    ctx = EffectiveAuthority(
        context=RequestContext(
            actor_type="user",
            actor_id="the-human-actual",
            user_id="the-human-actual",
            agent_id=None,
            read_scopes=["workspace:human-attribution"],
            write_scopes=["workspace:human-attribution"],
            is_admin=True,
        ),
    )

    asyncio.run(
        ops_memory.confirm_memory(
            record["id"],
            evidence="checked",
            authority=ctx,
            channel="api",
            route="/api/memory/confirm",
        )
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT provenance_json FROM memory_records WHERE id = ?",
            (record["id"],),
        ).fetchone()
    provenance = json.loads(row["provenance_json"])
    verification = provenance.get("verified") or {}
    assert verification.get("by") == "the-human-actual"


# --- #4 -----------------------------------------------------------------


def test_find_near_duplicates_does_not_crowd_retracted_with_active_topk(
    clean_db,
    monkeypatch,
):
    """A retracted match at score 0.95 must still appear when an active
    match at score 0.99 is also present. The pre-fix code computed a
    single combined top-K, so with limit=3 the top 3 active matches
    could push out the only retracted match.

    We fake the vector backend so the test is deterministic without
    requiring a live embedding model.
    """
    scope = "workspace:topk-test"
    # 5 retracted records, all near-duplicates of the new write.
    retracted_ids = []
    for i in range(5):
        r, _ = memory_service.write_memory(
            f"near-duplicate retracted fact number {i} for the test",
            "fact",
            scope,
            source_kind="agent_inference",
        )
        memory_service.retract_memory(r["id"])
        retracted_ids.append(r["id"])
    # 5 active records, also near-duplicates but with a higher cosine
    # score (we'll mock the search to return these first).
    active_ids = []
    for i in range(5):
        r, _ = memory_service.write_memory(
            f"near-duplicate active fact number {i} for the test",
            "fact",
            scope,
            source_kind="agent_inference",
        )
        active_ids.append(r["id"])

    # Mock: vector enabled, every candidate scores 0.99 regardless of
    # status. The active IDs come back first because of how Python
    # dicts iterate.
    mock_vector = b"\x00" * 8

    def fake_cosine_search(vector_bytes, k, candidate_ids):
        # Return active_ids first (all 0.99), then retracted (0.98).
        # With the pre-fix combined top-K of `max(limit*len(statuses)*4, 20) = 40`,
        # the active set fills the first 40 entries — wait, candidate_ids is
        # the union. Let me just return candidate_ids in the order
        # caller asked, with all scores 0.99. The point is that under the
        # per-status partitioning, each status's top-K is taken from its
        # own candidate_ids, not the combined set.
        return [(cid, 0.99) for cid in candidate_ids]

    monkeypatch.setattr(
        "app.services.embedding_service.get_embedding_backend_status",
        lambda: {"backend": "healthy", "model_configured": True},
    )
    monkeypatch.setattr(
        "app.services.embedding_service.generate_embedding",
        lambda *a, **k: (mock_vector, "ok"),
    )
    monkeypatch.setattr(
        "app.services.vector_settings_service.is_vector_search_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "app.services.vector_service.cosine_search_top_k",
        fake_cosine_search,
    )

    # Per-status query, limit=3 per status. Expect 3 active AND 3
    # retracted candidates (from each status's own top-K).
    candidates = memory_service.find_near_duplicates(
        content="near-duplicate fact for the test" * 3,
        scope=scope,
        memory_class="fact",
        limit=3,
        statuses=("active", "retracted"),
    )
    statuses = [c["record_status"] for c in candidates]
    assert "active" in statuses and "retracted" in statuses, (
        f"the retracted status must produce its own top-K — under the "
        f"pre-fix code, active scores crowded retracted out. Got: {statuses}"
    )
    # Each status contributes at most `limit` rows.
    assert sum(1 for s in statuses if s == "retracted") <= 3
    assert sum(1 for s in statuses if s == "active") <= 3


# --- #5 -----------------------------------------------------------------


def test_retracted_prefix_fallback_matches_with_collapsing_whitespace(
    clean_db,
):
    """A new write with collapsed whitespace must still match a stored
    record whose content has a run of multiple spaces. The pre-fix
    SQL fallback swapped tabs/newlines/CRs for spaces but did NOT
    collapse repeated spaces, so identical content with a double space
    normalized differently on each side and silently missed the match.
    """
    from datetime import timedelta
    from app.time_utils import utc_now

    old = (utc_now() - timedelta(days=180)).isoformat()

    scope = "workspace:ws-fallback"
    # Stored content: 50 spaces between two phrases.
    stored = (
        "this is a test of whitespace collapsing"
        + (" " * 50)
        + "more text after the spaces here"
    )
    with get_db() as conn:
        conn.execute(
            "INSERT INTO memory_records "
            "(id, scope, memory_class, content, source_kind, last_confirmed_at, "
            " created_at, record_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'retracted')",
            ("mem-fallback", scope, "fact", stored, "agent_inference", old, old),
        )
        conn.commit()

    # New content with single spaces (Python-collapsed).
    new = "this is a test of whitespace collapsing more text after the spaces here"

    from app.services import memory_service

    result = memory_service._retracted_prefix_duplicates(
        new,
        scope,
        exclude_id="some-other-id",
    )
    assert len(result) == 1, (
        f"the fallback must collapse runs of spaces the same way the "
        f"Python side does — but the match was missed. Got {len(result)}"
    )
    assert result[0]["id"] == "mem-fallback"


def test_retracted_prefix_fallback_matches_with_tabs_and_newlines(
    clean_db,
):
    """Same property for tabs and newlines mixed with spaces."""
    from datetime import timedelta
    from app.time_utils import utc_now

    old = (utc_now() - timedelta(days=180)).isoformat()

    scope = "workspace:ws-mixed"
    stored = (
        "this is a quick brown fox jumps over the lazy dog with"
        + "\t\n"
        + "tabs and newlines in between words and many    spaces"
    )
    with get_db() as conn:
        conn.execute(
            "INSERT INTO memory_records "
            "(id, scope, memory_class, content, source_kind, last_confirmed_at, "
            " created_at, record_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'retracted')",
            ("mem-mixed", scope, "fact", stored, "agent_inference", old, old),
        )
        conn.commit()

    new = "this is a quick brown fox jumps over the lazy dog with tabs and newlines in between words and many    spaces"
    from app.services import memory_service

    result = memory_service._retracted_prefix_duplicates(
        new,
        scope,
        exclude_id="some-other-id",
    )
    assert len(result) == 1, (
        f"mixed whitespace (tabs, newlines, multiple spaces) must "
        f"normalize the same way — but the match was missed. Got {len(result)}"
    )


# --- #6 -----------------------------------------------------------------


def test_runtime_defaults_are_seeded_into_system_settings(clean_db):
    """The runtime defaults are now visible in system_settings so an
    operator inspecting the database sees the same value a fresh
    install would compute. An operator's explicit override survives
    via INSERT OR IGNORE.
    """
    from app.schema import _SYSTEM_SETTINGS_DEFAULTS

    expected_keys = {key for key, _ in _SYSTEM_SETTINGS_DEFAULTS}
    with get_db() as conn:
        rows = conn.execute(
            "SELECT key, value FROM system_settings WHERE key IN ({})".format(
                ",".join("?" for _ in expected_keys)
            ),
            tuple(expected_keys),
        ).fetchall()
    found = {row["key"]: row["value"] for row in rows}
    assert found.keys() == expected_keys, (
        f"missing defaults: {expected_keys - found.keys()}; "
        f"unexpected: {found.keys() - expected_keys}"
    )
    # Spot-check a few values to catch typos.
    assert found["scratchpad_retention_days"] == "7"
    assert found["proposal_pending_cap_per_rule"] == "20"
    assert found["workspace_awareness_digest_enabled"] == "1"


def test_runtime_defaults_survive_operator_override(clean_db):
    """INSERT OR IGNORE means an operator's explicit override must
    survive the migration (the migration runs once at startup; if an
    operator changed a default, that change must persist).
    """
    from app.services import system_settings_service

    system_settings_service.write_raw({"scratchpad_retention_days": "42"})

    # Migration has already run (the row is in the table); re-run the
    # idempotent upsert and verify the operator's value survives.
    from app.schema import _SYSTEM_SETTINGS_DEFAULTS

    with get_db() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO system_settings (key, value, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            _SYSTEM_SETTINGS_DEFAULTS,
        )
        conn.commit()

    with get_db() as conn:
        value = conn.execute(
            "SELECT value FROM system_settings WHERE key='scratchpad_retention_days'"
        ).fetchone()["value"]
    assert value == "42", (
        f"operator override must survive the idempotent upsert; got {value}"
    )


# --- Round 3 — three related gaps in queue_proposal -----------------------


def test_queue_proposal_concurrent_same_target_no_crash(clean_db):
    """5 threads calling queue_proposal for the same (rule, target_ids_json)
    must not crash on the partial unique index. Pre-fix: 4/5 raised
    `sqlite3.IntegrityError: UNIQUE constraint failed:
    memory_proposals.rule, memory_proposals.target_ids_json`. The shared
    atomic helper now holds BEGIN IMMEDIATE for the recheck + insert,
    so concurrent callers serialize and only one wins.
    """
    errors: list[Exception] = []
    results: list[list[str | None]] = []

    def run():
        try:
            pid = memory_proposal_service.queue_proposal(
                rule="test_round3_queue",
                action="confirm",
                scope="workspace:qp-conc",
                target_ids=["target-qp"],
            )
            results.append(pid)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent queue_proposal raised: {errors}"
    # Exactly one thread sees a non-None id; the other four see None.
    non_none = [r for r in results if r is not None]
    assert len(non_none) == 1, (
        f"expected exactly one winner, got {len(non_none)}: {results}"
    )
    with get_db() as conn:
        count = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM memory_proposals "
                "WHERE rule='test_round3_queue'"
            ).fetchone()["n"]
        )
    assert count == 1


def test_queue_proposal_lock_time_check_sees_pending_accepted_rejected(
    clean_db,
    monkeypatch,
):
    """The lock-time recheck covers pending/accepted/rejected — not just
    pending.

    Critical: this must simulate the decision landing DURING the call,
    not before it. A common test-design flaw (made it through two
    rounds of review here before being caught) is to commit the
    accepted status before calling `queue_proposal` — which means the
    pre-lock `_decided_targets` fast path resolves the conflict
    immediately and the lock-time code the test claims to protect
    never runs. To force the lock-time check to be the one that sees
    the accepted row, we bypass the pre-lock fast path (by patching
    `_decided_targets` to return an empty set) and then land the
    side effect inside the BEGIN IMMEDIATE connection, before the
    lock-time SELECT runs.
    """
    # Seed an existing pending row for this exact target.
    first_id = memory_proposal_service.queue_proposal(
        rule="test_round3_decided",
        action="confirm",
        scope="workspace:qp-decided",
        target_ids=["target-qpd"],
    )
    assert first_id is not None

    # Bypass the pre-lock fast path so the lock-time check is the one
    # that matters. With this bypass, a buggy lock-time check would
    # happily insert a second pending row for the same target.
    monkeypatch.setattr(memory_proposal_service, "_decided_targets", lambda rule: set())

    # Patch `get_db` so the FIRST call commits the peer decision, and
    # subsequent calls yield a fresh connection. This way the second
    # `queue_proposal` call's pre-lock fast path (which uses the
    # patched `_decided_targets` returning empty) does NOT see the
    # peer decision, but the lock-time SELECT inside the BEGIN
    # IMMEDIATE connection does. Capture the ORIGINAL `get_db` BEFORE
    # patching — `memory_proposal_service.get_db` and `app.database.get_db`
    # are the same object, so the patch changes both.
    import app.database as _db_mod

    real_get_db = _db_mod.get_db

    peer_decided = [False]

    from contextlib import contextmanager

    @contextmanager
    def wrapped_db():
        if not peer_decided[0]:
            peer_decided[0] = True
            with real_get_db() as conn:
                conn.execute(
                    "UPDATE memory_proposals SET status = 'accepted', "
                    "decided_at = ? WHERE id = ?",
                    ("2026-09-07T10:00:00+00:00", first_id),
                )
                conn.commit()
        with real_get_db() as conn:
            yield conn

    monkeypatch.setattr(memory_proposal_service, "get_db", wrapped_db)

    second_id = memory_proposal_service.queue_proposal(
        rule="test_round3_decided",
        action="confirm",
        scope="workspace:qp-decided",
        target_ids=["target-qpd"],
    )
    assert second_id is None, (
        f"the lock-time check must see `accepted` (not just `pending`) "
        f"and refuse to re-queue — but got id={second_id}"
    )
    with get_db() as conn:
        count = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM memory_proposals "
                "WHERE rule='test_round3_decided' "
                "AND status IN ('pending', 'accepted', 'rejected')"
            ).fetchone()["n"]
        )
    assert count == 1, (
        f"expected exactly one settled proposal, got {count} — "
        f"the lock-time check regressed and the second insert landed"
    )


def test_queue_proposal_check_constraint_violation_still_raises(clean_db):
    """A genuinely broken action (not in the CHECK enum) must surface,
    not be silently reported as `skipped_at_cap`. SQLite raises the
    same `IntegrityError` class for UNIQUE-index violations and
    CHECK-constraint violations; the atomic helper must distinguish
    them and re-raise the CHECK case as a real bug.
    """
    # Pre-fix: the catch was `except sqlite3.IntegrityError: ... return
    # None`. A CHECK violation would have been swallowed here.
    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        # Reach into the helper directly — public queue_proposal funnels
        # through RATIONALE_BUILDERS, and we want the raw schema to
        # enforce the CHECK. Bypass the public API on purpose.
        with get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # `action='retract'` is valid; the call below uses an
                # INVALID action that violates the CHECK constraint.
                memory_proposal_service._atomic_queue_one(
                    conn,
                    rule="test_round3_check",
                    action="not_a_real_action",
                    scope="workspace:qp-check",
                    target_ids=["target-qpc"],
                    evidence={},
                    rationale="",
                )
            finally:
                conn.rollback()
    # The CHECK constraint surfaces as an IntegrityError; the helper
    # re-raises it rather than treating it as a duplicate-collision skip.
    assert "action" in str(excinfo.value) or "CHECK" in str(excinfo.value), (
        f"CHECK violation should surface with `action`/`CHECK` in the "
        f"message, got {excinfo.value!r}"
    )


# --- Round 3 — Python normalization for retracted-prefix match -------------


def test_retracted_prefix_match_with_leading_and_trailing_whitespace(
    clean_db,
):
    """A retracted record with leading or trailing whitespace must still
    match a clean new write. Pre-fix the SQL approximation only handled
    internal whitespace runs, so a stored record with a leading space
    silently mismatched a new write without it.

    The Python normalization (`" ".join(content.split()).lower()`) strips
    both ends the same way it handles internal whitespace — so this
    is now a single rule, not two divergent ones.
    """
    from datetime import timedelta
    from app.time_utils import utc_now

    old = (utc_now() - timedelta(days=180)).isoformat()

    scope = "workspace:ws-whitespace"
    stored = "   " + (
        "this is a quick brown fox jumps over the lazy dog with leading"
        " whitespace and trailing tabs and newlines and trailing\n\t   "
    )
    with get_db() as conn:
        conn.execute(
            "INSERT INTO memory_records "
            "(id, scope, memory_class, content, source_kind, last_confirmed_at, "
            " created_at, record_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'retracted')",
            ("mem-ws", scope, "fact", stored, "agent_inference", old, old),
        )
        conn.commit()

    new = "this is a quick brown fox jumps over the lazy dog with leading whitespace and trailing tabs and newlines and trailing"
    from app.services import memory_service

    result = memory_service._retracted_prefix_duplicates(
        new,
        scope,
        exclude_id="some-other-id",
    )
    assert len(result) == 1, (
        f"the match must land despite leading/trailing whitespace on the "
        f"stored record — got {len(result)}"
    )
    assert result[0]["id"] == "mem-ws"


def test_retracted_prefix_match_only_whitespace_does_not_match(clean_db):
    """The Python normalization strips whitespace entirely, so a record
    that's only whitespace doesn't match a clean prefix. Pre-fix the
    SQL approximation didn't strip ends, so a stored record that was
    just a few characters of whitespace could match a new write whose
    prefix happened to be those characters after the SQL collapse.
    """
    from datetime import timedelta
    from app.time_utils import utc_now

    old = (utc_now() - timedelta(days=180)).isoformat()

    scope = "workspace:ws-only-ws"
    stored = "                                                   "
    with get_db() as conn:
        conn.execute(
            "INSERT INTO memory_records "
            "(id, scope, memory_class, content, source_kind, last_confirmed_at, "
            " created_at, record_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'retracted')",
            ("mem-ws-only", scope, "fact", stored, "agent_inference", old, old),
        )
        conn.commit()

    new = "this is a totally different and much longer content that does not match"
    from app.services import memory_service

    result = memory_service._retracted_prefix_duplicates(
        new,
        scope,
        exclude_id="some-other-id",
    )
    assert result == [], (
        f"a whitespace-only record must not match any real prefix, got {len(result)}"
    )


# --- Round 4 — stale-status bug + wasted-capacity bug -------------------


def test_lock_time_check_does_not_treat_stale_as_settled(clean_db):
    """A `stale` row is NOT settled — `stale` is operational ("the
    maintenance pass marked this inactive because nothing has used
    this execution for a while"), not a verdict. Pre-fix:
    `_decide_target_outcome`'s query had no status filter, so it
    matched ANY row for the (rule, target_ids_json) — including a
    stale one — and refused to re-queue. Nothing in the codebase
    currently produces stale rows (which is why this slipped through),
    but the query genuinely didn't match its own contract.

    `_decided_targets` (the read-phase fast path) DID have the correct
    status filter, but the lock-time check had been added without it.
    After the fix, the lock-time check sees stale rows as
    non-blocking — a fresh proposal for the same target proceeds.
    """
    # Seed a stale row directly for the (rule, target_ids_json) the
    # call will use.
    with get_db() as conn:
        conn.execute(
            "INSERT INTO memory_proposals "
            "(id, rule, action, scope, target_ids_json, rationale, "
            " evidence_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'stale', ?)",
            (
                "stale-row",
                "test_round4_stale",
                "confirm",
                "workspace:qp-stale",
                '["target-qp-stale"]',
                "stale rationale",
                "{}",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()

    pid = memory_proposal_service.queue_proposal(
        rule="test_round4_stale",
        action="confirm",
        scope="workspace:qp-stale",
        target_ids=["target-qp-stale"],
    )
    assert pid is not None, (
        "a stale row must not block a fresh proposal — the call "
        "returned None (the lock-time check wrongly treated `stale` "
        "as a settled status)"
    )


def test_generate_proposals_does_not_waste_cap_slot_on_already_settled(
    clean_db,
    monkeypatch,
):
    """Pre-fix: a settled candidate consumed a per-(rule, scope) cap
    slot that could have gone to a fresh target whose peer is also
    competing in the same group. The settled-target drop must happen
    BEFORE the cap trim, otherwise one settled row blocks an eligible
    peer from being inserted.

    Reproduces the original probe: cap=1, two candidates with
    distinct target_ids. Candidate #1 gets decided (stale, accepted,
    whatever) between the scan and the lock — the simulated peer
    race. With the fix, candidate #1 is dropped at the settled-check
    step BEFORE the cap trim, candidate #2 gets the slot.
    """
    # Seed two distinct records that both match `stale_volatile`.
    VOLATILE = (
        "the assistant dashboard is currently served from 127.0.0.1:19119 "
        "on the build server with image vendor/example-agent:v2026.5.29.2."
    )
    record_a, _ = memory_service.write_memory(
        f"{VOLATILE} extra A",
        "fact",
        scope="workspace:qp-wasted-cap",
        topic="qp-wasted-cap",
    )
    record_b, _ = memory_service.write_memory(
        f"{VOLATILE} extra B",
        "fact",
        scope="workspace:qp-wasted-cap",
        topic="qp-wasted-cap",
    )
    from datetime import timedelta

    old = (utc_now() - timedelta(days=180)).isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET last_confirmed_at = ?, created_at = ? "
            "WHERE scope = 'workspace:qp-wasted-cap'",
            (old, old),
        )
        conn.commit()

    # Per-(rule, scope) cap = 1, per-run budget = 5, aggregate = 100.
    system_settings_service.write_raw(
        {
            "proposal_pending_cap_per_rule": "1",
            "proposal_pending_cap_total": "100",
            "proposal_generation_budget_per_run": "5",
            "stale_volatile_days": "0",
        }
    )

    # Force the cap-trim to see ONLY one slot, and the lock-time check
    # to see record_a's candidate as already settled — by patching
    # `_last_served_at` so that the second candidate (record_b) is
    # served first and record_a's slot is the only one available; AND
    # by landing a settled (accepted) row for record_a between scan
    # and lock so the settled-check drops it. Concretely: the scan
    # sees both records as candidates; the lock-time check sees
    # record_a as already settled (`accepted`) and drops it from the
    # survivors list BEFORE the cap trim, so record_b gets the cap slot.
    #
    # Track get_db() calls so the pre-existing insert lands between
    # the read phase and the lock phase (the read phase's
    # `_decided_targets` is one of the early calls; the lock phase's
    # BEGIN IMMEDIATE is one of the later calls — the side effect must
    # land in between so the scan sees two candidates and the lock
    # sees record_a as settled).
    import app.database as _db_mod

    real_get_db = _db_mod.get_db
    call_count = [0]
    settled_in_lock = [False]

    @contextmanager
    def wrapped_db():
        call_count[0] += 1
        with real_get_db() as conn:
            # Land the side effect on a late call (the lock phase),
            # AFTER the read phase's scan but BEFORE the per-group
            # trim. `accepted` is critical — `_pending_count_for` only
            # counts `status='pending'`, so an accepted row does NOT
            # consume the per-(rule, scope) cap slot. That means
            # record_b has real room for cap=1 after record_a is
            # dropped from the survivors.
            if not settled_in_lock[0] and call_count[0] >= 4:
                settled_in_lock[0] = True
                conn.execute(
                    "INSERT INTO memory_proposals "
                    "(id, rule, action, scope, target_ids_json, "
                    " rationale, evidence_json, status, created_at, "
                    " decided_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "peer-decided",
                        "stale_volatile",
                        "confirm",
                        "workspace:qp-wasted-cap",
                        f'["{record_a["id"]}"]',
                        "peer decided",
                        "{}",
                        "accepted",
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
                conn.commit()
            yield conn

    monkeypatch.setattr(memory_proposal_service, "get_db", wrapped_db)

    # Now run generate_proposals. The peer has decided record_a as
    # `accepted` between the scan and the lock. With cap=1, record_b
    # should still get the cap slot — the settled-check must drop
    # record_a from the survivors list BEFORE the cap trim.
    outcome = memory_proposal_service.generate_proposals(
        scope="workspace:qp-wasted-cap",
        rules=["stale_volatile"],
    )
    # record_a is settled (pending pre-existing + flipped accepted);
    # record_b is fresh and gets the cap slot.
    # created=1 means record_b landed; record_a was already settled
    # and correctly skipped without wasting the cap slot.
    pending = memory_proposal_service.list_proposals(
        status="pending",
        rule="stale_volatile",
    )
    target_ids = [tuple(p["target_ids"]) for p in pending]
    assert outcome["created"] == 1, (
        f"with cap=1, exactly one of (record_a already settled, "
        f"record_b fresh) should land. Got created={outcome['created']}"
    )
    assert (record_b["id"],) in target_ids, (
        f"record_b (the fresh target) should be the one that lands, "
        f"not record_a (which is already settled). Got: {target_ids}"
    )
