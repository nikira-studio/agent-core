EPISODIC = (
    "SAG-638 routine fallback sweep heartbeat (2026-06-07 15:16 UTC). "
    "Continuation tick on the now-activated registry. Tests 29/29 pass."
)
DURABLE = "Do NOT edit vendored dependencies directly: it is pulled frequently, so core edits get clobbered."


def test_mcp_write_warns_on_episodic_content(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_write",
            "params": {
                "content": EPISODIC,
                "memory_class": "fact",
                "scope": "agent:testagent",
            },
        },
    )
    assert r.status_code == 201, r.json()
    data = r.json()["data"]

    codes = [w["code"] for w in data.get("warnings", [])]
    assert "EPISODIC_CONTENT" in codes
    # The write still succeeds — advisory, never blocking.
    assert data["record"]["id"]
    assert data["record"]["expires_at"] is not None


def test_mcp_write_is_quiet_for_durable_content(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_write",
            "params": {
                "content": DURABLE,
                "memory_class": "decision",
                "scope": "agent:testagent",
            },
        },
    )
    assert r.status_code == 201, r.json()
    data = r.json()["data"]
    assert "warnings" not in data
    assert data["record"]["expires_at"] is None


def test_rest_write_warns_on_episodic_content(test_client, agent_token):
    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": EPISODIC,
            "memory_class": "fact",
            "scope": "agent:testagent",
        },
    )
    assert r.status_code == 201, r.json()
    data = r.json()["data"]
    assert [w["code"] for w in data.get("warnings", [])] == ["EPISODIC_CONTENT"]


def test_episodic_record_is_swept_once_expired(test_client, agent_token):
    """The expiry has to actually retire the record, not just decorate it."""
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_write",
            "params": {
                "content": EPISODIC,
                "memory_class": "fact",
                "scope": "agent:testagent",
            },
        },
    )
    record_id = r.json()["data"]["record"]["id"]

    from app.database import get_db
    from app.services import backup_service

    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (record_id,),
        )
        conn.commit()

    backup_service.run_scheduled_maintenance(triggered_by="test")

    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM memory_records WHERE id = ?", (record_id,)
        ).fetchone()
    assert row is None


def test_a_write_is_never_a_duplicate_of_itself(test_client, agent_token, monkeypatch):
    """The check runs after the write, so the new row is in the corpus already."""
    from app.services import memory_service

    seen = {}

    def fake_duplicates(
        content,
        scope,
        memory_class=None,
        threshold=None,
        limit=3,
        exclude_id=None,
        statuses=("active",),
    ):
        seen["exclude_id"] = exclude_id
        # Stand in for a live vector backend: everything looks identical.
        if exclude_id is None:
            return [{"id": exclude_id, "similarity": 1.0, "record_status": "active"}]
        return []

    monkeypatch.setattr(memory_service, "find_near_duplicates", fake_duplicates)

    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_write",
            "params": {
                "content": DURABLE,
                "memory_class": "decision",
                "scope": "agent:testagent",
            },
        },
    )
    assert r.status_code == 201, r.json()
    assert seen["exclude_id"] == r.json()["data"]["record"]["id"]
    assert "warnings" not in r.json()["data"]


def test_settings_accept_the_new_knobs(test_client, admin_token):
    r = test_client.post(
        "/api/dashboard/system-settings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "scratchpad_retention_days": "7",
            "solo_mode_enabled": "false",
            "episodic_memory_ttl_days": "14",
            "memory_dedupe_similarity": "0.88",
        },
    )
    assert r.status_code == 200, r.json()
    saved = r.json()["data"]["settings"]
    assert saved["episodic_memory_ttl_days"] == "14"
    assert saved["memory_dedupe_similarity"] == "0.88"

    from app.services.memory_service import episodic_ttl_days

    assert episodic_ttl_days() == 14


def test_settings_reject_out_of_range_knobs(test_client, admin_token):
    base = {"scratchpad_retention_days": "7", "solo_mode_enabled": "false"}

    r = test_client.post(
        "/api/dashboard/system-settings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={**base, "episodic_memory_ttl_days": "9999"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_EPISODIC_TTL"

    r2 = test_client.post(
        "/api/dashboard/system-settings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={**base, "memory_dedupe_similarity": "0.1"},
    )
    assert r2.status_code == 400
    assert r2.json()["error"]["code"] == "INVALID_DEDUPE_SIMILARITY"


def test_settings_zero_ttl_is_allowed(test_client, admin_token):
    """0 means 'store episodic writes permanently' — a real choice, not an error."""
    r = test_client.post(
        "/api/dashboard/system-settings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "scratchpad_retention_days": "7",
            "solo_mode_enabled": "false",
            "episodic_memory_ttl_days": "0",
        },
    )
    assert r.status_code == 200, r.json()
    assert r.json()["data"]["settings"]["episodic_memory_ttl_days"] == "0"


def test_settings_page_renders_the_new_controls(test_client, admin_token):
    r = test_client.get("/settings", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert "episodic-memory-ttl-days" in r.text
    assert "memory-dedupe-similarity" in r.text


# --- Workstream 4: PREVIOUSLY_RETRACTED write-time warning -----------------
#
# A write that closely matches a previously-retracted record should be
# flagged, separately from POSSIBLE_DUPLICATE, even on installations with no
# vector search configured (the non-embedding fallback covers both).
# The plan is explicit that this must work whether vector search is enabled
# or whether the embedding call transiently fails.

PREFIX = "the assistant dashboard is currently served from 127.0.0.1:19119 on the build server"


def _seed_retracted(scope, content, *, with_embeddings=True):
    """Insert a retracted record directly. Embeddings are skipped unless
    explicitly requested so the non-embedding fallback is exercised by
    default — the sandbox the reviewer ran their probe against had no
    vector search configured, which is exactly the case this workstream
    must support."""
    from app.database import get_db
    from app.services import memory_service

    record, _ = memory_service.write_memory(
        content=content,
        memory_class="fact",
        scope=scope,
        source_kind="agent_inference",
    )
    memory_service.retract_memory(record["id"])
    # Pin the retracted_at timestamp to a known value so we can assert the
    # warning carries it.
    fixed = "2026-04-15T12:00:00+00:00"
    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET status_changed_at = ? WHERE id = ?",
            (fixed, record["id"]),
        )
        conn.commit()
    return record["id"], fixed


def test_write_near_a_retracted_record_flags_previously_retracted_without_embeddings(
    test_client,
    agent_token,
):
    """Vector search disabled: the non-embedding prefix fallback must still
    flag the write. This is the default state on a fresh installation."""
    _seed_retracted("agent:testagent", PREFIX, with_embeddings=False)

    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": PREFIX + " and the image is vendor/example-agent:v2026.5.29.2.",
            "memory_class": "fact",
            "scope": "agent:testagent",
        },
    )
    assert r.status_code == 201, r.json()
    data = r.json()["data"]
    codes = [w["code"] for w in data.get("warnings", [])]
    assert "PREVIOUSLY_RETRACTED" in codes
    pw = next(w for w in data["warnings"] if w["code"] == "PREVIOUSLY_RETRACTED")
    assert pw["candidates"][0]["retracted_at"].startswith("2026-04-15")
    # The advisory must not block the write.
    assert data["record"]["id"]


def test_write_near_a_retracted_record_uses_one_embedding_when_available(
    test_client,
    agent_token,
):
    """Vector search enabled, embedding call succeeds: still exactly one
    embedding per write's duplicate check, and the retracted flag is raised
    from the same single embedding query (not a separate one)."""
    from unittest.mock import patch

    from app.services import memory_service

    # Seed a retracted record whose content shares the prefix. The seed
    # writes its own embeddings too; we'll reset the mock afterwards to
    # count only the duplicate-check calls.
    _seed_retracted("agent:testagent", PREFIX)

    mock_vector = b"\x00" * 384 * 4
    with (
        patch(
            "app.services.embedding_service.get_embedding_backend_status",
            return_value={"backend": "healthy", "model_configured": True},
        ),
        patch(
            "app.services.embedding_service.generate_embedding",
            return_value=(mock_vector, "ok"),
        ) as mock_gen,
        patch(
            "app.services.vector_settings_service.is_vector_search_enabled",
            return_value=True,
        ),
        patch("app.services.vector_service.cosine_search_top_k", return_value=[]),
    ):
        mock_gen.reset_mock()
        warnings = memory_service.assess_memory_write(
            content=PREFIX + " extended with more context.",
            scope="agent:testagent",
            memory_class="fact",
        )
        # assess_memory_write is the duplicate-check entry point. Exactly
        # one embedding call from here, regardless of how many candidates
        # exist across statuses.
        assert mock_gen.call_count == 1, (
            f"expected one embedding call from duplicate check, got {mock_gen.call_count}"
        )
        codes = [w["code"] for w in warnings]
        assert "PREVIOUSLY_RETRACTED" in codes


def test_write_embedding_failure_still_flags_retracted_via_fallback(
    test_client,
    agent_token,
):
    """Vector search enabled, embedding call raises: the non-embedding
    fallback runs anyway and still surfaces the retracted match. Round 1 of
    the plan missed this transient-failure case."""
    from unittest.mock import patch

    _seed_retracted("agent:testagent", PREFIX)

    with (
        patch(
            "app.services.embedding_service.get_embedding_backend_status",
            return_value={"backend": "healthy", "model_configured": True},
        ),
        patch(
            "app.services.embedding_service.generate_embedding",
            side_effect=RuntimeError("embedding provider down"),
        ),
        patch(
            "app.services.vector_settings_service.is_vector_search_enabled",
            return_value=True,
        ),
    ):
        r = test_client.post(
            "/api/memory/write",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={
                "content": PREFIX + " even more detail here.",
                "memory_class": "fact",
                "scope": "agent:testagent",
            },
        )
        assert r.status_code == 201, r.json()
        codes = [w["code"] for w in r.json()["data"].get("warnings", [])]
        assert "PREVIOUSLY_RETRACTED" in codes


def test_write_with_only_active_matches_omits_previously_retracted(
    test_client,
    agent_token,
):
    """Sanity check: if no retracted record matches, only POSSIBLE_DUPLICATE
    surfaces — PREVIOUSLY_RETRACTED is not raised from thin air."""
    from app.services import memory_service

    memory_service.write_memory(
        content=PREFIX,
        memory_class="fact",
        scope="agent:testagent",
    )
    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": PREFIX + " with extra detail.",
            "memory_class": "fact",
            "scope": "agent:testagent",
        },
    )
    assert r.status_code == 201, r.json()
    codes = [w["code"] for w in r.json()["data"].get("warnings", [])]
    # No retracted record exists, so PREVIOUSLY_RETRACTED is not produced.
    # POSSIBLE_DUPLICATE may or may not appear depending on the embedding
    # backend (which is disabled in the sandbox), so we only assert the
    # absence of PREVIOUSLY_RETRACTED.
    assert "PREVIOUSLY_RETRACTED" not in codes


def test_restored_record_produces_possible_duplicate_not_previously_retracted(
    test_client,
    agent_token,
):
    """After restore_memory reactivates a record, the warning semantics revert
    to POSSIBLE_DUPLICATE — there is no retracted record to flag anymore."""
    from app.services import memory_service

    record_id, _ = _seed_retracted("agent:testagent", PREFIX, with_embeddings=False)
    memory_service.restore_memory(record_id)

    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": PREFIX + " additional context.",
            "memory_class": "fact",
            "scope": "agent:testagent",
        },
    )
    assert r.status_code == 201, r.json()
    codes = [w["code"] for w in r.json()["data"].get("warnings", [])]
    assert "PREVIOUSLY_RETRACTED" not in codes


def test_write_is_never_flagged_as_previously_retracted_for_scratchpad(
    test_client,
    agent_token,
):
    """scratchpad writes skip both duplicate checks, same as before."""
    _seed_retracted("agent:testagent", PREFIX, with_embeddings=False)

    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": PREFIX + " scratchpad variant.",
            "memory_class": "scratchpad",
            "scope": "agent:testagent",
        },
    )
    assert r.status_code == 201, r.json()
    codes = [w["code"] for w in r.json()["data"].get("warnings", [])]
    assert "PREVIOUSLY_RETRACTED" not in codes
    assert "POSSIBLE_DUPLICATE" not in codes


def test_find_near_duplicates_returns_record_status_per_candidate(clean_db):
    """Direct service-level check: per-status partitioning is reflected in
    the returned candidates, and the limit is applied within each status."""
    from app.services import memory_service

    scope = "workspace:fixture"
    # 4 retracted near-duplicates of the same prefix.
    for i in range(4):
        record, _ = memory_service.write_memory(
            content=PREFIX + f" extra-{i}.",
            memory_class="fact",
            scope=scope,
        )
        memory_service.retract_memory(record["id"])
    # 1 active near-duplicate.
    memory_service.write_memory(
        content=PREFIX + " active extra.",
        memory_class="fact",
        scope=scope,
    )

    candidates = memory_service.find_near_duplicates(
        PREFIX + " new write.",
        scope=scope,
        memory_class="fact",
        limit=2,
        statuses=("active", "retracted"),
    )
    by_status = {}
    for c in candidates:
        by_status.setdefault(c["record_status"], []).append(c)
    # limit=2 applies per status -> at most 2 retracted and at most 2 active.
    assert len(by_status.get("retracted", [])) <= 2
    assert len(by_status.get("active", [])) <= 2
    # Every candidate carries its own record_status.
    assert all("record_status" in c for c in candidates)
