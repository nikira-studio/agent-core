import json
import re
import secrets
import struct
from typing import Optional

from app.database import get_db
from app.models.enums import MEMORY_CLASSES, SOURCE_KINDS, RECORD_STATUSES, normalize_id
from app.security.pii_detector import contains_pii
from app.time_utils import utc_now_iso

try:
    from app.services import embedding_service, vector_service
    _EMBEDDING_AVAILABLE = True
except Exception:
    _EMBEDDING_AVAILABLE = False


FTS5_SPECIAL = re.compile(r'[()^:*?"\'-]|--(.*?)$')


def _sanitize_fts_query(query: str) -> str:
    query = query.strip()
    if not query:
        return ""
    query = query[:500]
    cleaned = FTS5_SPECIAL.sub(" ", query)
    tokens = cleaned.split()
    if not tokens:
        return ""
    safe_tokens = [f'"{t}"' for t in tokens if t]
    return " AND ".join(safe_tokens)


def _normalize_scope(scope: str) -> str:
    parts = scope.split(":", 1)
    if len(parts) == 2 and parts[0] in ("user", "agent", "workspace"):
        return f"{parts[0]}:{normalize_id(parts[1])}"
    return scope


def write_memory(
    content: str,
    memory_class: str,
    scope: str,
    domain: Optional[str] = None,
    topic: Optional[str] = None,
    confidence: float = 0.5,
    importance: float = 0.5,
    source_kind: str = "agent_inference",
    event_time: Optional[str] = None,
    supersedes_id: Optional[str] = None,
    allow_pii_shared: bool = False,
) -> tuple[dict, str | None]:
    if memory_class not in MEMORY_CLASSES:
        raise ValueError(f"Invalid memory_class: {memory_class}")
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"Invalid source_kind: {source_kind}")
    if not content.strip():
        raise ValueError("Content cannot be empty")

    normalized_scope = _normalize_scope(scope)

    if scope == "shared" and not allow_pii_shared:
        if contains_pii(content):
            return {}, "PII_DETECTED"

    record_id = secrets.token_urlsafe(16)
    now = utc_now_iso()

    with get_db() as conn:
        if supersedes_id:
            conn.execute(
                "UPDATE memory_records SET record_status = 'superseded', "
                "superseded_by_id = ? WHERE id = ? AND record_status = 'active'",
                (record_id, supersedes_id),
            )

        cursor = conn.execute(
            """
            INSERT INTO memory_records
            (id, content, memory_class, scope, domain, topic, confidence, importance,
             source_kind, event_time, created_at, record_status, supersedes_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
            """,
            (record_id, content, memory_class, normalized_scope, domain, topic,
             confidence, importance, source_kind, event_time or now, now, supersedes_id),
        )
        conn.commit()

        if _EMBEDDING_AVAILABLE:
            try:
                vector_bytes, embed_status = embedding_service.generate_embedding(content)
                if vector_bytes is not None:
                    vector_service.store_embedding(record_id, vector_bytes)
            except Exception:
                pass

        return {
            "id": record_id,
            "content": content,
            "memory_class": memory_class,
            "scope": normalized_scope,
            "domain": domain,
            "topic": topic,
            "confidence": confidence,
            "importance": importance,
            "source_kind": source_kind,
            "event_time": event_time,
            "record_status": "active",
            "supersedes_id": supersedes_id,
        }, None


def get_memory_record(record_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, content, memory_class, scope, domain, topic, confidence, importance, "
            "source_kind, event_time, created_at, record_status, superseded_by_id, supersedes_id "
            "FROM memory_records WHERE id = ?",
            (record_id,),
        ).fetchone()
        return dict(row) if row else None


def retract_memory(record_id: str, retracted_by: Optional[str] = None) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE memory_records SET record_status = 'retracted' WHERE id = ? AND record_status = 'active'",
            (record_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def restore_memory(record_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE memory_records SET record_status = 'active' WHERE id = ? AND record_status = 'retracted'",
            (record_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_memory_hard(record_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM memory_records WHERE id = ?", (record_id,))
        conn.commit()
        return cursor.rowcount > 0


def search_memory(
    query: str,
    authorized_scopes: list[str],
    domain: Optional[str] = None,
    topic: Optional[str] = None,
    memory_class: Optional[str] = None,
    min_confidence: float = 0.0,
    limit: int = 20,
    offset: int = 0,
    include_retracted: bool = False,
    include_superseded: bool = False,
) -> tuple[list[dict], str]:
    sanitized = _sanitize_fts_query(query)

    status_filter = ""
    if not include_retracted:
        status_filter += " AND mr.record_status != 'retracted'"
    if not include_superseded:
        status_filter += " AND mr.record_status != 'superseded'"

    scope_placeholders = ",".join(["?" for _ in authorized_scopes])
    params = [sanitized] + authorized_scopes
    extra = []
    extra_vals = []
    if domain:
        extra.append("mr.domain = ?")
        extra_vals.append(domain)
    if topic:
        extra.append("mr.topic = ?")
        extra_vals.append(topic)
    if memory_class:
        extra.append("mr.memory_class = ?")
        extra_vals.append(memory_class)
    if min_confidence > 0:
        extra.append("mr.confidence >= ?")
        extra_vals.append(min_confidence)

    extra_sql = " AND " + " AND ".join(extra) if extra else ""

    retrieval_mode = "fts_only"
    semantic_candidates: list[dict] = []

    if sanitized and _EMBEDDING_AVAILABLE:
        try:
            vector_bytes, embed_status = embedding_service.generate_embedding(query)
            if vector_bytes is not None and vector_service.is_sqlite_vec_available():
                with get_db() as conn:
                    candidate_rows = conn.execute(
                        f"""
                        SELECT mr.id, mr.content, mr.memory_class, mr.scope, mr.domain, mr.topic,
                               mr.confidence, mr.importance, mr.source_kind, mr.event_time,
                               mr.created_at, mr.record_status, mr.superseded_by_id, mr.supersedes_id
                        FROM memory_records mr
                        WHERE mr.scope IN ({scope_placeholders}){status_filter}{extra_sql}
                        """,
                        authorized_scopes + extra_vals,
                    ).fetchall()
                candidate_ids = [r["id"] for r in candidate_rows]
                if candidate_ids:
                    top_k = min(limit * 3, 100)
                    scored = vector_service.cosine_search_top_k(vector_bytes, top_k, candidate_ids)
                    scored_map = {rec_id: score for rec_id, score in scored}
                    semantic_candidates = [
                        dict(r) for r in candidate_rows if r["id"] in scored_map
                    ]
                    for r in semantic_candidates:
                        r["_semantic_score"] = scored_map.get(r["id"], 0.0)
                    semantic_candidates.sort(key=lambda x: x["_semantic_score"], reverse=True)
                    retrieval_mode = "hybrid"
        except Exception:
            retrieval_mode = "fts_only"

    fts_results: list[dict] = []
    if sanitized:
        sql = f"""
            SELECT mr.id, mr.content, mr.memory_class, mr.scope, mr.domain, mr.topic,
                   mr.confidence, mr.importance, mr.source_kind, mr.event_time,
                   mr.created_at, mr.record_status, mr.superseded_by_id, mr.supersedes_id
            FROM memory_records mr
            JOIN memory_records_fts fts ON fts.rowid = mr.rowid
            WHERE fts.content MATCH ? AND mr.scope IN ({scope_placeholders}){status_filter}{extra_sql}
            ORDER BY mr.importance DESC, mr.created_at DESC
            LIMIT ? OFFSET ?
        """
        fts_params = params + extra_vals + [limit, offset]
        with get_db() as conn:
            fts_rows = conn.execute(sql, fts_params).fetchall()
            fts_results = [dict(row) for row in fts_rows]
    elif not semantic_candidates:
        return [], "fts_only"

    merged: list[dict] = []
    seen_ids = set()
    if retrieval_mode == "hybrid" and semantic_candidates:
        for r in semantic_candidates:
            if r["id"] not in seen_ids:
                merged.append(r)
                seen_ids.add(r["id"])
        for r in fts_results:
            if r["id"] not in seen_ids:
                merged.append(r)
                seen_ids.add(r["id"])
        merged.sort(key=lambda x: x.get("_semantic_score", 0.0) if "_semantic_score" in x else (x.get("importance", 0.5) * 0.5), reverse=True)
    else:
        merged = fts_results

    for r in merged:
        r.pop("_semantic_score", None)

    return merged, retrieval_mode


def get_memory_by_scope(
    scope: str,
    limit: int = 50,
    offset: int = 0,
    record_status: Optional[str] = None,
) -> list[dict]:
    normalized_scope = _normalize_scope(scope)
    status_sql = " AND record_status = ?" if record_status else ""
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT id, content, memory_class, scope, domain, topic, confidence, importance,
                   source_kind, event_time, created_at, record_status, superseded_by_id, supersedes_id
            FROM memory_records
            WHERE scope = ?{status_sql}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            ([normalized_scope] + ([record_status] if record_status else []) + [limit, offset]),
        ).fetchall()
        return [dict(row) for row in rows]


def get_supersession_chain(record_id: str) -> list[dict]:
    current = get_memory_record(record_id)
    if not current:
        return []

    seen_before = set()
    before = []
    while current and current.get("supersedes_id") and current["id"] not in seen_before:
        seen_before.add(current["id"])
        previous = get_memory_record(current["supersedes_id"])
        if not previous:
            break
        before.append(previous)
        current = previous

    chain = list(reversed(before))
    seen = {record["id"] for record in chain}
    current = get_memory_record(record_id)
    while current and current["id"] not in seen:
        seen.add(current["id"])
        chain.append(current)
        next_id = current.get("superseded_by_id")
        current = get_memory_record(next_id) if next_id else None

    return chain
