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

import heapq
import logging
import math
import operator
import struct

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


# SQLite caps the number of ? in one statement (999 on older builds), and a workspace with many
# records can pass far more candidates than that, so they are looked up in pages.
_ID_PAGE = 500


def _scored_rows(conn, record_ids, model, query_arr, norm_q):
    for start in range(0, len(record_ids), _ID_PAGE):
        page = record_ids[start:start + _ID_PAGE]
        placeholders = ",".join("?" * len(page))
        for row in conn.execute(
            f"SELECT record_id, vector FROM memory_embeddings "
            f"WHERE record_id IN ({placeholders}) AND model = ?",
            [*page, model],
        ):
            try:
                stored_arr = struct.unpack(f"{len(row[1]) // 4}f", row[1])
                if len(stored_arr) != len(query_arr):
                    continue
                dot = sum(map(operator.mul, query_arr, stored_arr))
                norm_s = sum(map(operator.mul, stored_arr, stored_arr)) ** 0.5
                if not math.isfinite(norm_s):
                    continue
                similarity = dot / (norm_q * norm_s) if norm_q > 0 and norm_s > 0 else 0.0
                yield (row[0], similarity)
            except Exception:
                continue


def cosine_search_top_k(
    query_vector: bytes, top_k: int, record_ids: list[str]
) -> list[tuple[str, float]]:
    if not record_ids:
        return []
    try:
        query_arr = struct.unpack(f"{len(query_vector) // 4}f", query_vector)
        norm_q = sum(map(operator.mul, query_arr, query_arr)) ** 0.5
        if not query_arr or not math.isfinite(norm_q):
            return []
        with get_db() as conn:
            # Only the best top_k are kept while the rows stream past, not every score.
            return heapq.nsmallest(
                max(top_k, 0),
                _scored_rows(conn, list(record_ids), get_vector_model(), query_arr, norm_q),
                key=lambda item: (-item[1], item[0]),
            )
    except Exception:
        logger.warning("Vector similarity search failed", exc_info=True)
        return []
