import json

from app.database import get_db
from app.services import memory_service, verification_service


def _write(content="The search engine lives in the memory service.", **extra):
    record, _ = memory_service.write_memory(
        content=content,
        memory_class=extra.pop("memory_class", "fact"),
        scope=extra.pop("scope", "workspace:proj"),
        **extra,
    )
    return record


def _set_root(tmp_path, scope="workspace:proj"):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_settings (key, value) VALUES "
            "('workspace_repo_roots', ?)",
            (json.dumps({scope: str(tmp_path)}),),
        )
        conn.commit()


# --- repo anchors ----------------------------------------------------------


def test_a_present_file_verifies_and_records_evidence(clean_db, tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "memory.py").write_text("# code")
    _set_root(tmp_path)
    record = _write(subject_anchor="repo:app/memory.py")

    result = verification_service.verify_record(record)
    assert result["status"] == verification_service.VERIFIED

    stored = memory_service.get_memory_record(record["id"])
    verified = json.loads(stored["provenance_json"])["verified"]
    assert "app/memory.py exists" in verified["evidence"]
    assert memory_service.days_since_confirmed(stored) == 0


def test_a_vanished_file_reports_missing_without_touching_the_record(clean_db, tmp_path):
    _set_root(tmp_path)
    record = _write(subject_anchor="repo:app/deleted.py")

    result = verification_service.verify_record(record)
    assert result["status"] == verification_service.MISSING

    stored = memory_service.get_memory_record(record["id"])
    assert stored["record_status"] == "active", "a missing anchor is evidence, not a verdict"
    assert "verified" not in (stored["provenance_json"] or "")


def test_an_unconfigured_scope_is_unverifiable_not_missing(clean_db):
    """A wrong or absent root would mark a whole workspace stale. Say nothing instead."""
    record = _write(subject_anchor="repo:app/memory.py")
    result = verification_service.verify_record(record)
    assert result["status"] == verification_service.UNVERIFIABLE
    assert "no repository root" in result["detail"]


def test_anchor_paths_cannot_escape_the_workspace_root(clean_db, tmp_path):
    _set_root(tmp_path)
    record = _write(subject_anchor="repo:../../../etc/passwd")
    result = verification_service.verify_record(record)
    assert result["status"] == verification_service.UNVERIFIABLE
    assert "escapes" in result["detail"]


# --- what verification does not claim --------------------------------------


def test_decisions_are_never_verified(clean_db, tmp_path):
    """Nothing in the world can confirm a choice."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "memory.py").write_text("# code")
    _set_root(tmp_path)
    record = _write(
        content="Do not edit the memory service directly.",
        memory_class="decision",
        subject_anchor="repo:app/memory.py",
    )
    result = verification_service.verify_record(record)
    assert result["status"] == verification_service.UNVERIFIABLE
    assert result["detail"] == "not a fact"


def test_records_without_an_anchor_are_unverifiable(clean_db):
    result = verification_service.verify_record(_write())
    assert result["status"] == verification_service.UNVERIFIABLE
    assert result["detail"] == "no subject anchor"


def test_every_builtin_type_has_a_verifier(clean_db):
    """The types this system claims to check itself, it must actually check."""
    from app.services.memory_service import BUILTIN_ANCHOR_TYPES

    assert set(BUILTIN_ANCHOR_TYPES) == set(verification_service.BUILTIN_VERIFIERS)
    assert verification_service.available_anchor_types() >= set(BUILTIN_ANCHOR_TYPES)


# --- service anchors -------------------------------------------------------


def test_a_deleted_binding_reports_missing(clean_db):
    record = _write(subject_anchor="service:no-such-binding")
    result = verification_service.verify_record(record)
    assert result["status"] == verification_service.MISSING


def test_a_failing_service_is_unverifiable_not_missing(clean_db):
    """A service being down does not make the memory about it false."""
    from app.services import connector_service

    connector_type = connector_service.list_connector_types()[0]
    binding = connector_service.create_binding(
        connector_type_id=connector_type["id"],
        scope="workspace:proj",
        name="Probe",
        config_json="{}",
    )
    record = _write(subject_anchor=f"service:{binding['id']}")

    result = verification_service.verify_record(record)
    assert result["status"] == verification_service.UNVERIFIABLE


# --- the pass --------------------------------------------------------------


def test_a_pass_verifies_and_queues_review_for_what_vanished(clean_db, tmp_path):
    (tmp_path / "here.py").write_text("# code")
    _set_root(tmp_path)
    present = _write(content="Ranking lives here.", subject_anchor="repo:here.py")
    gone = _write(content="Ranking used to live there.", subject_anchor="repo:gone.py")

    result = verification_service.verify_scope("workspace:proj")
    assert result["verified"] == 1
    assert result["missing"] == 1
    assert result["proposals_queued"] == 1

    from app.services import memory_proposal_service

    proposals = memory_proposal_service.list_proposals(rule="anchor_missing")
    assert proposals[0]["target_ids"] == [gone["id"]]
    assert "gone.py" in proposals[0]["evidence"]["detail"]
    # The rationale is written for whoever reviews it, not for the machine.
    assert "out of date" in proposals[0]["rationale"]
    assert memory_service.get_memory_record(present["id"])["last_confirmed_at"]


def test_a_repeated_pass_does_not_re_ask(clean_db, tmp_path):
    _set_root(tmp_path)
    _write(subject_anchor="repo:gone.py")

    first = verification_service.verify_scope("workspace:proj")
    second = verification_service.verify_scope("workspace:proj")
    assert first["proposals_queued"] == 1
    assert second["proposals_queued"] == 0


def test_maintenance_runs_the_pass(clean_db, tmp_path):
    from app.services import backup_service

    (tmp_path / "here.py").write_text("# code")
    _set_root(tmp_path)
    _write(subject_anchor="repo:here.py")

    result = backup_service.run_scheduled_maintenance(triggered_by="test")
    assert result["records_verified"] == 1
    assert result["anchors_missing"] == 0


def test_the_pass_can_be_switched_off(clean_db, tmp_path):
    from app.services import backup_service

    (tmp_path / "here.py").write_text("# code")
    _set_root(tmp_path)
    _write(subject_anchor="repo:here.py")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO system_settings (key, value) VALUES ('verification_pass_enabled', '0')"
        )
        conn.commit()

    assert backup_service.run_scheduled_maintenance(triggered_by="test")["records_verified"] == 0


# --- confirmation requires evidence ----------------------------------------


def test_confirmation_without_evidence_is_refused(clean_db):
    record = _write()
    try:
        memory_service.confirm_memory(record["id"], evidence="  ")
        raise AssertionError("expected a refusal")
    except ValueError as exc:
        assert "evidence is required" in str(exc)


def test_mcp_confirm_requires_evidence(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_write",
            "params": {
                "content": "The scheduler ticks every ten minutes.",
                "memory_class": "fact",
                "scope": "agent:testagent",
            },
        },
    )
    record_id = r.json()["data"]["record"]["id"]

    without = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "memory_confirm", "params": {"record_id": record_id}},
    )
    assert without.status_code == 400
    error = without.json()["error"]
    assert error["code"] == "INVALID_PARAMS"
    # The refusal has to teach what evidence means, not just name the field.
    assert "evidence" in error["message"]
    assert "What you looked at" in error["message"]

    with_evidence = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_confirm",
            "params": {"record_id": record_id, "evidence": "read app/scheduler.py"},
        },
    )
    assert with_evidence.status_code == 200, with_evidence.json()


def test_looking_at_a_record_does_not_confirm_it(test_client, admin_token):
    """The review queue's 'looks right' must not claim anyone checked."""
    from app.services import memory_proposal_service

    record = _write(
        content="the assistant is currently served from 127.0.0.1:19119 on the build server.",
        scope="workspace:proj",
    )
    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET created_at = '2026-01-01T00:00:00+00:00', "
            "last_confirmed_at = NULL WHERE id = ?",
            (record["id"],),
        )
        conn.commit()

    memory_proposal_service.generate_proposals(scope="workspace:proj")
    proposal = memory_proposal_service.list_proposals(rule="stale_volatile")[0]
    memory_proposal_service.decide_proposal(
        proposal["id"], "accepted", decided_by="alex", outcome="still_current"
    )

    stored = memory_service.get_memory_record(record["id"])
    assert stored["last_confirmed_at"] is None, "an opinion must not become evidence"
    assert stored["record_status"] == "active"
    # And the queue stops asking, without anyone having claimed a check.
    again = memory_proposal_service.generate_proposals(scope="workspace:proj")
    assert again["created"] == 0


# --- fixing a wrong pointer ------------------------------------------------


def test_a_wrong_pointer_can_be_fixed_instead_of_retracted(clean_db, tmp_path):
    """The middle answer: the memory is good, the pointer was wrong."""
    from app.services import memory_proposal_service

    (tmp_path / "real.py").write_text("# code")
    _set_root(tmp_path)
    record = _write(subject_anchor="repo:app/data/data.db")

    verification_service.verify_scope("workspace:proj")
    proposal = memory_proposal_service.list_proposals(rule="anchor_missing")[0]

    result = memory_proposal_service.reanchor_proposal(
        proposal["id"], "repo:real.py", decided_by="alex"
    )
    assert result["applied_count"] == 1
    assert result["recheck"][0]["status"] == verification_service.VERIFIED

    stored = memory_service.get_memory_record(record["id"])
    assert stored["subject_anchor"] == "repo:real.py"
    assert stored["record_status"] == "active"
    assert stored["last_confirmed_at"], "a fixed pointer that resolves counts as verified"

    provenance = json.loads(stored["provenance_json"])
    assert provenance["anchor_corrected"]["from"] == "repo:app/data/data.db"


def test_fixing_to_another_bad_pointer_says_so(clean_db, tmp_path):
    from app.services import memory_proposal_service

    _set_root(tmp_path)
    _write(subject_anchor="repo:gone.py")
    verification_service.verify_scope("workspace:proj")
    proposal = memory_proposal_service.list_proposals(rule="anchor_missing")[0]

    result = memory_proposal_service.reanchor_proposal(
        proposal["id"], "repo:also-gone.py", decided_by="alex"
    )
    assert result["recheck"][0]["status"] == verification_service.MISSING


def test_reanchor_is_exposed_over_rest_and_mcp(test_client, admin_token, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_write",
            "params": {
                "content": "Ranking floors exact matches above weak semantic hits.",
                "memory_class": "fact",
                "scope": "agent:testagent",
                "subject_anchor": "repo:wrong.py",
            },
        },
    )
    record_id = r.json()["data"]["record"]["id"]

    fixed = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_reanchor",
            "params": {
                "record_id": record_id,
                "subject_anchor": "repo:app/services/memory_service.py",
            },
        },
    )
    assert fixed.status_code == 200, fixed.json()
    assert fixed.json()["data"]["record"]["subject_anchor"] == "repo:app/services/memory_service.py"

    bad = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_reanchor",
            "params": {"record_id": record_id, "subject_anchor": "no-type-prefix"},
        },
    )
    assert bad.status_code == 400


def test_the_review_card_offers_the_fix(test_client, admin_token, tmp_path):
    _set_root(tmp_path, scope="workspace:proj")
    _write(subject_anchor="repo:gone.py")
    verification_service.verify_scope("workspace:proj")

    html = test_client.get(
        "/memory", headers={"Authorization": f"Bearer {admin_token}"}
    ).text
    assert "Fix the pointer" in html
    assert "reanchorProposal(" in html


# --- runtime-looking anchors ------------------------------------------------


def test_a_runtime_path_anchor_is_warned_about_on_write(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_write",
            "params": {
                "content": "The message_count reset bug was fixed on 2026-07-11.",
                "memory_class": "fact",
                "scope": "agent:testagent",
                "subject_anchor": "repo:app/data/data.db",
            },
        },
    )
    assert r.status_code == 201, r.json()
    assert "ANCHOR_LOOKS_RUNTIME" in [w["code"] for w in r.json()["data"].get("warnings", [])]


def test_a_runtime_anchor_gets_a_different_explanation(clean_db, tmp_path):
    from app.services import memory_proposal_service

    _set_root(tmp_path)
    _write(subject_anchor="repo:app/data/data.db")
    verification_service.verify_scope("workspace:proj")

    proposal = memory_proposal_service.list_proposals(rule="anchor_missing")[0]
    assert "probably never the right thing to point at" in proposal["rationale"]


# --- host anchors via connectors -------------------------------------------


def test_a_host_with_no_binding_stays_unverifiable(clean_db):
    record = _write(subject_anchor="host:192.0.2.99")
    result = verification_service.verify_record(record)
    assert result["status"] == verification_service.UNVERIFIABLE
    assert "no connector binding targets" in result["detail"]


def test_an_ambiguous_host_is_not_guessed_at(clean_db):
    from app.services import connector_service

    connector_type = connector_service.list_connector_types()[0]
    for name in ("One", "Two"):
        connector_service.create_binding(
            connector_type_id=connector_type["id"],
            scope="workspace:proj",
            name=name,
            config_json=json.dumps({"base_url": "http://192.0.2.10:8080"}),
        )

    record = _write(subject_anchor="host:192.0.2.10")
    result = verification_service.verify_record(record)
    assert result["status"] == verification_service.UNVERIFIABLE
    assert "ambiguous" in result["detail"]


def test_an_unreachable_host_is_never_reported_as_missing(clean_db):
    """An outage must not manufacture evidence that memories are false."""
    from app.services import connector_service

    connector_type = connector_service.list_connector_types()[0]
    connector_service.create_binding(
        connector_type_id=connector_type["id"],
        scope="workspace:proj",
        name="Unreachable",
        config_json=json.dumps({"base_url": "http://192.0.2.1:9/"}),
    )
    record = _write(subject_anchor="host:192.0.2.1")

    result = verification_service.verify_record(record)
    assert result["status"] == verification_service.UNVERIFIABLE


def test_a_capped_sweep_rotates_instead_of_starving(clean_db, tmp_path):
    """The bug the first unattended run exposed.

    Ordering by last_confirmed_at meant records that can never be confirmed
    sorted to the front every night and ate the whole budget, so records that
    could be re-checked were never reached again.
    """
    (tmp_path / "here.py").write_text("# code")
    _set_root(tmp_path)

    unverifiable = {
        _write(content=f"Host note {i}.", subject_anchor=f"host:10.0.0.{i}")["id"]
        for i in range(3)
    }
    checkable = _write(content="Checkable note.", subject_anchor="repo:here.py")["id"]

    # A budget smaller than the corpus: the unverifiable ones come first.
    first = verification_service.verify_scope("workspace:proj", limit=3)
    assert first["verified"] == 0
    assert {r["record_id"] for r in first["results"]} == unverifiable

    # Next run must move on rather than re-checking the same three.
    second = verification_service.verify_scope("workspace:proj", limit=3)
    assert checkable in {r["record_id"] for r in second["results"]}
    assert second["verified"] == 1


def test_every_attempt_is_recorded_whatever_the_answer(clean_db, tmp_path):
    _set_root(tmp_path)
    record = _write(subject_anchor="host:10.0.0.9")
    verification_service.verify_scope("workspace:proj")

    with get_db() as conn:
        row = conn.execute(
            "SELECT last_verify_attempt_at, last_confirmed_at FROM memory_records WHERE id = ?",
            (record["id"],),
        ).fetchone()
    assert row["last_verify_attempt_at"], "an unverifiable record was still looked at"
    assert row["last_confirmed_at"] is None, "looking is not confirming"



def test_absolute_repo_anchors_are_rejected(clean_db):
    """The same directory has a different absolute path in every container.

    /opt/projects/notes on the host is /host/projects/notes inside another
    agent — an absolute anchor is resolvable only by whoever wrote it, which is
    the opposite of memory that outlives the agent.
    """
    for absolute in (
        "repo:/host/projects/notes/index.md",
        "repo:/opt/projects/example-app/app/main.py",
    ):
        try:
            memory_service.normalize_subject_anchor(absolute)
            raise AssertionError(f"{absolute} should have been rejected")
        except ValueError as exc:
            assert "relative to the workspace root" in str(exc)

    assert (
        memory_service.normalize_subject_anchor("repo:app/main.py") == "repo:app/main.py"
    )


def test_an_anchor_missing_card_asks_the_right_question(clean_db, tmp_path):
    """It is a confirm proposal mechanically, but "is this still current?" is
    the wrong question to put to someone about a file that has vanished."""
    from app.services import memory_proposal_service

    _set_root(tmp_path)
    _write(subject_anchor="repo:gone.py")
    verification_service.verify_scope("workspace:proj")

    proposal = memory_proposal_service.list_proposals(rule="anchor_missing")[0]
    assert proposal["prompt"] == (
        "This points at something that is no longer there — is the memory still worth keeping?"
    )

    stale = memory_proposal_service.list_proposals(rule="stale_volatile")
    for other in stale:
        assert other["prompt"] == "Is this still current?"


# --- verification as a capability, not a fixed set of domains ---------------


def _register_verifier(anchor_type, binding_id="b-1", action="GET /check", **extra):
    import json

    spec = {"binding_id": binding_id, "action": action, **extra}
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_settings (key, value) VALUES "
            "('verification_bindings', ?)",
            (json.dumps({anchor_type: spec}),),
        )
        conn.commit()


def test_an_anchor_type_this_system_never_heard_of_is_accepted(clean_db):
    """What settles a record depends on what the record is about.

    A corpus about a business, a household or a research project has nothing
    resembling a repository path, and the write path must not insist on one.
    """
    for anchor in ("url:https://example.com/policy", "doc:handbook.pdf", "contact:jane-doe"):
        record = _write(subject_anchor=anchor)
        assert record["subject_anchor"] == anchor


def test_an_unknown_type_is_unverifiable_not_wrong(clean_db):
    record = _write(subject_anchor="doc:handbook.pdf")
    result = verification_service.verify_record(record)
    assert result["status"] == verification_service.UNVERIFIABLE
    assert "no verifier" in result["detail"]


def test_writing_an_uncheckable_anchor_says_so_without_refusing(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_write",
            "params": {
                "content": "The refund policy lives in the customer handbook.",
                "memory_class": "fact",
                "scope": "agent:testagent",
                "subject_anchor": "doc:handbook.pdf",
            },
        },
    )
    assert r.status_code == 201, r.json()
    codes = [w["code"] for w in r.json()["data"].get("warnings", [])]
    assert "ANCHOR_TYPE_UNVERIFIABLE" in codes


def test_a_binding_can_teach_it_a_new_anchor_type(clean_db, monkeypatch):
    """The installation supplies the domain; this system supplies the mechanism."""
    from app.services import connector_service

    seen = {}

    def fake(binding_id, action, params=None):
        seen.update({"binding_id": binding_id, "action": action, "params": params})
        return {"success": True}

    monkeypatch.setattr(connector_service, "execute_binding_action_with_logging", fake)
    _register_verifier("url", value_param="url")
    record = _write(subject_anchor="url:https://example.com/policy")

    result = verification_service.verify_record(record)
    assert result["status"] == verification_service.VERIFIED
    assert seen["params"]["url"] == "https://example.com/policy"
    assert memory_service.get_memory_record(record["id"])["last_confirmed_at"]


def test_a_binding_reporting_not_found_is_evidence(clean_db, monkeypatch):
    from app.services import connector_service

    monkeypatch.setattr(
        connector_service,
        "execute_binding_action_with_logging",
        lambda *a, **k: {"success": False, "status_code": 404, "error": "Not Found"},
    )
    _register_verifier("url")
    result = verification_service.verify_record(_write(subject_anchor="url:https://example.com/gone"))
    assert result["status"] == verification_service.MISSING


def test_a_binding_that_merely_fails_is_not_evidence(clean_db, monkeypatch):
    """Same rule as an unreachable host: an outage is not a false memory."""
    from app.services import connector_service

    monkeypatch.setattr(
        connector_service,
        "execute_binding_action_with_logging",
        lambda *a, **k: {"success": False, "error": "connection timed out"},
    )
    _register_verifier("url")
    result = verification_service.verify_record(_write(subject_anchor="url:https://example.com/x"))
    assert result["status"] == verification_service.UNVERIFIABLE


def test_an_incomplete_verifier_registration_is_reported(clean_db):
    import json

    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_settings (key, value) VALUES "
            "('verification_bindings', ?)",
            (json.dumps({"url": {"binding_id": "b-1"}}),),
        )
        conn.commit()
    result = verification_service.verify_record(_write(subject_anchor="url:https://example.com/x"))
    assert result["status"] == verification_service.UNVERIFIABLE
    assert "incompletely configured" in result["detail"]
