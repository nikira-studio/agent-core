import struct
from typing import Optional

from app.config import settings
from app.database import get_db


_vec_available: Optional[bool] = None


def is_sqlite_vec_available() -> bool:
    global _vec_available
    if _vec_available is not None:
        return _vec_available
    try:
        with get_db() as conn:
            conn.execute("SELECT vec0_version()")
        _vec_available = True
    except Exception:
        _vec_available = False
    return _vec_available


def ensure_vector_table() -> bool:
    if not is_sqlite_vec_available():
        return False
    try:
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_embeddings_vec (
                    rowid INTEGER PRIMARY KEY,
                    embedding BLOB NOT NULL
                )
            """)
            conn.commit()
        return True
    except Exception:
        return False


def store_embedding(record_id: str, vector_bytes: bytes) -> bool:
    if not is_sqlite_vec_available():
        return False
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO memory_embeddings (record_id, vector, model, created_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (record_id, vector_bytes, settings.EMBEDDING_MODEL),
            )
            conn.commit()
        return True
    except Exception:
        return False


def get_embedding(record_id: str) -> Optional[bytes]:
    if not is_sqlite_vec_available():
        return None
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT vector FROM memory_embeddings WHERE record_id = ?",
                (record_id,),
            ).fetchone()
        return bytes(row[0]) if row else None
    except Exception:
        return None


def cosine_search_top_k(query_vector: bytes, top_k: int, record_ids: list[str]) -> list[tuple[str, float]]:
    if not is_sqlite_vec_available() or not record_ids:
        return []
    try:
        import array
        query_arr = array.array("f", struct.unpack(f"{len(query_vector)//4}f", query_vector))
        with get_db() as conn:
            placeholders = ",".join(["?" for _ in record_ids])
            rows = conn.execute(
                f"SELECT record_id, vector FROM memory_embeddings WHERE record_id IN ({placeholders})",
                record_ids,
            ).fetchall()

        results = []
        for row in rows:
            try:
                stored_arr = array.array("f", struct.unpack(f"{len(row[1])//4}f", row[1]))
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
        return []
