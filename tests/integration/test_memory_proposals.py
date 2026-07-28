HEARTBEAT = (
    "SAG-638 routine fallback sweep heartbeat (2026-06-07 15:16 UTC). Continuation tick "
    "on the now-activated registry. Tests 29/29 pass in 0.18s."
)
CLOSEOUT = (
    "STA-47 closed done 2026-06-21. Built reusable Python client package at "
    "/host/projects/Apps/healthquery/healthquery_client/ (installable as healthquery-client)."
)
DURABLE = "Do NOT edit vendored dependencies directly: it is pulled frequently, so core edits get clobbered."
VOLATILE = (
    "the assistant dashboard is currently served from 127.0.0.1:19119 on the build server with image "
    "vendor/example-agent:v2026.5.29.2."
)


def _write(scope="workspace:proj", content=DURABLE, memory_class="fact", **kwargs):
    from app.services.memory_service import write_memory

    record, _ = write_memory(
        content=content, memory_class=memory_class, scope=scope, **kwargs
    )
    return record


def _generate(test_client, admin_token, **body):
    return test_client.post(
        "/api/memory/proposals/generate",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=body,
    )


def _pending(test_client, admin_token, **params):
    r = test_client.get(
        "/api/memory/proposals",
        headers={"Authorization": f"Bearer {admin_token}"},
        params=params,
    )
    assert r.status_code == 200, r.json()
    return r.json()["data"]["proposals"]


def test_generate_finds_episodic_backlog(test_client, admin_token):
    # expires_at set explicitly: a record written before automatic expiry existed.
    stale_log = _write(content=HEARTBEAT, expires_at=None)
    from app.database import get_db

    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET expires_at = NULL WHERE id = ?", (stale_log["id"],)
        )
        conn.commit()
    _write(content=DURABLE, memory_class="decision")

    r = _generate(test_client, admin_token)
    assert r.status_code == 200, r.json()
    assert r.json()["data"]["created"] >= 1

    proposals = _pending(test_client, admin_token, rule="episodic_log")
    assert len(proposals) == 1
    assert proposals[0]["target_ids"] == [stale_log["id"]]
    assert proposals[0]["action"] == "retract"
    assert proposals[0]["evidence"]["records"][0]["id"] == stale_log["id"]


def test_durable_records_are_never_proposed(test_client, admin_token):
    _write(content=DURABLE, memory_class="decision")
    _generate(test_client, admin_token)
    assert _pending(test_client, admin_token) == []


def test_ticket_closeout_is_its_own_rule(test_client, admin_token):
    """Split out so its precision is measured separately from clear noise."""
    record = _write(content=CLOSEOUT)
    _generate(test_client, admin_token)

    proposals = _pending(test_client, admin_token, rule="ticket_closeout")
    assert [p["target_ids"] for p in proposals] == [[record["id"]]]
    assert _pending(test_client, admin_token, rule="episodic_log") == []


def test_duplicate_cluster_keeps_the_newest(test_client, admin_token):
    older = _write(content=DURABLE + " Recorded first.")
    newer = _write(content=DURABLE + " Recorded second.")
    from app.database import get_db

    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET created_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
            (older["id"],),
        )
        conn.commit()

    _generate(test_client, admin_token)
    proposals = _pending(test_client, admin_token, rule="duplicate_cluster")
    assert len(proposals) == 1
    assert proposals[0]["target_ids"] == [older["id"]]
    assert proposals[0]["evidence"]["keep"]["id"] == newer["id"]


def test_stale_volatile_asks_for_confirmation_not_deletion(test_client, admin_token):
    record = _write(content=VOLATILE)
    from app.database import get_db

    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET created_at = '2026-01-01T00:00:00+00:00', "
            "last_confirmed_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
            (record["id"],),
        )
        conn.commit()

    _generate(test_client, admin_token)
    proposals = _pending(test_client, admin_token, rule="stale_volatile")
    assert len(proposals) == 1
    assert proposals[0]["action"] == "confirm"


def test_stale_volatile_leaves_decisions_alone(test_client, admin_token):
    """A decision that quotes a version is still a decision, not a stale fact."""
    record = _write(
        content=(
            "zwave-js-ui image must be pinned, not :latest. The :latest tag drifted to "
            "11.16.0 and broke Home Assistant's Z-Wave JS integration."
        ),
        memory_class="decision",
    )
    from app.database import get_db

    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET created_at = '2026-01-01T00:00:00+00:00', "
            "last_confirmed_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
            (record["id"],),
        )
        conn.commit()

    _generate(test_client, admin_token)
    assert _pending(test_client, admin_token, rule="stale_volatile") == []


def test_accepting_a_retraction_retracts_but_does_not_delete(test_client, admin_token):
    record = _write(content=CLOSEOUT)
    _generate(test_client, admin_token)
    proposal = _pending(test_client, admin_token)[0]

    r = test_client.post(
        f"/api/memory/proposals/{proposal['id']}/decide",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"verdict": "accepted"},
    )
    assert r.status_code == 200, r.json()
    assert r.json()["data"]["proposal"]["applied_count"] == 1

    from app.services.memory_service import get_memory_record

    stored = get_memory_record(record["id"])
    assert stored is not None, "accepting must be recoverable, not destructive"
    assert stored["record_status"] == "retracted"


def test_answering_looks_right_does_not_claim_verification(test_client, admin_token):
    record = _write(content=VOLATILE)
    from app.database import get_db

    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET created_at = '2026-01-01T00:00:00+00:00', "
            "last_confirmed_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
            (record["id"],),
        )
        conn.commit()

    _generate(test_client, admin_token)
    proposal = _pending(test_client, admin_token, rule="stale_volatile")[0]
    test_client.post(
        f"/api/memory/proposals/{proposal['id']}/decide",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"verdict": "accepted"},
    )

    from app.services.memory_service import get_memory_record

    stored = get_memory_record(record["id"])
    assert stored["record_status"] == "active"
    # Reading a record on screen is not checking it against the world, so the
    # confirmation timestamp is left exactly as it was. The queue stops asking
    # because the proposal is decided, not because anyone verified anything.
    assert stored["last_confirmed_at"].startswith("2026-01-01")
    assert _pending(test_client, admin_token, rule="stale_volatile") == []


def _stale_confirm_proposal(test_client, admin_token):
    record = _write(content=VOLATILE)
    from app.database import get_db

    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET created_at = '2026-01-01T00:00:00+00:00', "
            "last_confirmed_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
            (record["id"],),
        )
        conn.commit()
    _generate(test_client, admin_token)
    return record, _pending(test_client, admin_token, rule="stale_volatile")[0]


def test_out_of_date_answer_retracts_the_record(test_client, admin_token):
    """The reviewer must be able to say 'no, it isn't' — not only 'yes' or 'skip'."""
    record, proposal = _stale_confirm_proposal(test_client, admin_token)

    r = test_client.post(
        f"/api/memory/proposals/{proposal['id']}/decide",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"verdict": "accepted", "outcome": "no_longer_current"},
    )
    assert r.status_code == 200, r.json()
    assert r.json()["data"]["proposal"]["applied_count"] == 1

    from app.services.memory_service import get_memory_record

    assert get_memory_record(record["id"])["record_status"] == "retracted"


def test_an_out_of_date_answer_still_counts_the_rule_as_right(test_client, admin_token):
    """Asking was useful whichever way the answer went."""
    _, proposal = _stale_confirm_proposal(test_client, admin_token)
    test_client.post(
        f"/api/memory/proposals/{proposal['id']}/decide",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"verdict": "accepted", "outcome": "no_longer_current"},
    )

    r = test_client.get(
        "/api/memory/proposals/stats",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    stats = {row["rule"]: row for row in r.json()["data"]["rules"]}["stale_volatile"]
    assert stats["accepted"] == 1
    assert stats["precision"] == 1.0


def test_not_worth_keeping_retracts_and_is_recorded_separately(test_client, admin_token):
    """Still accurate, but too vague to act on — a different finding from 'out of date'."""
    import json

    record, proposal = _stale_confirm_proposal(test_client, admin_token)

    r = test_client.post(
        f"/api/memory/proposals/{proposal['id']}/decide",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"verdict": "accepted", "outcome": "not_useful"},
    )
    assert r.status_code == 200, r.json()

    from app.services.memory_service import get_memory_record

    assert get_memory_record(record["id"])["record_status"] == "retracted"

    from app.database import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT details_json FROM audit_log WHERE action = 'memory_proposal_decided' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    # The reason is kept, not flattened into "retracted": it is the only record
    # of why, and the signal a usefulness check would have to learn from.
    assert json.loads(row["details_json"])["outcome"] == "not_useful"


def test_outcome_is_rejected_on_a_retract_proposal(test_client, admin_token):
    _write(content=CLOSEOUT)
    _generate(test_client, admin_token)
    proposal = _pending(test_client, admin_token, rule="ticket_closeout")[0]

    r = test_client.post(
        f"/api/memory/proposals/{proposal['id']}/decide",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"verdict": "accepted", "outcome": "still_current"},
    )
    assert r.status_code == 400


def test_unknown_outcome_is_rejected(test_client, admin_token):
    _, proposal = _stale_confirm_proposal(test_client, admin_token)
    r = test_client.post(
        f"/api/memory/proposals/{proposal['id']}/decide",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"verdict": "accepted", "outcome": "sort_of"},
    )
    assert r.status_code == 400


def test_rejecting_leaves_the_record_alone(test_client, admin_token):
    record = _write(content=CLOSEOUT)
    _generate(test_client, admin_token)
    proposal = _pending(test_client, admin_token)[0]

    r = test_client.post(
        f"/api/memory/proposals/{proposal['id']}/decide",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"verdict": "rejected"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["proposal"]["applied_count"] == 0

    from app.services.memory_service import get_memory_record

    assert get_memory_record(record["id"])["record_status"] == "active"


def test_a_rejected_suggestion_is_not_proposed_again(test_client, admin_token):
    """The queue has to converge, not nag."""
    _write(content=CLOSEOUT)
    _generate(test_client, admin_token)
    proposal = _pending(test_client, admin_token)[0]
    test_client.post(
        f"/api/memory/proposals/{proposal['id']}/decide",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"verdict": "rejected"},
    )

    r = _generate(test_client, admin_token)
    assert r.json()["data"]["created"] == 0
    assert r.json()["data"]["skipped_already_known"] >= 1
    assert _pending(test_client, admin_token) == []


def test_regenerating_does_not_duplicate_pending_proposals(test_client, admin_token):
    _write(content=CLOSEOUT)
    _generate(test_client, admin_token)
    _generate(test_client, admin_token)
    assert len(_pending(test_client, admin_token)) == 1


def test_a_decided_proposal_cannot_be_decided_twice(test_client, admin_token):
    _write(content=CLOSEOUT)
    _generate(test_client, admin_token)
    proposal = _pending(test_client, admin_token)[0]
    for verdict in ("accepted", "rejected"):
        r = test_client.post(
            f"/api/memory/proposals/{proposal['id']}/decide",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"verdict": verdict},
        )
        if verdict == "rejected":
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "INVALID_VERDICT"


def test_unknown_verdict_is_rejected(test_client, admin_token):
    _write(content=CLOSEOUT)
    _generate(test_client, admin_token)
    proposal = _pending(test_client, admin_token)[0]
    r = test_client.post(
        f"/api/memory/proposals/{proposal['id']}/decide",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"verdict": "maybe"},
    )
    assert r.status_code == 400


def test_unknown_rule_is_rejected(test_client, admin_token):
    r = _generate(test_client, admin_token, rules=["not_a_rule"])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UNKNOWN_RULE"


def test_queued_proposals_explain_themselves_in_current_wording(test_client, admin_token):
    """Copy gets revised; a proposal waiting in the queue should not quote old copy."""
    _write(content=CLOSEOUT)
    _generate(test_client, admin_token)

    from app.database import get_db

    with get_db() as conn:
        conn.execute(
            "UPDATE memory_proposals SET rationale = 'Wording from an older release.'"
        )
        conn.commit()

    proposal = _pending(test_client, admin_token)[0]
    assert proposal["rationale"] != "Wording from an older release."
    assert "ticket" in proposal["rationale"].lower()
    assert proposal["prompt"] == "Should agents stop using this memory?"


def test_confirm_proposals_ask_a_plain_question(test_client, admin_token):
    _, proposal = _stale_confirm_proposal(test_client, admin_token)
    assert proposal["prompt"] == "Is this still current?"
    assert "days" in proposal["rationale"]
    # The description says what the memory looks like, not what found it.
    assert "stale_volatile" not in proposal["rule_description"]


def test_stats_report_precision_only_once_decided(test_client, admin_token):
    r = test_client.get(
        "/api/memory/proposals/stats",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.json()
    by_rule = {row["rule"]: row for row in r.json()["data"]["rules"]}
    # An untested rule must not look either perfect or broken.
    assert by_rule["episodic_log"]["precision"] is None

    _write(content=CLOSEOUT)
    _generate(test_client, admin_token)
    proposal = _pending(test_client, admin_token)[0]
    test_client.post(
        f"/api/memory/proposals/{proposal['id']}/decide",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"verdict": "accepted"},
    )

    r2 = test_client.get(
        "/api/memory/proposals/stats",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    stats = {row["rule"]: row for row in r2.json()["data"]["rules"]}["ticket_closeout"]
    assert stats["accepted"] == 1
    assert stats["decided"] == 1
    assert stats["precision"] == 1.0
    assert stats["records_affected"] == 1


def test_scope_filter_limits_generation(test_client, admin_token):
    _write(scope="workspace:a", content=CLOSEOUT)
    _write(scope="workspace:b", content=CLOSEOUT)

    _generate(test_client, admin_token, scope="workspace:a")
    proposals = _pending(test_client, admin_token)
    assert [p["scope"] for p in proposals] == ["workspace:a"]


def test_queue_is_admin_only(test_client, agent_token):
    for method, path in (
        ("get", "/api/memory/proposals"),
        ("get", "/api/memory/proposals/stats"),
    ):
        r = getattr(test_client, method)(
            path, headers={"Authorization": f"Bearer {agent_token}"}
        )
        assert r.status_code in (401, 403), f"{path} was reachable by an agent"

    r = test_client.post(
        "/api/memory/proposals/generate",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={},
    )
    assert r.status_code in (401, 403)


def test_proposals_path_is_not_read_as_a_record_id(test_client, admin_token):
    """GET /api/memory/proposals must not resolve as GET /api/memory/{record_id}."""
    r = test_client.get(
        "/api/memory/proposals", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert r.status_code == 200
    assert "proposals" in r.json()["data"]


def test_decisions_are_audited(test_client, admin_token):
    import json

    _write(content=CLOSEOUT)
    _generate(test_client, admin_token)
    proposal = _pending(test_client, admin_token)[0]
    test_client.post(
        f"/api/memory/proposals/{proposal['id']}/decide",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"verdict": "accepted"},
    )

    from app.database import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT details_json FROM audit_log WHERE action = 'memory_proposal_decided' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    details = json.loads(row["details_json"])
    assert details["verdict"] == "accepted"
    assert details["rule"] == "ticket_closeout"
    assert details["records_affected"] == 1
