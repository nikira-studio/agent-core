import pytest


@pytest.fixture(autouse=True)
def _db(clean_db):
    pass


def _make(agent_id, description, memory_scope="workspace:proj", result=None, note=None):
    from app.services.activity_service import create_activity, update_activity

    activity = create_activity(
        agent_id=agent_id,
        user_id="testuser",
        task_description=description,
        memory_scope=memory_scope,
    )
    if result is not None or note is not None:
        update_activity(
            activity["id"],
            status="completed" if result is not None else None,
            task_result=result,
            task_note=note,
        )
    return activity


def test_search_matches_task_description():
    from app.services.activity_service import search_activities

    _make("claude", "Rebuild the vector index")
    _make("codex", "Write the backup docs")

    hits = search_activities("vector")
    assert [h["task_description"] for h in hits] == ["Rebuild the vector index"]


def test_search_matches_task_result_and_note():
    from app.services.activity_service import search_activities

    _make("claude", "Investigate a failure", result="Root cause was a stale CIFS mtime")
    _make("codex", "Unrelated work", note="Touched the pagination helper")

    assert len(search_activities("CIFS")) == 1
    assert len(search_activities("pagination")) == 1


def test_search_finds_other_agents_work():
    """The whole point: one agent finding what a different agent already did."""
    from app.services.activity_service import search_activities

    _make("claude", "Ship the healthquery ingest endpoint")
    hits = search_activities("healthquery")
    assert len(hits) == 1
    assert hits[0]["agent_id"] == "claude"


def test_search_orders_newest_first():
    from app.database import get_db
    from app.services.activity_service import search_activities

    old = _make("claude", "Deploy the search index")
    new = _make("codex", "Deploy the search index again")
    with get_db() as conn:
        conn.execute(
            "UPDATE agent_activity SET started_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
            (old["id"],),
        )
        conn.commit()

    hits = search_activities("deploy")
    assert [h["id"] for h in hits] == [new["id"], old["id"]]


def test_search_filters_by_scope_agent_status_and_since():
    from app.services.activity_service import search_activities

    _make("claude", "Patch the adapter loader", memory_scope="workspace:a")
    _make("codex", "Patch the adapter cache", memory_scope="workspace:b", result="done")

    assert len(search_activities("patch")) == 2
    assert len(search_activities("patch", memory_scope="workspace:a")) == 1
    assert len(search_activities("patch", agent_id="codex")) == 1
    assert len(search_activities("patch", status="completed")) == 1
    assert len(search_activities("patch", since="2026-01-01")) == 2
    assert len(search_activities("patch", since="2099-01-01")) == 0


def test_search_returns_empty_for_untokenizable_query():
    """An operator-only query must return nothing, not silently list everything."""
    from app.services.activity_service import search_activities

    _make("claude", "Some real work")
    assert search_activities("   ") == []
    assert search_activities("!!!") == []


def test_search_survives_fts_operator_syntax():
    from app.services.activity_service import search_activities

    _make("claude", "Handle NEAR misses in search")
    # Would be an fts5 syntax error if the query were passed through raw.
    assert len(search_activities('NEAR("a" "b")')) == 0
    assert len(search_activities('search"')) == 1
    # Tokens are AND-ed, so a bare operator word is matched literally and misses.
    assert len(search_activities('search OR misses')) == 0
    assert len(search_activities('search misses')) == 1


def test_search_index_tracks_updates_and_deletes():
    from app.database import get_db
    from app.services.activity_service import search_activities, update_activity

    activity = _make("claude", "Placeholder description")
    update_activity(activity["id"], task_result="Resolved the zwave regression")
    assert len(search_activities("zwave")) == 1

    with get_db() as conn:
        conn.execute("DELETE FROM agent_activity WHERE id = ?", (activity["id"],))
        conn.commit()
    assert search_activities("zwave") == []


def test_existing_rows_are_backfilled_into_the_index():
    """A trail written before the index existed must become searchable, not stay invisible."""
    from app.database import get_db
    from app.schema import _ensure_activity_fts
    from app.services.activity_service import search_activities

    _make("claude", "Historic work on the edgerouter upgrade")

    with get_db() as conn:
        # Simulate a database that predates the FTS migration.
        conn.executescript(
            """
            DROP TRIGGER IF EXISTS agent_activity_ai;
            DROP TRIGGER IF EXISTS agent_activity_au;
            DROP TRIGGER IF EXISTS agent_activity_ad;
            DROP TABLE IF EXISTS agent_activity_fts;
            """
        )
        conn.commit()

    with get_db() as conn:
        _ensure_activity_fts(conn)

    assert len(search_activities("edgerouter")) == 1
