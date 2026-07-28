"""Embedding storage and similarity, in plain SQLite.

Vectors live in `memory_embeddings` as float32 blobs and similarity is computed
in Python. That is fast enough at this scale — the candidate set is already
narrowed by scope and status before anything is scored — and it means semantic
search works on any SQLite build.

There was previously a `vec_version()` probe gating every function here, left
over from an approach that stored vectors in a sqlite-vec virtual table. Nothing
in this module calls a vec_* function any more, so the probe only had the effect
of silently disabling semantic search on installs without the extension.
"""

import logging
import struct
from typing import Optional

from app.database import get_db
from app.services.vector_settings_service import get_vector_model

logger = logging.getLogger(__name__)


def store_embedding(record_id: str, vector_bytes: bytes) -> bool:
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO memory_embeddings (record_id, vector, model, created_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (record_id, vector_bytes, get_vector_model()),
            )
            conn.commit()
        return True
    except Exception:
        # Never fatal to a memory write, but never silent either: a store that
        # keeps failing means search quietly degrades to text matching, which is
        # exactly the kind of fault that hides for months.
        logger.warning("Could not store embedding for %s", record_id, exc_info=True)
        return False


def get_embedding(record_id: str) -> Optional[bytes]:
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT vector FROM memory_embeddings WHERE record_id = ?",
                (record_id,),
            ).fetchone()
        return bytes(row[0]) if row else None
    except Exception:
        logger.warning("Could not read embedding for %s", record_id, exc_info=True)
        return None


def cosine_search_top_k(
    query_vector: bytes, top_k: int, record_ids: list[str]
) -> list[tuple[str, float]]:
    if not record_ids:
        return []
    try:
        import array

        query_arr = array.array(
            "f", struct.unpack(f"{len(query_vector) // 4}f", query_vector)
        )
        with get_db() as conn:
            placeholders = ",".join(["?" for _ in record_ids])
            rows = conn.execute(
                f"SELECT record_id, vector FROM memory_embeddings WHERE record_id IN ({placeholders})",
                record_ids,
            ).fetchall()

        results = []
        for row in rows:
            try:
                stored_arr = array.array(
                    "f", struct.unpack(f"{len(row[1]) // 4}f", row[1])
                )
                dot = sum(q * s for q, s in zip(query_arr, stored_arr))
                norm_q = sum(x * x for x in query_arr) ** 0.5
                norm_s = sum(x * x for x in stored_arr) ** 0.5
                if norm_q > 0 and norm_s > 0:
                    similarity = dot / (norm_q * norm_s)
                else:
                    similarity = 0.0
                results.append((row[0], similarity))
            except Exception:
                continue

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]
    except Exception:
        logger.warning("Vector similarity search failed", exc_info=True)
        return []
