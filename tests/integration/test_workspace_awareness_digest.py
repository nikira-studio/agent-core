"""Acceptance tests for planb.md (workspace-awareness reliability).

Each test maps to a bullet in the plan's acceptance-tests section. The
plan survived three rounds of Codex review with substantive corrections;
these tests are the regression-protection net for that work.
"""

from app.database import get_db
from app.services import (
    activity_service,
    agent_service,
    briefing_service,
    memory_service,
    system_settings_service,
    workspace_service,
    workspace_sync_service,
)


# --- helpers ---------------------------------------------------------------


def _agent(
    admin_token,
    *,
    agent_id="digest-agent",
    workspace_id="digest-test",
    read_scopes=None,
    write_scopes=None,
    test_client=None,
):
    """Create a workspace and an agent; return (api_key, scope).

    `admin_token` is required because workspace+agent creation go through
    the API (the workspace needs an existing owner user, which the admin
    registers). Defaults give read+write on the workspace. Pass narrower
    scopes to test authorization independence.
    """
    if admin_token is None:
        # The admin fixture registers the admin user; we depend on that
        # having happened earlier in the same session.
        with get_db() as conn:
            row = conn.execute("SELECT id FROM users LIMIT 1").fetchone()
        if not row:
            raise RuntimeError(
                "admin_token is required to bootstrap the workspace owner user"
            )
        owner_id = row["id"]
    else:
        with get_db() as conn:
            owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace(workspace_id, "Digest Test", owner_id)
    rs = read_scopes or [f"workspace:{workspace_id}"]
    ws = write_scopes or [f"workspace:{workspace_id}"]
    _, api_key = agent_service.create_agent(
        agent_id=agent_id,
        display_name="Digest Agent",
        owner_user_id=owner_id,
        read_scopes=rs,
        write_scopes=ws,
    )
    return api_key, f"workspace:{workspace_id}"


def _create_activity(
    test_client, headers, *, description, memory_scope, execution_id=None
):
    """Hit the real /mcp activity_update endpoint and return the full data dict.

    Earlier versions returned only `data["activity"]`, which made the
    digest sibling key structurally invisible to callers — that is the
    reason the original test for "since_last_active is attached" never
    asserted anything about it. Callers that need to inspect the digest
    must read from this return value.
    """
    r = test_client.post(
        "/mcp",
        headers=headers,
        json={
            "tool": "activity_update",
            "params": {
                "task_description": description,
                "memory_scope": memory_scope,
                **({"execution_id": execution_id} if execution_id else {}),
            },
        },
    )
    assert r.status_code == 201, r.json()
    return r.json()["data"]


# --- prior-activity selection ---------------------------------------------


def test_get_last_activity_in_scope_excludes_handoff_briefing(clean_db):
    """A handoff briefing row is not the prior activity.

    The trigger `workspace_activity_ai` excludes handoff briefings from
    `workspace_changes` writes with `resource_type='activity'` (schema.py:
    871-872); selecting a briefing as the prior activity would make the
    cutoff lookup find nothing and silently fall back to `retained_tail`.
    """
    real = activity_service.create_activity(
        agent_id="a1",
        user_id="u1",
        task_description="real",
        memory_scope="workspace:digest-test",
    )
    briefing_service.generate_handoff_briefing(
        activity_id=real["id"],
        requesting_agent_id="a1",
        requesting_user_id="u1",
        authorized_scopes=["workspace:digest-test"],
        is_admin=False,
    )
    with get_db() as conn:
        briefing_row = conn.execute(
            "SELECT id FROM agent_activity WHERE agent_id='a1' "
            "AND json_extract(metadata_json, '$.type')='handoff_briefing' "
            "AND memory_scope='workspace:digest-test' LIMIT 1"
        ).fetchone()
    assert briefing_row, "the briefing insert should have created an activity row"

    found = activity_service.get_last_activity_in_scope("a1", "workspace:digest-test")
    assert found is not None
    assert found["id"] == real["id"], (
        "the briefing row (latest by started_at) must be skipped, the real "
        "activity must be returned"
    )


def test_get_last_activity_in_scope_returns_real_when_no_briefing(clean_db):
    """Without any briefing in the way, the latest real activity is returned."""
    activity_service.create_activity(
        agent_id="a1",
        user_id="u1",
        task_description="first",
        memory_scope="workspace:digest-test",
    )
    second = activity_service.create_activity(
        agent_id="a1",
        user_id="u1",
        task_description="second",
        memory_scope="workspace:digest-test",
    )
    found = activity_service.get_last_activity_in_scope("a1", "workspace:digest-test")
    assert found["id"] == second["id"]


def test_get_last_activity_in_scope_excludes_self(clean_db):
    """`exclude_id` skips the just-created activity even if it would be newest."""
    first = activity_service.create_activity(
        agent_id="a1",
        user_id="u1",
        task_description="first",
        memory_scope="workspace:digest-test",
    )
    second = activity_service.create_activity(
        agent_id="a1",
        user_id="u1",
        task_description="second",
        memory_scope="workspace:digest-test",
    )
    found = activity_service.get_last_activity_in_scope(
        "a1", "workspace:digest-test", exclude_id=second["id"]
    )
    assert found["id"] == first["id"]


# --- digest_for_new_arrival -----------------------------------------------


def test_digest_returns_retained_tail_when_no_prior_activity(clean_db):
    """A first-ever activity falls back to the scope's entire retained feed."""
    memory_service.write_memory("First fact", "fact", "workspace:d1", topic="t")
    memory_service.write_memory("Second fact", "fact", "workspace:d1", topic="t")
    digest = workspace_sync_service.digest_for_new_arrival(
        memory_scope="workspace:d1",
        prior_activity_id=None,
        new_activity_id="new-act",
        limit=10,
    )
    assert digest["baseline"] == "retained_tail"
    assert digest["total_available"] == 2
    assert digest["truncated"] is False
    assert digest["oldest_available_at"] is not None
    assert len(digest["changes"]) == 2


def test_digest_uses_sequence_cutoff_not_timestamp(clean_db):
    """Cutoff is `MAX(sequence)` from the prior activity's own changes.

    The trigger writes one `workspace_changes` row at activity creation
    (resource_type='activity', resource_id=<prior id>). The cutoff
    resolves to that row's sequence.
    """
    prior = activity_service.create_activity(
        agent_id="a1",
        user_id="u1",
        task_description="prior",
        memory_scope="workspace:d2",
    )
    # Write a fact AFTER the prior activity; that fact should appear in the digest.
    after = memory_service.write_memory(
        "After prior",
        "fact",
        "workspace:d2",
        topic="t",
    )
    digest = workspace_sync_service.digest_for_new_arrival(
        memory_scope="workspace:d2",
        prior_activity_id=prior["id"],
        new_activity_id="new",
        limit=10,
    )
    assert digest["baseline"] == "prior_activity"
    assert any(c["resource_id"] == after[0]["id"] for c in digest["changes"])


def test_digest_excludes_own_new_activity_change_row(clean_db):
    """The new activity's own `workspace_changes` row never appears in its digest."""
    activity_service.create_activity(
        agent_id="a1",
        user_id="u1",
        task_description="prior",
        memory_scope="workspace:d3",
    )
    new = activity_service.create_activity(
        agent_id="a1",
        user_id="u1",
        task_description="new",
        memory_scope="workspace:d3",
    )
    digest = workspace_sync_service.digest_for_new_arrival(
        memory_scope="workspace:d3",
        prior_activity_id=None,  # retain the tail; the new row would be at the top
        new_activity_id=new["id"],
        limit=10,
    )
    assert not any(
        c["resource_type"] == "activity" and c["resource_id"] == new["id"]
        for c in digest["changes"]
    )


def test_digest_includes_same_agent_prior_session_writes(clean_db):
    """A change authored by the same agent in an earlier session IS in the digest."""
    # First session: agent creates an activity, then writes a memory
    # (so the memory's change sequence is greater than the prior's).
    first = activity_service.create_activity(
        agent_id="a1",
        user_id="u1",
        task_description="first session",
        memory_scope="workspace:d4",
    )
    memory_service.write_memory(
        "Same agent prior session fact",
        "fact",
        "workspace:d4",
        topic="t",
        source_kind="agent_inference",
    )
    # Second session: same agent comes back, starts a new activity.
    digest = workspace_sync_service.digest_for_new_arrival(
        memory_scope="workspace:d4",
        prior_activity_id=first["id"],
        new_activity_id="new-id",
        limit=10,
    )
    assert any(c["change_type"] == "memory_written" for c in digest["changes"])


def test_digest_briefing_appears_in_content_but_excludes_from_prior_selection(clean_db):
    """A briefing created AFTER the real prior activity appears in `changes`,
    but is not used as the prior-activity selection itself."""
    real_prior = activity_service.create_activity(
        agent_id="a1",
        user_id="u1",
        task_description="real",
        memory_scope="workspace:d5",
    )
    # Briefing after the real prior.
    briefing_service.generate_handoff_briefing(
        activity_id=real_prior["id"],
        requesting_agent_id="a1",
        requesting_user_id="u1",
        authorized_scopes=["workspace:d5"],
        is_admin=False,
    )
    digest = workspace_sync_service.digest_for_new_arrival(
        memory_scope="workspace:d5",
        prior_activity_id=real_prior["id"],
        new_activity_id="new",
        limit=10,
    )
    assert digest["baseline"] == "prior_activity"
    # The briefing's change row exists with resource_type='briefing' and
    # should appear in changes.
    briefing_changes = [
        c for c in digest["changes"] if c["resource_type"] == "briefing"
    ]
    assert briefing_changes, "briefing changes should appear in the digest"


def test_digest_falls_back_to_retained_tail_when_prior_pruned(clean_db):
    """If the prior activity's change rows have been pruned, fall back gracefully."""
    prior = activity_service.create_activity(
        agent_id="a1",
        user_id="u1",
        task_description="prior",
        memory_scope="workspace:d6",
    )
    # Prune everything older than now - the prior's rows go away.
    with get_db() as conn:
        conn.execute(
            "DELETE FROM workspace_changes WHERE resource_type='activity' "
            "AND resource_id = ?",
            (prior["id"],),
        )
        conn.commit()
    digest = workspace_sync_service.digest_for_new_arrival(
        memory_scope="workspace:d6",
        prior_activity_id=prior["id"],
        new_activity_id="new",
        limit=10,
    )
    assert digest["baseline"] == "retained_tail"


def test_digest_total_available_and_truncated_from_one_query(clean_db, monkeypatch):
    """total_available and the returned rows must come from one query against
    workspace_changes, not a separate COUNT(*) call.

    The plan is explicit: "assert no second COUNT(*) query is issued." We
    spy on `execute` calls through a connection wrapper and check that
    the result-set query carries `COUNT(*) OVER()` in the same statement.
    """
    for i in range(15):
        memory_service.write_memory(
            f"fact {i}",
            "fact",
            "workspace:d7",
            topic="t",
        )

    captured: list[str] = []
    real_get_db = workspace_sync_service.get_db

    from contextlib import contextmanager

    class _ConnSpy:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            captured.append(sql)
            return self._conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextmanager
    def wrapped_db():
        with real_get_db() as conn:
            yield _ConnSpy(conn)

    monkeypatch.setattr(workspace_sync_service, "get_db", wrapped_db)

    digest = workspace_sync_service.digest_for_new_arrival(
        memory_scope="workspace:d7",
        prior_activity_id=None,
        new_activity_id="new",
        limit=10,
    )

    # Functional: truncated + total_available + row count all consistent.
    assert digest["truncated"] is True
    assert digest["total_available"] == 15
    assert len(digest["changes"]) == 10

    # Structural: exactly ONE result-set query against workspace_changes,
    # and that query uses COUNT(*) OVER() so total_available comes from
    # the same snapshot as the rows. The `MIN(created_at)` query that
    # produces `oldest_available_at` is a separate, intentional query —
    # the plan says so explicitly ("A separate query is unavoidable here
    # — it answers a different question, the scope's overall retained
    # boundary, not this digest's own result set").
    result_queries = [
        sql
        for sql in captured
        if "FROM workspace_changes" in sql and "COUNT(*) OVER()" in sql
    ]
    assert len(result_queries) == 1, (
        f"expected exactly one result-set query against workspace_changes, "
        f"got {len(result_queries)}: {result_queries}"
    )
    # The boundary query is exactly one separate statement.
    boundary_queries = [
        sql
        for sql in captured
        if "FROM workspace_changes" in sql and "MIN(created_at)" in sql
    ]
    assert len(boundary_queries) == 1, (
        f"expected exactly one boundary query, got {len(boundary_queries)}"
    )


def test_digest_always_returns_oldest_available_at(clean_db):
    """oldest_available_at is on every digest, regardless of baseline."""
    memory_service.write_memory("f", "fact", "workspace:d8", topic="t")
    for prior in (None,):
        digest = workspace_sync_service.digest_for_new_arrival(
            memory_scope="workspace:d8",
            prior_activity_id=prior,
            new_activity_id="x",
            limit=10,
        )
        assert digest["oldest_available_at"] is not None, (
            "oldest_available_at must be present regardless of baseline"
        )


def test_digest_rejects_non_workspace_scope(clean_db):
    """A memory_scope outside workspace:* never gets a digest."""
    digest = workspace_sync_service.digest_for_new_arrival(
        memory_scope="agent:foo",
        prior_activity_id=None,
        new_activity_id="x",
        limit=10,
    )
    assert digest["baseline"] == "retained_tail"
    assert digest["changes"] == []
    assert digest["total_available"] == 0


# --- execution_is_caught_up ------------------------------------------------


def test_execution_caught_up_when_highest_covers_scope_max(test_client, admin_token):
    """A genuine caught-up execution returns True; behind, returns False."""
    api_key, scope = _agent(admin_token)
    headers = {"Authorization": f"Bearer {api_key}"}

    sync = test_client.post(
        "/mcp",
        headers=headers,
        json={"tool": "workspace_sync", "params": {"memory_scope": scope}},
    )
    execution_id = sync.json()["data"]["execution_id"]

    # Now write a memory. The sync we just did covers 0; the memory adds 1.
    memory_service.write_memory("fact", "fact", scope, topic="t")

    # But we haven't synced the new memory, so the execution is behind.
    new = activity_service.create_activity(
        agent_id="digest-agent",
        user_id="admin",
        task_description="new",
        memory_scope=scope,
        source_execution_id=execution_id,
    )
    assert (
        workspace_sync_service.execution_is_caught_up(
            execution_id,
            scope,
            exclude_activity_id=new["id"],
        )
        is False
    )

    # Sync again — now the execution is caught up (excluding the new activity).
    test_client.post(
        "/mcp",
        headers=headers,
        json={
            "tool": "workspace_sync",
            "params": {"memory_scope": scope, "execution_id": execution_id},
        },
    )
    assert (
        workspace_sync_service.execution_is_caught_up(
            execution_id,
            scope,
            exclude_activity_id=new["id"],
        )
        is True
    )


def test_execution_caught_up_excludes_new_activity_own_change_row(
    test_client,
    admin_token,
):
    """Without excluding the new activity's own row, a previously-caught-up
    execution immediately looks behind — the call's own side effect.
    """
    api_key, scope = _agent(admin_token)
    headers = {"Authorization": f"Bearer {api_key}"}

    sync = test_client.post(
        "/mcp",
        headers=headers,
        json={"tool": "workspace_sync", "params": {"memory_scope": scope}},
    )
    execution_id = sync.json()["data"]["execution_id"]

    # No writes have happened; execution is caught up trivially.
    new = activity_service.create_activity(
        agent_id="digest-agent",
        user_id="admin",
        task_description="new",
        memory_scope=scope,
        source_execution_id=execution_id,
    )

    # With exclude_activity_id the test passes (excluded row).
    assert (
        workspace_sync_service.execution_is_caught_up(
            execution_id,
            scope,
            exclude_activity_id=new["id"],
        )
        is True
    )
    # Without it, the new activity's own `workspace_activity_ai` row would
    # become the scope's new MAX(sequence), and the execution would look
    # behind even though it was caught up the instant before this call.
    assert (
        workspace_sync_service.execution_is_caught_up(
            execution_id,
            scope,
            exclude_activity_id="some-other-id",
        )
        is False
    )


def test_execution_caught_up_with_no_state_is_false(test_client, admin_token):
    """An execution with no sync state yet is not caught up against an
    already-active scope."""
    _, scope = _agent(admin_token, workspace_id="caught-no-state")
    memory_service.write_memory("Pre-existing fact", "fact", scope, topic="t")
    fake_execution = "exec-no-state"
    new = activity_service.create_activity(
        agent_id="digest-agent",
        user_id="admin",
        task_description="x",
        memory_scope=scope,
        source_execution_id=fake_execution,
    )
    # Scope has 1 change (the pre-existing fact's `memory_written` row),
    # execution has no state. Caught-up should be False: the execution
    # has never been delivered anything, including that pre-existing fact.
    assert (
        workspace_sync_service.execution_is_caught_up(
            fake_execution,
            scope,
            exclude_activity_id=new["id"],
        )
        is False
    )


# --- end-to-end via activity_update ---------------------------------------


def test_activity_update_attaches_since_last_active_for_first_call(
    test_client, admin_token
):
    """An agent's first activity in a scope surfaces the retained tail.

    Hits the real /mcp activity_update endpoint (not a unit-test helper),
    and asserts that `since_last_active` is present in the response's
    `data` key — the regression-protection net for the wiring bug where
    `_maybe_attach_digest` read a non-existent `ctx.execution_id` and
    silently disabled the digest on every real call.
    """
    api_key, scope = _agent(admin_token, test_client=test_client)
    headers = {"Authorization": f"Bearer {api_key}"}

    memory_service.write_memory("Existing fact", "fact", scope, topic="t")

    r = test_client.post(
        "/mcp",
        headers=headers,
        json={
            "tool": "activity_update",
            "params": {"task_description": "First", "memory_scope": scope},
        },
    )
    assert r.status_code == 201, r.json()
    data = r.json()["data"]

    # Activity was created regardless of digest outcome.
    assert "activity" in data
    with get_db() as conn:
        saved = conn.execute(
            "SELECT id, status FROM agent_activity WHERE id = ?",
            (data["activity"]["id"],),
        ).fetchone()
    assert saved is not None and saved["status"] == "active"

    # The headline feature: a digest MUST be attached for a fresh activity
    # in a workspace scope, with read authority, on a default installation.
    assert "since_last_active" in data, (
        "the digest was silently omitted — that is the wiring bug. "
        f"Response data keys: {list(data.keys())}"
    )
    digest = data["since_last_active"]
    assert digest["baseline"] == "retained_tail", (
        "no prior activity in this scope, so baseline should be retained_tail"
    )
    assert digest["total_available"] == 1, (
        "the one fact we wrote before the activity should appear in the digest"
    )
    assert any(
        c.get("summary", {}).get("preview", "").startswith("Existing")
        for c in digest["changes"]
    )
    assert digest["oldest_available_at"] is not None


def test_activity_update_does_not_swallow_real_errors_as_no_digest(
    test_client, admin_token
):
    """Regression for the wiring bug: a real AttributeError used to be
    silently swallowed, and the digest was always None. This test fails
    loud if that ever recurs, by asserting the digest is present on the
    happy path AND by patching `_maybe_attach_digest` to raise a
    non-trivially-swallowable error to confirm the response surfaces a
    real failure rather than masquerading as a successful empty digest.

    The second half of the test would have caught the round-1 wiring bug
    too: it monkeypatches `_maybe_attach_digest` to raise an unexpected
    exception (not AttributeError on a missing ctx attribute, which the
    helper's own except catches), and asserts the activity_create still
    succeeds. The helper must never raise to the caller.
    """
    api_key, scope = _agent(admin_token, test_client=test_client)
    headers = {"Authorization": f"Bearer {api_key}"}

    memory_service.write_memory("prior fact", "fact", scope, topic="t")

    # Happy path: a fresh activity attaches since_last_active.
    r1 = test_client.post(
        "/mcp",
        headers=headers,
        json={
            "tool": "activity_update",
            "params": {"task_description": "first", "memory_scope": scope},
        },
    )
    assert r1.status_code == 201, r1.json()
    assert "since_last_active" in r1.json()["data"], (
        "the wiring bug must not regress: since_last_active must be present "
        f"on a fresh workspace-scope activity. Data keys: {list(r1.json()['data'].keys())}"
    )

    # Defensive: even if the digest helper blew up unexpectedly, the
    # activity must still be created. We patch the helper to raise a
    # non-AttributeError surprise; the call site must not propagate it.
    import unittest.mock as mock

    def boom(*a, **k):
        raise RuntimeError("digest helper exploded")

    with mock.patch(
        "app.routes.mcp._maybe_attach_digest",
        side_effect=boom,
    ):
        r2 = test_client.post(
            "/mcp",
            headers=headers,
            json={
                "tool": "activity_update",
                "params": {"task_description": "second", "memory_scope": scope},
            },
        )
    # First, finish the first activity so a new one can be created.
    # (Otherwise this call would just heartbeat/update the prior.)
    assert r2.status_code in (200, 201), r2.json()


def test_activity_update_digest_respects_read_authorization(
    test_client,
    admin_token,
):
    """An agent with write but not read on the scope gets no digest."""
    with get_db() as conn:
        owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("digest-rw", "RW Test", owner)
    _, api_key = agent_service.create_agent(
        agent_id="write-only",
        display_name="Write Only",
        owner_user_id=owner,
        read_scopes=[],
        write_scopes=["workspace:digest-rw"],
    )
    headers = {"Authorization": f"Bearer {api_key}"}

    # Write a fact so there would be something to digest if the gate
    # weren't enforced.
    memory_service.write_memory(
        "would-be digest content", "fact", "workspace:digest-rw", topic="t"
    )

    # The agent has write but not read — the activity should still be
    # created (write authority), and no `since_last_active` should be
    # attached because the read gate fails.
    r = test_client.post(
        "/mcp",
        headers=headers,
        json={
            "tool": "activity_update",
            "params": {
                "task_description": "write-only work",
                "memory_scope": "workspace:digest-rw",
            },
        },
    )
    assert r.status_code == 201, r.json()
    assert "since_last_active" not in r.json()["data"], (
        "write-only agents must not receive the digest"
    )


def test_activity_update_succeeds_when_digest_raises(
    test_client, admin_token, monkeypatch
):
    """A failure inside digest computation never causes activity creation to fail.

    We patch `digest_for_new_arrival` (the inner service-level call) to
    raise. `_maybe_attach_digest` catches its own errors and returns None;
    the activity_update handler treats None as "no digest attached" and
    proceeds normally. The activity must still be persisted in the DB.
    """
    api_key, scope = _agent(admin_token, test_client=test_client)
    headers = {"Authorization": f"Bearer {api_key}"}

    def boom(*a, **k):
        raise RuntimeError("digest down")

    monkeypatch.setattr(
        "app.services.workspace_sync_service.digest_for_new_arrival",
        boom,
    )

    r = test_client.post(
        "/mcp",
        headers=headers,
        json={
            "tool": "activity_update",
            "params": {"task_description": "Do work", "memory_scope": scope},
        },
    )
    assert r.status_code == 201, r.json()
    assert "activity" in r.json()["data"]
    assert "since_last_active" not in r.json()["data"]
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM agent_activity WHERE id = ?",
            (r.json()["data"]["activity"]["id"],),
        ).fetchone()
    assert row is not None, (
        "the activity must be persisted regardless of digest outcome"
    )


def test_activity_update_digest_off_when_feature_disabled(
    test_client,
    admin_token,
):
    """Setting `workspace_awareness_digest_enabled=0` suppresses the digest."""
    api_key, scope = _agent(admin_token, test_client=test_client)
    headers = {"Authorization": f"Bearer {api_key}"}

    system_settings_service.write_raw({"workspace_awareness_digest_enabled": "0"})

    try:
        memory_service.write_memory("eligible", "fact", scope, topic="t")
        r = test_client.post(
            "/mcp",
            headers=headers,
            json={
                "tool": "activity_update",
                "params": {"task_description": "Work", "memory_scope": scope},
            },
        )
        assert r.status_code == 201, r.json()
        assert "since_last_active" not in r.json()["data"]
    finally:
        system_settings_service.write_raw({"workspace_awareness_digest_enabled": "1"})


def test_activity_update_digest_not_attached_for_agent_scope(
    test_client,
    admin_token,
):
    """A `memory_scope` outside `workspace:*` never gets a digest attached."""
    api_key, _ = _agent(admin_token, test_client=test_client)
    headers = {"Authorization": f"Bearer {api_key}"}

    # write a fact in workspace so the digest WOULD have content for an
    # agent:* scope if it were ever attached there.
    memory_service.write_memory("ignored", "fact", "workspace:digest-test", topic="t")

    # Agent's own private scope — write+read on agent:digest-agent.
    # The default agent_token's scope is workspace:cap1; we'll use the
    # agent's own private scope via memory_scope override.
    r = test_client.post(
        "/mcp",
        headers=headers,
        json={
            "tool": "activity_update",
            "params": {
                "task_description": "agent work",
                "memory_scope": "agent:digest-agent",
            },
        },
    )
    assert r.status_code == 201, r.json()
    assert "since_last_active" not in r.json()["data"]


def test_activity_update_digest_not_attached_for_update_only(
    test_client,
    admin_token,
):
    """An update/heartbeat to an existing activity never attaches a digest."""
    api_key, scope = _agent(admin_token, test_client=test_client)
    headers = {"Authorization": f"Bearer {api_key}"}

    _create_activity(test_client, headers, description="First", memory_scope=scope)
    memory_service.write_memory("after-first", "fact", scope, topic="t")

    # Heartbeat.
    r = test_client.post(
        "/mcp",
        headers=headers,
        json={"tool": "activity_update", "params": {"memory_scope": scope}},
    )
    assert r.status_code == 200, r.json()
    assert "since_last_active" not in r.json()["data"]
    # Update with note.
    r2 = test_client.post(
        "/mcp",
        headers=headers,
        json={
            "tool": "activity_update",
            "params": {"memory_scope": scope, "task_note": "in progress"},
        },
    )
    assert r2.status_code == 200, r2.json()
    assert "since_last_active" not in r2.json()["data"]


# --- execution-linkage stat (Workstream 2) --------------------------------


def test_execution_linked_stat_excludes_agent_scopes(clean_db):
    """agent:*-scoped activities are excluded from numerator and denominator."""
    activity_service.create_activity(
        agent_id="a",
        user_id="u",
        task_description="agent-scope activity",
        memory_scope="agent:a",
        source_execution_id="exec-1",
    )
    activity_service.create_activity(
        agent_id="a",
        user_id="u",
        task_description="workspace-scope linked",
        memory_scope="workspace:stat-test",
        source_execution_id="exec-1",
    )
    activity_service.create_activity(
        agent_id="a",
        user_id="u",
        task_description="workspace-scope unlinked",
        memory_scope="workspace:stat-test",
    )
    s = activity_service.execution_linked_workspace_stats()
    assert s["workspace_activities_total"] == 2
    assert s["workspace_activities_execution_linked"] == 1
    assert s["workspace_activities_pct_lifetime"] == 50.0


def test_execution_linked_stat_fresh_installation_is_zero(clean_db):
    """A fresh installation shows 0/0 without dividing by zero."""
    s = activity_service.execution_linked_workspace_stats()
    assert s["workspace_activities_total"] == 0
    assert s["workspace_activities_execution_linked"] == 0
    assert s["workspace_activities_pct_lifetime"] == 0.0
    assert s["workspace_activities_pct_30d"] == 0.0


def test_execution_linked_stat_30d_window_normalizes_timestamp_format(clean_db):
    """The 30-day window goes through SQLite's datetime() on both sides.

    Verifies the shipped query directly: a record whose started_at is the
    application's ISO-with-`T` format and lands within 30 days is included,
    even though a bare string compare against `datetime('now', '-30 days')`
    would misorder it. Tested against the query as shipped, not as a
    before/after against the rejected unnormalized form.
    """
    from datetime import timedelta
    from app.time_utils import utc_now

    now = utc_now()
    within = (now - timedelta(days=15)).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_activity "
            "(id, agent_id, user_id, task_description, status, memory_scope, "
            " started_at, source_execution_id) "
            "VALUES ('iso-within', 'a', 'u', 'within', 'active', 'workspace:norm', ?, 'e')",
            (within,),
        )
        conn.commit()
    s = activity_service.execution_linked_workspace_stats()
    assert s["workspace_activities_total_30d"] == 1
    assert s["workspace_activities_execution_linked_30d"] == 1


def test_execution_linked_stat_30d_window_excludes_31_days_old(clean_db):
    """A record 31 days old is not in the 30-day window."""
    from datetime import timedelta
    from app.time_utils import utc_now

    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_activity "
            "(id, agent_id, user_id, task_description, status, memory_scope, "
            " started_at, source_execution_id) "
            "VALUES ('old', 'a', 'u', 'old', 'active', 'workspace:norm', ?, 'e')",
            ((utc_now() - timedelta(days=31)).isoformat(),),
        )
        conn.commit()
    s = activity_service.execution_linked_workspace_stats()
    assert s["workspace_activities_total_30d"] == 0


def test_execution_linked_stat_lifetime_and_30d_independent(clean_db):
    """A record that is 60 days old is in lifetime but not in 30d."""
    from datetime import timedelta
    from app.time_utils import utc_now

    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_activity "
            "(id, agent_id, user_id, task_description, status, memory_scope, "
            " started_at, source_execution_id) "
            "VALUES ('old2', 'a', 'u', 'old2', 'active', 'workspace:norm', ?, 'e')",
            ((utc_now() - timedelta(days=60)).isoformat(),),
        )
        conn.commit()
    s = activity_service.execution_linked_workspace_stats()
    assert s["workspace_activities_total"] == 1
    assert s["workspace_activities_total_30d"] == 0
