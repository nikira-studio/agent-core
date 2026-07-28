from app.database import get_db


def _write(test_client, token, content, **extra):
    body = {"content": content, "memory_class": "fact", "scope": "agent:testagent"}
    body.update(extra)
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={"tool": "memory_write", "params": body},
    )
    assert r.status_code == 201, r.json()
    return r.json()["data"]["record"]["id"]


def _search(test_client, token, query, **params):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={"tool": "memory_search", "params": {"query": query, **params}},
    )
    assert r.status_code == 200, r.json()
    return r.json()["data"]["records"]


def _age(record_id, days):
    """Backdate confirmation so staleness has something to bite on."""
    from datetime import timedelta

    from app.time_utils import utc_now

    when = (utc_now() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET last_confirmed_at = ?, created_at = ? WHERE id = ?",
            (when, when, record_id),
        )
        conn.commit()


def _counts(record_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT recall_count, helpful_count, unhelpful_count, last_recalled_at "
            "FROM memory_records WHERE id = ?",
            (record_id,),
        ).fetchone()
    return dict(row)


# --- staleness -------------------------------------------------------------


def test_results_report_how_long_since_confirmed(test_client, agent_token):
    record_id = _write(test_client, agent_token, "The scheduler ticks every ten minutes.")
    _age(record_id, 140)

    result = _search(test_client, agent_token, "scheduler")[0]
    assert result["days_since_confirmed"] == 140


def test_a_fresh_write_is_confirmed_today(test_client, agent_token):
    _write(test_client, agent_token, "The scheduler ticks every ten minutes.")
    assert _search(test_client, agent_token, "scheduler")[0]["days_since_confirmed"] == 0


def test_confirming_clears_staleness(test_client, agent_token):
    record_id = _write(test_client, agent_token, "The scheduler ticks every ten minutes.")
    _age(record_id, 200)

    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_confirm",
            "params": {"record_id": record_id, "evidence": "read app/scheduler.py"},
        },
    )
    assert r.status_code == 200, r.json()
    assert _search(test_client, agent_token, "scheduler")[0]["days_since_confirmed"] == 0


def test_stale_facts_rank_below_confirmed_ones(test_client, agent_token):
    stale = _write(test_client, agent_token, "Ranking token: the ingest port is 8000.")
    fresh = _write(test_client, agent_token, "Ranking token: the ingest port is 9000.")
    _age(stale, 400)

    order = [r["id"] for r in _search(test_client, agent_token, "ranking token")]
    assert order.index(fresh) < order.index(stale)


def test_decisions_are_not_penalised_for_age(test_client, agent_token):
    """A decision stands until revised; burying it for age loses the constraint."""
    from app.services.memory_service import _staleness_penalty

    old_fact = {"memory_class": "fact", "last_confirmed_at": "2020-01-01T00:00:00+00:00"}
    old_decision = {
        "memory_class": "decision",
        "last_confirmed_at": "2020-01-01T00:00:00+00:00",
    }
    assert _staleness_penalty(old_fact) < 0
    assert _staleness_penalty(old_decision) == 0.0


# --- observed usefulness ---------------------------------------------------


def test_returned_records_are_counted_as_recalled(test_client, agent_token):
    record_id = _write(test_client, agent_token, "the build server resolves at 192.0.2.10.")
    assert _counts(record_id)["recall_count"] == 0

    _search(test_client, agent_token, "the build server")
    counts = _counts(record_id)
    assert counts["recall_count"] == 1
    assert counts["last_recalled_at"] is not None

    _search(test_client, agent_token, "the build server")
    assert _counts(record_id)["recall_count"] == 2


def test_records_outside_the_returned_page_are_not_counted(test_client, agent_token):
    """The candidate set is an implementation detail; only what was shown counts."""
    ids = [
        _write(test_client, agent_token, f"Paging token entry number {i}.")
        for i in range(3)
    ]
    shown = _search(test_client, agent_token, "paging token", limit=1)
    assert len(shown) == 1
    counted = [i for i in ids if _counts(i)["recall_count"] > 0]
    assert counted == [shown[0]["id"]]


def test_feedback_is_recorded(test_client, agent_token):
    record_id = _write(test_client, agent_token, "the build server resolves at 192.0.2.10.")
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "memory_feedback", "params": {"record_id": record_id, "helpful": True}},
    )
    assert r.status_code == 200, r.json()
    assert r.json()["data"]["helpful_count"] == 1
    assert _counts(record_id)["helpful_count"] == 1


def test_unhelpful_records_rank_below_unrated_ones(test_client, agent_token):
    rated = _write(test_client, agent_token, "Sorting token: the first candidate.")
    unrated = _write(test_client, agent_token, "Sorting token: the second candidate.")
    for _ in range(3):
        test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={
                "tool": "memory_feedback",
                "params": {"record_id": rated, "helpful": False},
            },
        )

    order = [r["id"] for r in _search(test_client, agent_token, "sorting token")]
    assert order.index(unrated) < order.index(rated)


def test_helpful_records_rank_above_unrated_ones(test_client, agent_token):
    helpful = _write(test_client, agent_token, "Lifting token: the first candidate.")
    unrated = _write(test_client, agent_token, "Lifting token: the second candidate.")
    for _ in range(3):
        test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={
                "tool": "memory_feedback",
                "params": {"record_id": helpful, "helpful": True},
            },
        )

    order = [r["id"] for r in _search(test_client, agent_token, "lifting token")]
    assert order.index(helpful) < order.index(unrated)


def test_feedback_needs_only_read_access(test_client, agent_token, admin_token):
    """A reader who cannot rate what it was given never reaches the ranking."""
    from app.services.memory_service import write_memory

    record, _ = write_memory(
        content="A record in a scope the agent can read but not write.",
        memory_class="fact",
        scope="user:admin",
    )
    test_client.put(
        "/api/agents/testagent",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"read_scopes": ["user:admin"], "write_scopes": []},
    )

    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_feedback",
            "params": {"record_id": record["id"], "helpful": True},
        },
    )
    assert r.status_code == 200, r.json()


def test_confirm_requires_write_access(test_client, agent_token, admin_token):
    from app.services.memory_service import write_memory

    record, _ = write_memory(
        content="A record the agent may read but must not re-confirm.",
        memory_class="fact",
        scope="user:admin",
    )
    test_client.put(
        "/api/agents/testagent",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"read_scopes": ["user:admin"], "write_scopes": []},
    )

    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_confirm",
            "params": {"record_id": record["id"], "evidence": "read the config"},
        },
    )
    assert r.status_code == 403


def test_confirm_and_feedback_are_audited(test_client, agent_token):
    record_id = _write(test_client, agent_token, "the build server resolves at 192.0.2.10.")
    test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_confirm",
            "params": {"record_id": record_id, "evidence": "checked the host"},
        },
    )
    test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "memory_feedback", "params": {"record_id": record_id, "helpful": False}},
    )
    with get_db() as conn:
        actions = {
            row["action"]
            for row in conn.execute(
                "SELECT action FROM audit_log WHERE resource_id = ?", (record_id,)
            ).fetchall()
        }
    assert {"memory_confirmed", "memory_feedback"} <= actions
