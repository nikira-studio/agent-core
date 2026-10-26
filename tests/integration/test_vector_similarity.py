"""Stored provider vectors must be comparable before entering recall ranking."""

import struct

from app.database import get_db
from app.services.vector_service import cosine_search_top_k
from app.services.vector_settings_service import save_vector_settings


def test_vector_similarity_rejects_invalid_vectors_and_preserves_ties(clean_db):
    save_vector_settings(model="test")
    with get_db() as conn:
        for id, values in [
            ("b", [1, 0]),
            ("a", [1, 0]),
            ("zero", [0, 0]),
            ("different-model-dimensions", [1, 0, 0]),
            ("nan", [float("nan"), 0]),
            ("infinity", [float("inf"), 0]),
        ]:
            conn.execute(
                "INSERT INTO memory_records(id,content,memory_class,scope,created_at) "
                "VALUES (?, 'vector fixture', 'decision', 'agent:vectors', '2026-01-01')",
                (id,),
            )
            conn.execute(
                "INSERT INTO memory_embeddings(record_id, vector, model) VALUES (?, ?, 'test')",
                (id, struct.pack(f"{len(values)}f", *values)),
            )
        conn.commit()
    ids = ["b", "a", "zero", "different-model-dimensions", "nan", "infinity"]
    assert cosine_search_top_k(struct.pack("2f", 1, 0), 10, ids) == [
        ("a", 1.0),
        ("b", 1.0),
        ("zero", 0.0),
    ]
    for values in ([float("nan"), 0], [float("inf"), 0], []):
        assert (
            cosine_search_top_k(struct.pack(f"{len(values)}f", *values), 10, ids) == []
        )
    assert cosine_search_top_k(struct.pack("2f", 0, 0), 2, ids) == [
        ("a", 0.0),
        ("b", 0.0),
    ]


def test_vector_similarity_excludes_embeddings_from_previous_model(clean_db):
    save_vector_settings(model="current-model")
    vector = struct.pack("2f", 1, 0)
    with get_db() as conn:
        for id, model in [("current", "current-model"), ("old", "previous-model")]:
            conn.execute(
                "INSERT INTO memory_records(id,content,memory_class,scope,created_at) "
                "VALUES (?, 'vector fixture', 'decision', 'agent:vectors', '2026-01-01')",
                (id,),
            )
            conn.execute(
                "INSERT INTO memory_embeddings(record_id,vector,model) VALUES (?,?,?)",
                (id, vector, model),
            )
        conn.commit()
    assert cosine_search_top_k(vector, 10, ["current", "old"]) == [("current", 1.0)]
