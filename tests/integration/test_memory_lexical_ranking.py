"""Ranking regressions measured by the recall corpus."""

import struct

import pytest

from app.database import get_db
from app.services import (
    memory_service,
    embedding_service,
    vector_service,
    vector_settings_service,
)


@pytest.mark.parametrize("hybrid", [False, True])
def test_text_relevance_survives_final_sort_and_importance_distractors(
    clean_db, monkeypatch, hybrid
):
    vector_settings_service.save_vector_settings(enabled=hybrid)
    # Weak semantic matches let lexical evidence decide this exact lookup.
    monkeypatch.setattr(
        embedding_service,
        "generate_embedding",
        lambda text: (
            struct.pack("2f", 1, 0) if text == "scheduler" else struct.pack("2f", 0, 1),
            "healthy",
        ),
    )

    def write(text, importance):
        rec, error = memory_service.write_memory(
            text, "decision", "agent:lexical", importance=importance
        )
        assert error is None
        return rec

    target = write("scheduler scheduler scheduler bounded retries.", 0.1)
    distractors = [
        write(
            "scheduler " + "unrelated discussion about meetings and tasks " * 35, 0.95
        )
        for _ in range(8)
    ]
    # Both within the candidate set and beyond the old importance-selected page.
    for limit in (2, 10):
        rows, mode = memory_service.search_memory(
            "scheduler", ["agent:lexical"], limit=limit
        )
        assert rows[0]["id"] == target["id"]
        assert mode == ("hybrid" if hybrid else "fts_only")
        assert not any(k.startswith("_") for r in rows for k in r)
    assert len(distractors) == 8


def test_semantic_pages_beyond_100_and_no_service_recall_writes(clean_db, monkeypatch):
    vector_settings_service.save_vector_settings(enabled=True)
    vector = struct.pack("2f", 1, 0)
    monkeypatch.setattr(
        embedding_service, "generate_embedding", lambda text: (vector, "healthy")
    )
    with get_db() as conn:
        for index in range(120):
            conn.execute(
                "INSERT INTO memory_records(id,content,memory_class,scope,created_at) VALUES (?,?, 'decision','agent:pages','2026-01-01')",
                (f"page-{index:03}", f"document {index}"),
            )
        conn.commit()
    for index in range(120):
        assert vector_service.store_embedding(f"page-{index:03}", vector)
    rows, mode = memory_service.search_memory(
        "meaningful", ["agent:pages"], limit=5, offset=105
    )
    assert mode == "hybrid"
    assert [r["id"] for r in rows] == [f"page-{i:03}" for i in range(105, 110)]
    with get_db() as conn:
        assert (
            conn.execute("SELECT sum(recall_count) FROM memory_records").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("hybrid", [False, True])
def test_pagination_uses_one_ranking_before_slicing(clean_db, monkeypatch, hybrid):
    vector_settings_service.save_vector_settings(enabled=hybrid)
    vector = struct.pack("2f", 1, 0)
    monkeypatch.setattr(
        embedding_service, "generate_embedding", lambda text: (vector, "healthy")
    )
    with get_db() as conn:
        for index in range(12):
            conn.execute(
                "INSERT INTO memory_records(id,content,memory_class,scope,created_at,importance,helpful_count) "
                "VALUES (?, 'scheduler', 'decision', 'agent:stable-pages', '2026-01-01', ?, ?)",
                (f"stable-{index:02}", index / 12, index),
            )
        conn.commit()
    if hybrid:
        for index in range(12):
            assert vector_service.store_embedding(f"stable-{index:02}", vector)
    whole, _ = memory_service.search_memory(
        "scheduler", ["agent:stable-pages"], limit=12
    )
    pages = []
    for offset in range(12):
        rows, _ = memory_service.search_memory(
            "scheduler", ["agent:stable-pages"], limit=1, offset=offset
        )
        pages.extend(r["id"] for r in rows)
    assert pages == [r["id"] for r in whole]


def test_search_expiry_uses_the_same_clock_as_ranking(clean_db, monkeypatch):
    from datetime import datetime, timezone

    vector_settings_service.save_vector_settings(enabled=False)
    monkeypatch.setattr(
        memory_service, "utc_now", lambda: datetime(2035, 1, 1, tzinfo=timezone.utc)
    )
    with get_db() as conn:
        conn.execute(
            "INSERT INTO memory_records(id,content,memory_class,scope,created_at,expires_at) "
            "VALUES ('expired', 'clock', 'decision', 'agent:clock', '2026-01-01', '2030-01-01')"
        )
        conn.commit()
    rows, _ = memory_service.search_memory("clock", ["agent:clock"])
    assert rows == []


def test_final_page_rechecks_scope_after_ranking(clean_db, monkeypatch):
    from contextlib import contextmanager

    vector_settings_service.save_vector_settings(enabled=False)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO memory_records(id,content,memory_class,scope,created_at) "
            "VALUES ('moving', 'scheduler', 'decision', 'agent:allowed', '2026-01-01')"
        )
        conn.commit()
    original_get_db = memory_service.get_db
    reads = 0

    @contextmanager
    def moving_record():
        nonlocal reads
        with original_get_db() as conn:
            reads += 1
            if reads == 2:
                conn.execute(
                    "UPDATE memory_records SET scope='agent:private' WHERE id='moving'"
                )
                conn.commit()
            yield conn

    monkeypatch.setattr(memory_service, "get_db", moving_record)
    rows, _ = memory_service.search_memory("scheduler", ["agent:allowed"])
    assert rows == []
