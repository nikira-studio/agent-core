"""Standing context: records loaded at session start rather than retrieved.

The failure this addresses is specific — a constraint has to win a search to be
seen, and a constraint that loses is the same as no constraint. Pinning is a
property the rest of the system respects, so most of these tests are about the
interactions rather than the flag itself.
"""

import pytest

from app.database import get_db
from app.services import memory_proposal_service, memory_service

RULE = "Do not edit vendored dependencies directly; they are refreshed from upstream."


def _write(content=RULE, memory_class="decision", scope="workspace:proj"):
    record, _ = memory_service.write_memory(
        content=content, memory_class=memory_class, scope=scope
    )
    return record


def test_pinning_makes_a_record_load_without_searching(clean_db):
    record = _write()
    memory_service.set_pinned(record["id"], True)

    standing = memory_service.pinned_records(["workspace:proj"])
    assert [r["id"] for r in standing] == [record["id"]]


def test_unpinning_removes_it(clean_db):
    record = _write()
    memory_service.set_pinned(record["id"], True)
    memory_service.set_pinned(record["id"], False)
    assert memory_service.pinned_records(["workspace:proj"]) == []


def test_only_durable_classes_can_be_pinned(clean_db):
    """A scratchpad is temporary by definition; standing context is not."""
    scratch = _write(content="Temporary note.", memory_class="scratchpad")
    with pytest.raises(ValueError, match="can be pinned"):
        memory_service.set_pinned(scratch["id"], True)


def test_the_list_is_capped_so_it_stays_readable(clean_db):
    """An uncapped pin list becomes a second unranked copy of the corpus."""
    limit = memory_service.pinned_limit()
    for i in range(limit):
        memory_service.set_pinned(_write(content=f"Standing rule {i}.")["id"], True)

    one_too_many = _write(content="One rule too many.")
    with pytest.raises(ValueError, match="already has"):
        memory_service.set_pinned(one_too_many["id"], True)

    assert len(memory_service.pinned_records(["workspace:proj"])) == limit


def test_the_cap_is_per_scope(clean_db):
    limit = memory_service.pinned_limit()
    for i in range(limit):
        memory_service.set_pinned(_write(content=f"Rule {i}.")["id"], True)

    other = _write(content="A rule in another workspace.", scope="workspace:other")
    assert memory_service.set_pinned(other["id"], True) is not None


def test_pinning_is_scope_filtered_on_read(clean_db):
    mine = _write(content="Visible rule.")
    theirs = _write(content="Rule in another scope.", scope="workspace:other")
    memory_service.set_pinned(mine["id"], True)
    memory_service.set_pinned(theirs["id"], True)

    visible = memory_service.pinned_records(["workspace:proj"])
    assert [r["id"] for r in visible] == [mine["id"]]


# --- the interactions that make it cohesive rather than a bolt-on -----------


def test_cleanup_rules_never_propose_a_pinned_record(clean_db):
    """The operator already answered the question these rules ask."""
    closeout = _write(
        content=(
            "STA-47 closed done 2026-06-21. Built a reusable client package at "
            "clients/reporting/ (installable as reporting-client)."
        ),
        memory_class="fact",
    )
    memory_proposal_service.generate_proposals(scope="workspace:proj")
    assert memory_proposal_service.list_proposals(rule="ticket_closeout"), "precondition"

    with get_db() as conn:
        conn.execute("DELETE FROM memory_proposals")
        conn.commit()

    memory_service.set_pinned(closeout["id"], True)
    memory_proposal_service.generate_proposals(scope="workspace:proj")
    assert memory_proposal_service.list_proposals(status="pending") == []


def test_the_usefulness_reviewer_skips_pinned_records(clean_db):
    """No model call, and no cost, on something already judged by a person."""
    from app.services import usefulness_service

    vague = _write(
        content="On 2026-05-16 the deploy failed and was traced to a missing dependency.",
        memory_class="fact",
    )
    assert usefulness_service.candidates("workspace:proj"), "precondition"

    memory_service.set_pinned(vague["id"], True)
    assert usefulness_service.candidates("workspace:proj") == []


def test_a_handoff_briefing_carries_standing_context(clean_db):
    from app.services import activity_service, briefing_service

    activity = activity_service.create_activity(
        agent_id="agent-a", user_id="alex",
        task_description="Refactor the ingest path", memory_scope="workspace:proj",
    )
    memory_service.set_pinned(_write()["id"], True)

    briefing = briefing_service.generate_handoff_briefing(
        activity["id"],
        requesting_agent_id="agent-b",
        requesting_user_id="alex",
        authorized_scopes=["workspace:proj"],
        is_admin=True,
    )
    assert any("vendored dependencies" in r["content"] for r in briefing["pinned"])


def test_pinned_state_is_visible_in_results(clean_db):
    record = _write()
    memory_service.set_pinned(record["id"], True)
    lean = memory_service.lean_record(memory_service.get_memory_record(record["id"]))
    assert lean["pinned"] == 1


def test_a_pinned_fact_is_still_verified(clean_db, tmp_path):
    """Pinning says "always show this", not "stop checking it"."""
    import json

    from app.services import verification_service

    (tmp_path / "config.py").write_text("# code")
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_settings (key, value) VALUES ('workspace_repo_roots', ?)",
            (json.dumps({"workspace:proj": str(tmp_path)}),),
        )
        conn.commit()

    record, _ = memory_service.write_memory(
        content="The ingest timeout lives in config.py.",
        memory_class="fact",
        scope="workspace:proj",
        subject_anchor="repo:config.py",
    )
    memory_service.set_pinned(record["id"], True)

    result = verification_service.verify_scope("workspace:proj")
    assert result["verified"] == 1


# --- the tool surface ------------------------------------------------------


def test_the_full_request_and_grant_flow(test_client, agent_token):
    write = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_write",
            "params": {"content": RULE, "memory_class": "decision", "scope": "agent:testagent"},
        },
    )
    record_id = write.json()["data"]["record"]["id"]

    empty = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "memory_pinned", "params": {}},
    )
    assert empty.json()["data"]["count"] == 0

    pin = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "memory_pin", "params": {"record_id": record_id}},
    )
    assert pin.status_code == 200, pin.json()
    # Nothing is standing context until an operator says so.
    proposal = memory_proposal_service.list_proposals(rule="pin_request")[0]
    memory_proposal_service.decide_proposal(proposal["id"], "accepted", decided_by="alex")

    loaded = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "memory_pinned", "params": {}},
    )
    assert [r["id"] for r in loaded.json()["data"]["records"]] == [record_id]


def test_pinning_needs_write_access(test_client, agent_token, admin_token):
    record, _ = memory_service.write_memory(
        content=RULE, memory_class="decision", scope="user:admin"
    )
    test_client.put(
        "/api/agents/testagent",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"read_scopes": ["user:admin"], "write_scopes": []},
    )
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "memory_pin", "params": {"record_id": record["id"]}},
    )
    assert r.status_code == 403


def test_granting_past_the_cap_is_refused_visibly(test_client, admin_token):
    """The operator must not be told they granted something that did not happen."""
    limit = memory_service.pinned_limit()
    for i in range(limit):
        memory_service.set_pinned(_write(content=f"Rule {i}.")["id"], True)

    extra = _write(content="One rule too many.")
    memory_proposal_service.queue_proposal(
        rule="pin_request",
        action="pin",
        scope="workspace:proj",
        target_ids=[extra["id"]],
        evidence={"pin": True, "requested_by": "agent-a"},
    )
    proposal = memory_proposal_service.list_proposals(rule="pin_request")[0]

    r = test_client.post(
        f"/api/memory/proposals/{proposal['id']}/decide",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"verdict": "accepted"},
    )
    assert r.status_code == 400
    assert "Unpin something first" in r.json()["error"]["message"]
    # Still pending, because nothing was applied.
    assert memory_proposal_service.get_proposal(proposal["id"])["status"] == "pending"
    assert memory_service.get_memory_record(extra["id"])["pinned"] == 0


# --- who gets to decide what every session sees ----------------------------


def test_an_agent_cannot_pin_by_itself(test_client, agent_token):
    """Standing context reaches every session in a scope, including other
    agents'. That makes it the most influential thing an agent could write, so
    asking and granting are separated."""
    write = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_write",
            "params": {"content": RULE, "memory_class": "decision", "scope": "agent:testagent"},
        },
    )
    record_id = write.json()["data"]["record"]["id"]

    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "memory_pin", "params": {"record_id": record_id}},
    )
    assert r.status_code == 200, r.json()
    assert r.json()["data"]["queued"] is True

    assert memory_service.get_memory_record(record_id)["pinned"] == 0
    loaded = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "memory_pinned", "params": {}},
    )
    assert loaded.json()["data"]["count"] == 0


def test_the_request_reaches_the_review_queue(test_client, agent_token, admin_token):
    write = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_write",
            "params": {"content": RULE, "memory_class": "decision", "scope": "agent:testagent"},
        },
    )
    record_id = write.json()["data"]["record"]["id"]
    test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "memory_pin", "params": {"record_id": record_id}},
    )

    proposals = memory_proposal_service.list_proposals(rule="pin_request")
    assert [p["target_ids"] for p in proposals] == [[record_id]]
    assert "yours to grant" in proposals[0]["rationale"]

    memory_proposal_service.decide_proposal(
        proposals[0]["id"], "accepted", decided_by="alex"
    )
    assert memory_service.get_memory_record(record_id)["pinned"] == 1


def test_declining_leaves_it_unpinned(test_client, agent_token):
    record = _write(scope="agent:testagent")
    test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "memory_pin", "params": {"record_id": record["id"]}},
    )
    proposal = memory_proposal_service.list_proposals(rule="pin_request")[0]
    memory_proposal_service.decide_proposal(proposal["id"], "rejected", decided_by="alex")
    assert memory_service.get_memory_record(record["id"])["pinned"] == 0


def test_an_agent_cannot_quietly_remove_a_standing_rule(test_client, agent_token):
    """Unpinning is how a constraint stops applying, so it is reviewed too."""
    record = _write(scope="agent:testagent")
    memory_service.set_pinned(record["id"], True)

    test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "memory_pin", "params": {"record_id": record["id"], "pinned": False}},
    )
    assert memory_service.get_memory_record(record["id"])["pinned"] == 1

    proposal = memory_proposal_service.list_proposals(rule="pin_request")[0]
    assert "stop showing" in proposal["rationale"]
    memory_proposal_service.decide_proposal(proposal["id"], "accepted", decided_by="alex")
    assert memory_service.get_memory_record(record["id"])["pinned"] == 0


def test_an_operator_still_pins_directly(clean_db):
    """The service call an operator surface uses is unchanged."""
    record = _write()
    assert memory_service.set_pinned(record["id"], True) is not None
    assert memory_service.get_memory_record(record["id"])["pinned"] == 1
