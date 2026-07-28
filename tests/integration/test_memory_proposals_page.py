CLOSEOUT = (
    "STA-47 closed done 2026-06-21. Built reusable Python client package at "
    "/host/projects/Apps/healthquery/healthquery_client/ (installable as healthquery-client)."
)


def _write(content=CLOSEOUT, scope="workspace:proj", memory_class="fact"):
    from app.services.memory_service import write_memory

    record, _ = write_memory(content=content, memory_class=memory_class, scope=scope)
    return record


def _page(test_client, token):
    r = test_client.get("/memory", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.status_code
    return r.text


def test_review_card_renders_for_admin(test_client, admin_token):
    html = _page(test_client, admin_token)
    assert "Memory Clean-up" in html
    assert "generateProposals()" in html
    assert "Nothing to look at" in html


def test_queued_proposal_is_rendered_with_its_evidence(test_client, admin_token):
    record = _write()
    test_client.post(
        "/api/memory/proposals/generate",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )

    html = _page(test_client, admin_token)
    assert "(1 to look at)" in html
    assert "ticket_closeout" in html
    assert record["id"] in html
    assert "healthquery_client" in html, "operator must see what they are deciding on"
    assert "decideProposal(" in html
    # Buttons must name what happens to the memory, not the verdict recorded.
    assert "Yes, retract it" in html
    assert "Retracted Records below" in html


def test_rule_accuracy_table_is_present(test_client, admin_token):
    html = _page(test_client, admin_token)
    assert "How good these suggestions have been" in html
    assert "episodic_log" in html
    assert "stale_volatile" in html


def test_decided_proposal_leaves_the_queue(test_client, admin_token):
    _write()
    test_client.post(
        "/api/memory/proposals/generate",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    r = test_client.get(
        "/api/memory/proposals", headers={"Authorization": f"Bearer {admin_token}"}
    )
    proposal_id = r.json()["data"]["proposals"][0]["id"]
    test_client.post(
        f"/api/memory/proposals/{proposal_id}/decide",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"verdict": "rejected"},
    )

    html = _page(test_client, admin_token)
    assert "(0 to look at)" in html
    assert proposal_id not in html


def test_review_card_is_hidden_from_non_admins(test_client, clean_db):
    """Pruning is an operator judgement; a regular user should not be offered it."""
    from app.services.auth_service import create_user, create_session

    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    create_user("regular", "user@test.local", "testpassword123", "Regular", "user")
    member_token = create_session("regular", channel="dashboard")["session_id"]

    html = _page(test_client, member_token)
    assert "Memory Clean-up" not in html
    # The affordance, not the script: handler definitions are emitted for every
    # visitor, and the endpoints they call are admin-gated server-side.
    assert 'onclick="generateProposals()"' not in html
    assert 'onclick="decideProposal(' not in html


def test_proposal_content_is_escaped_in_the_page(test_client, admin_token):
    """Record text reaches the review card; it must not reach it as markup."""
    _write(
        content=(
            "STA-99 closed done 2026-06-21. Wrote <script>alert('xss')</script> into "
            "the deployment notes for the reporting service."
        )
    )
    test_client.post(
        "/api/memory/proposals/generate",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )

    html = _page(test_client, admin_token)
    assert "<script>alert('xss')</script>" not in html
    assert "&lt;script&gt;" in html
