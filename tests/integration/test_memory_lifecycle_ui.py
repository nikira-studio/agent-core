"""The memory list should show what the system actually acts on.

The list gave a column to `confidence` — a number the writing agent assigns to
itself, which ranking ignores. Meanwhile the fields that decide how a record is
treated (is it standing context, can anything verify it, when did anyone last
check) appeared nowhere. The page was showing the one score that means least.
"""

from app.services import memory_service

SCOPE = "workspace:proj"


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _workspace(client, token):
    """The page lists only scopes the viewer can reach, so the records under
    test have to live somewhere this admin actually sees."""
    client.post(
        "/api/workspaces",
        headers=_headers(token),
        json={"id": "proj", "name": "Proj", "description": "x"},
    )


def _write(content, **extra):
    record, _ = memory_service.write_memory(
        content=content,
        memory_class=extra.pop("memory_class", "fact"),
        scope=SCOPE,
        **extra,
    )
    return record


def _page(client, token):
    _workspace(client, token)
    r = client.get("/memory", headers=_headers(token))
    assert r.status_code == 200
    return r.text


def test_the_list_no_longer_leads_with_a_self_assigned_score(test_client, admin_token):
    _write("The ingest route is POST /api/webhook/health.")
    page = _page(test_client, admin_token)
    assert "<th>State</th>" in page
    assert "<th>Confidence</th>" not in page, (
        "ranking ignores confidence; the column implied otherwise"
    )


def test_an_anchored_record_says_so(test_client, admin_token):
    _write(
        "The ingest route is POST /api/webhook/health.",
        subject_anchor="repo:app/routes/webhooks.py",
    )
    assert "anchored" in _page(test_client, admin_token)


def test_a_fact_nobody_has_checked_is_visible_as_such(test_client, admin_token):
    _write("The build server is 192.0.2.10.")
    assert "never confirmed" in _page(test_client, admin_token)


def test_a_confirmed_fact_shows_how_long_ago(test_client, admin_token):
    record = _write("The build server is 192.0.2.10.")
    memory_service.confirm_memory(
        record["id"], evidence="checked the host responds on port 22"
    )
    page = _page(test_client, admin_token)
    assert "confirmed today" in page
    assert "never confirmed" not in page


def _age(record_id, *, created_at, last_confirmed_at=None):
    from app.database import get_db

    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET created_at = ?, last_confirmed_at = ? WHERE id = ?",
            (created_at, last_confirmed_at, record_id),
        )
        conn.commit()


def test_a_fact_confirmed_long_ago_is_flagged(test_client, admin_token):
    """Age only matters for facts, and only past the point of usefulness."""
    record = _write("The build server is 192.0.2.10.")
    _age(
        record["id"],
        created_at="2024-01-01T00:00:00+00:00",
        last_confirmed_at="2025-01-01T00:00:00+00:00",
    )

    page = _page(test_client, admin_token)
    assert "badge-warning" in page, "a fact unchecked for a year should stand out"
    assert "confirmed " in page


def test_a_fact_never_confirmed_is_flagged_once_it_is_old(test_client, admin_token):
    """The case the previous test claimed to cover but did not.

    Setting last_confirmed_at makes a record an *old confirmed* fact. A fact
    nobody has ever checked was staying muted forever, even though ranking
    ages it from created_at exactly like any other.
    """
    record = _write("The build server is 192.0.2.10.")
    _age(record["id"], created_at="2024-01-01T00:00:00+00:00")

    page = _page(test_client, admin_token)
    assert "never confirmed," in page, "an old unverified fact should show its age"
    assert "badge-warning" in page


def test_a_recent_unconfirmed_fact_is_not_alarming(test_client, admin_token):
    """Written yesterday and unchecked is normal, not a problem."""
    _write("The build server is 192.0.2.10.")
    page = _page(test_client, admin_token)
    assert "never confirmed" in page
    assert "badge-warning" not in page


def test_the_anchor_badge_does_not_claim_a_check_happened(test_client, admin_token):
    """An anchor says what could settle the record, not that anything has."""
    _write(
        "The ingest route is POST /api/webhook/health.",
        subject_anchor="repo:app/routes/webhooks.py",
    )
    page = _page(test_client, admin_token)
    assert "Can be checked against" in page
    assert "Checked against" not in page.replace("Can be checked against", "")


def test_a_decision_is_not_nagged_about_confirmation(test_client, admin_token):
    """A decision does not go stale because time passed."""
    _write("Do not edit vendored dependencies directly.", memory_class="decision")
    page = _page(test_client, admin_token)
    assert "never confirmed" not in page


def test_pinned_records_are_marked(test_client, admin_token):
    record = _write("Always run the migration before deploying.", memory_class="decision")
    memory_service.set_pinned(record["id"], True)
    assert "pinned" in _page(test_client, admin_token)


def test_the_detail_pane_shows_the_anchor_and_pin_state(test_client, admin_token):
    page = _page(test_client, admin_token)
    assert 'id="mem-detail-anchor"' in page
    assert 'id="mem-detail-pinned"' in page
    assert "nothing can verify this" in page, (
        "an unanchored fact should say so rather than showing an empty field"
    )


def test_version_history_dates_are_not_printed_as_markup(test_client, admin_token):
    """localDt returns an element; escaping the whole metadata array printed it.

    Topic and status are record data and must still be escaped — only the
    trusted element is exempt.
    """
    page = _page(test_client, admin_token)
    assert "metadataParts.push(localDt(r.created_at))" in page
    assert "metadataParts.map(escapeHtml)" not in page
    assert "escapeHtml(r.topic)" in page
    assert "escapeHtml(r.record_status)" in page
