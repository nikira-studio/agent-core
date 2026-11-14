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






def _age(record_id, *, created_at, last_confirmed_at=None):
    from app.database import get_db

    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET created_at = ?, last_confirmed_at = ? WHERE id = ?",
            (created_at, last_confirmed_at, record_id),
        )
        conn.commit()








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


def test_the_list_never_nags_about_confirmation(test_client, admin_token):
    """Nobody is asked to confirm memories, so the list shows no "never confirmed" warning and no pin state."""
    _write("An unchecked agent fact")
    html = _page(test_client, admin_token)
    assert "An unchecked agent fact" in html
    assert "never confirmed" not in html and "badge-info'>pinned" not in html


def test_the_list_and_the_detail_use_the_same_words(test_client, admin_token):
    _write("The weekly report goes out on Fridays.")
    page = _page(test_client, admin_token)
    assert "<th>Kind</th>" in page and "<th>Class</th>" not in page
    assert "personal (the owner only)" in page and ">user:" not in page.split("mem-results-body", 1)[1].split("</tbody>", 1)[0]
    # "Written by" says it once: no "(written by the owner)" after "the owner through the API"
    assert "(' + kind + ')'" not in page and "written by the owner" not in page.split("function viewMemory", 1)[1].split("async function _loadChain", 1)[0]
