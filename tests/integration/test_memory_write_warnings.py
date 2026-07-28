EPISODIC = (
    "SAG-638 routine fallback sweep heartbeat (2026-06-07 15:16 UTC). "
    "Continuation tick on the now-activated registry. Tests 29/29 pass."
)
DURABLE = (
    "Do NOT edit vendored dependencies directly: it is pulled frequently, so core edits get clobbered."
)


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

    def fake_duplicates(content, scope, memory_class=None, threshold=None, limit=3, exclude_id=None):
        seen["exclude_id"] = exclude_id
        # Stand in for a live vector backend: everything looks identical.
        return [{"id": exclude_id, "similarity": 1.0}] if exclude_id is None else []

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
    r = test_client.get(
        "/settings", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert r.status_code == 200
    assert "episodic-memory-ttl-days" in r.text
    assert "memory-dedupe-similarity" in r.text
