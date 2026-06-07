import logging
import re
import secrets
import json
from pathlib import PurePath
from typing import Optional

from app.database import get_db
from app.models.enums import MEMORY_CLASSES, SOURCE_KINDS, normalize_id
from app.security.pii_detector import contains_pii
from app.time_utils import parse_utc_datetime, utc_now, utc_now_iso

try:
    from app.services import embedding_service, vector_service, vector_settings_service

    _EMBEDDING_AVAILABLE = True
except Exception:
    _EMBEDDING_AVAILABLE = False


logger = logging.getLogger(__name__)

FTS5_SPECIAL = re.compile(r'[()^:*?"\'-]|--(.*?)$')
MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S+")

MEMORY_RECORD_COLUMNS = (
    "id, content, memory_class, scope, domain, topic, confidence, importance, "
    "source_kind, event_time, created_at, record_status, superseded_by_id, "
    "supersedes_id, provenance_json, slot_key, valid_from, valid_to, last_confirmed_at, expires_at"
)


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


def _normalize_optional_timestamp(value: Optional[str], field_name: str) -> Optional[str]:
    if value is None or value == "":
        return None
    try:
        return parse_utc_datetime(value).isoformat()
    except Exception as e:
        raise ValueError(f"Invalid {field_name}") from e


def build_provenance(
    *,
    actor_type: str,
    actor_id: str,
    channel: str,
    source_kind: str,
    scope: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    extras: Optional[dict] = None,
) -> str:
    payload = {
        "actor_type": actor_type,
        "actor_id": actor_id,
        "channel": channel,
        "source_kind": source_kind,
        "timestamp": utc_now_iso(),
    }
    if scope:
        payload["scope"] = scope
    if user_id:
        payload["user_id"] = user_id
    if agent_id:
        payload["agent_id"] = agent_id
    if extras:
        payload.update(extras)
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def sanitize_import_filename(filename: str) -> str:
    name = (filename or "notes.txt").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = PurePath(name).name.strip() or "notes.txt"
    return name[:160]


def parse_import_text(
    content: str,
    filename: str,
    *,
    max_chunk_chars: int = 2400,
) -> list[dict]:
    """Split imported notes into deterministic memory-sized chunks."""
    text = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    sections: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        if MARKDOWN_HEADING.match(line) and current:
            sections.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current).strip())

    chunks: list[dict] = []
    source = sanitize_import_filename(filename)

    def add_chunk(chunk_text: str) -> None:
        normalized = chunk_text.strip()
        if normalized:
            chunks.append({"content": normalized, "source_filename": source})

    for section in sections:
        if len(section) <= max_chunk_chars:
            add_chunk(section)
            continue

        buffer = ""
        for paragraph in re.split(r"\n\s*\n", section):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            if len(paragraph) > max_chunk_chars:
                if buffer:
                    add_chunk(buffer)
                    buffer = ""
                for start in range(0, len(paragraph), max_chunk_chars):
                    add_chunk(paragraph[start : start + max_chunk_chars])
                continue
            next_buffer = f"{buffer}\n\n{paragraph}".strip() if buffer else paragraph
            if len(next_buffer) > max_chunk_chars:
                add_chunk(buffer)
                buffer = paragraph
            else:
                buffer = next_buffer
        if buffer:
            add_chunk(buffer)

    return chunks


def _freshness_bonus(record: dict) -> float:
    bonus = 0.0
    now = utc_now()
    try:
        valid_from = (
            parse_utc_datetime(record["valid_from"])
            if record.get("valid_from")
            else None
        )
        valid_to = (
            parse_utc_datetime(record["valid_to"]) if record.get("valid_to") else None
        )
        last_confirmed_at = (
            parse_utc_datetime(record["last_confirmed_at"])
            if record.get("last_confirmed_at")
            else None
        )
    except Exception:
        return 0.0

    if valid_from:
        bonus += 0.02 if valid_from <= now else -0.03
    if valid_to:
        bonus += 0.03 if valid_to >= now else -0.15
    if last_confirmed_at:
        age_days = max((now - last_confirmed_at).total_seconds() / 86400.0, 0.0)
        bonus += max(0.0, 0.06 - min(age_days / 180.0, 0.06))
    return bonus


def _current_record_priority(record: dict) -> int:
    return 0 if record.get("record_status") == "active" else 1


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
    provenance_json: Optional[str] = None,
    slot_key: Optional[str] = None,
    valid_from: Optional[str] = None,
    valid_to: Optional[str] = None,
    last_confirmed_at: Optional[str] = None,
    expires_at: Optional[str] = None,
    allow_pii_shared: bool = False,
) -> tuple[dict, str | None]:
    if memory_class not in MEMORY_CLASSES:
        raise ValueError(f"Invalid memory_class: {memory_class}")
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"Invalid source_kind: {source_kind}")
    if not content.strip():
        raise ValueError("Content cannot be empty")

    normalized_scope = _normalize_scope(scope)
    normalized_slot_key = normalize_id(slot_key) if slot_key else None
    if normalized_slot_key and memory_class != "preference":
        raise ValueError("slot_key is only valid for preference records")

    normalized_valid_from = _normalize_optional_timestamp(valid_from, "valid_from")
    normalized_valid_to = _normalize_optional_timestamp(valid_to, "valid_to")
    normalized_last_confirmed_at = _normalize_optional_timestamp(
        last_confirmed_at, "last_confirmed_at"
    )
    normalized_expires_at = _normalize_optional_timestamp(expires_at, "expires_at")
    if normalized_valid_from and normalized_valid_to:
        if parse_utc_datetime(normalized_valid_to) < parse_utc_datetime(
            normalized_valid_from
        ):
            raise ValueError("valid_to cannot be earlier than valid_from")

    if scope == "shared" and not allow_pii_shared:
        if contains_pii(content):
            return {}, "PII_DETECTED"

    record_id = secrets.token_urlsafe(16)
    now = utc_now_iso()
    slot_supersedes_id = None
    if memory_class == "preference" and normalized_slot_key:
        with get_db() as conn:
            row = conn.execute(
                """
                SELECT id
                FROM memory_records
                WHERE scope = ? AND memory_class = 'preference'
                  AND slot_key = ? AND record_status = 'active'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (normalized_scope, normalized_slot_key),
            ).fetchone()
            if row:
                slot_supersedes_id = row["id"]
    if supersedes_id and slot_supersedes_id and supersedes_id != slot_supersedes_id:
        raise ValueError(
            "slot_key preference writes can only supersede the current active record for that slot"
        )
    effective_supersedes_id = supersedes_id or slot_supersedes_id

    with get_db() as conn:
        if effective_supersedes_id:
            conn.execute(
                "UPDATE memory_records SET record_status = 'superseded', "
                "superseded_by_id = ? WHERE id = ? AND record_status = 'active'",
                (record_id, effective_supersedes_id),
            )

        conn.execute(
            """
            INSERT INTO memory_records
            (id, content, memory_class, scope, domain, topic, confidence, importance,
             source_kind, event_time, created_at, record_status, supersedes_id,
             provenance_json, slot_key, valid_from, valid_to, last_confirmed_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                content,
                memory_class,
                normalized_scope,
                domain,
                topic,
                confidence,
                importance,
                source_kind,
                event_time or now,
                now,
                effective_supersedes_id,
                provenance_json,
                normalized_slot_key,
                normalized_valid_from,
                normalized_valid_to,
                normalized_last_confirmed_at,
                normalized_expires_at,
            ),
        )
        conn.commit()

        if _EMBEDDING_AVAILABLE and vector_settings_service.is_vector_search_enabled():
            try:
                vector_bytes, embed_status = embedding_service.generate_embedding(
                    content
                )
                if vector_bytes is not None:
                    vector_service.store_embedding(record_id, vector_bytes)
            except Exception as e:
                logger.warning(
                    "Vector embedding failed for memory write %s; falling back to FTS: %s",
                    record_id,
                    e,
                )

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
            "supersedes_id": effective_supersedes_id,
            "provenance_json": provenance_json,
            "slot_key": normalized_slot_key,
            "valid_from": normalized_valid_from,
            "valid_to": normalized_valid_to,
            "last_confirmed_at": normalized_last_confirmed_at,
            "expires_at": normalized_expires_at,
        }, None


def get_memory_record(record_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            f"SELECT {MEMORY_RECORD_COLUMNS} "
            "FROM memory_records WHERE id = ?",
            (record_id,),
        ).fetchone()
        return dict(row) if row else None


def retract_memory(record_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE memory_records SET record_status = 'retracted' WHERE id = ? AND record_status = 'active'",
            (record_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def move_memory(
    record_id: str,
    new_scope: str,
    provenance_json: Optional[str] = None,
    allow_pii_shared: bool = False,
) -> tuple[Optional[dict], Optional[str]]:
    """Atomically relocate an active record to ``new_scope``.

    In one transaction: write the record's content into the destination scope
    (preserving class/domain/topic/slot_key/confidence/importance, stamping
    ``moved_from`` provenance and ``supersedes_id`` = original) and retract the
    original (``moved_to`` provenance + ``superseded_by_id`` = new record).

    Returns ``(new_record, None)`` on success, or ``(None|{}, error_code)`` where
    error_code is one of NOT_FOUND, NOT_ACTIVE, SAME_SCOPE, PII_DETECTED.
    """
    old = get_memory_record(record_id)
    if not old:
        return None, "NOT_FOUND"
    if old.get("record_status") != "active":
        return None, "NOT_ACTIVE"

    normalized_new_scope = _normalize_scope(new_scope)
    if normalized_new_scope == old["scope"]:
        return None, "SAME_SCOPE"

    if normalized_new_scope == "shared" and not allow_pii_shared:
        if contains_pii(old.get("content") or ""):
            return {}, "PII_DETECTED"

    new_id = secrets.token_urlsafe(16)
    now = utc_now_iso()

    new_prov: dict = {}
    if provenance_json:
        try:
            new_prov = json.loads(provenance_json)
        except (TypeError, ValueError):
            new_prov = {}
    new_prov["moved_from"] = {
        "record_id": old["id"],
        "scope": old["scope"],
        "moved_at": now,
    }
    new_provenance_json = json.dumps(new_prov)

    old_prov: dict = {}
    if old.get("provenance_json"):
        try:
            old_prov = json.loads(old["provenance_json"])
        except (TypeError, ValueError):
            old_prov = {}
    old_prov["moved_to"] = {
        "record_id": new_id,
        "scope": normalized_new_scope,
        "moved_at": now,
    }
    old_provenance_json = json.dumps(old_prov)

    with get_db() as conn:
        # Re-check the source is still active inside the transaction (race guard).
        row = conn.execute(
            "SELECT record_status FROM memory_records WHERE id = ?",
            (old["id"],),
        ).fetchone()
        if not row or row["record_status"] != "active":
            return None, "NOT_ACTIVE"

        conn.execute(
            """
            INSERT INTO memory_records
            (id, content, memory_class, scope, domain, topic, confidence, importance,
             source_kind, event_time, created_at, record_status, supersedes_id,
             provenance_json, slot_key, valid_from, valid_to, last_confirmed_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id,
                old["content"],
                old["memory_class"],
                normalized_new_scope,
                old.get("domain"),
                old.get("topic"),
                old.get("confidence"),
                old.get("importance"),
                old.get("source_kind"),
                old.get("event_time") or now,
                now,
                old["id"],
                new_provenance_json,
                old.get("slot_key"),
                old.get("valid_from"),
                old.get("valid_to"),
                old.get("last_confirmed_at"),
                old.get("expires_at"),
            ),
        )
        conn.execute(
            "UPDATE memory_records SET record_status = 'retracted', "
            "superseded_by_id = ?, provenance_json = ? "
            "WHERE id = ? AND record_status = 'active'",
            (new_id, old_provenance_json, old["id"]),
        )
        conn.commit()

    if _EMBEDDING_AVAILABLE and vector_settings_service.is_vector_search_enabled():
        try:
            vector_bytes, _ = embedding_service.generate_embedding(old["content"])
            if vector_bytes is not None:
                vector_service.store_embedding(new_id, vector_bytes)
        except Exception as e:
            logger.warning(
                "Vector embedding failed for memory move %s: %s", new_id, e
            )

    return get_memory_record(new_id), None


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
        conn.execute(
            "DELETE FROM memory_embeddings WHERE record_id = ?",
            (record_id,),
        )
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

    status_filter = " AND (mr.expires_at IS NULL OR datetime(mr.expires_at) > datetime('now'))"
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

    if (
        sanitized
        and _EMBEDDING_AVAILABLE
        and vector_settings_service.is_vector_search_enabled()
    ):
        try:
            vector_bytes, embed_status = embedding_service.generate_embedding(query)
            if vector_bytes is not None and vector_service.is_sqlite_vec_available():
                with get_db() as conn:
                    candidate_rows = conn.execute(
                        f"""
                        SELECT mr.{MEMORY_RECORD_COLUMNS.replace(', ', ', mr.')}
                        FROM memory_records mr
                        WHERE mr.scope IN ({scope_placeholders}){status_filter}{extra_sql}
                        """,
                        authorized_scopes + extra_vals,
                    ).fetchall()
                candidate_ids = [r["id"] for r in candidate_rows]
                if candidate_ids:
                    top_k = min(limit * 3, 100)
                    scored = vector_service.cosine_search_top_k(
                        vector_bytes, top_k, candidate_ids
                    )
                    scored_map = {rec_id: score for rec_id, score in scored}
                    semantic_candidates = [
                        dict(r) for r in candidate_rows if r["id"] in scored_map
                    ]
                    for r in semantic_candidates:
                        r["_semantic_score"] = scored_map.get(r["id"], 0.0)
                        r["_freshness_score"] = _freshness_bonus(r)
                    semantic_candidates.sort(
                        key=lambda x: x["_semantic_score"], reverse=True
                    )
                    retrieval_mode = "hybrid"
        except Exception as e:
            logger.warning(
                "Vector embedding failed for memory search; falling back to FTS: %s",
                e,
            )
            retrieval_mode = "fts_only"

    fts_results: list[dict] = []
    if sanitized:
        sql = f"""
            SELECT mr.{MEMORY_RECORD_COLUMNS.replace(', ', ', mr.')}
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
            for r in fts_results:
                r["_freshness_score"] = _freshness_bonus(r)
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
        merged.sort(
            key=lambda x: (
                _current_record_priority(x),
                -(
                    (
                        x.get("_semantic_score", 0.0)
                        if "_semantic_score" in x
                        else (x.get("importance", 0.5) * 0.5)
                    )
                    + x.get("_freshness_score", 0.0)
                ),
                x.get("created_at", ""),
            )
        )
    else:
        merged = fts_results
        merged.sort(
            key=lambda x: (
                _current_record_priority(x),
                -(x.get("importance", 0.5) + x.get("_freshness_score", 0.0)),
                x.get("created_at", ""),
            )
        )

    for r in merged:
        r.pop("_semantic_score", None)
        r.pop("_freshness_score", None)

    start = max(offset, 0)
    end = start + max(limit, 0) if limit >= 0 else None
    return merged[start:end], retrieval_mode


def get_memory_by_scope(
    scope: str,
    limit: int = 50,
    offset: int = 0,
    record_status: Optional[str] = None,
) -> list[dict]:
    normalized_scope = _normalize_scope(scope)
    status_sql = " AND record_status = ?" if record_status else ""
    expires_sql = " AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))"
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT {MEMORY_RECORD_COLUMNS}
            FROM memory_records
            WHERE scope = ?{status_sql}{expires_sql}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (
                [normalized_scope]
                + ([record_status] if record_status else [])
                + [limit, offset]
            ),
        ).fetchall()
        return [dict(row) for row in rows]


def get_memory_by_scopes(
    scopes: list[str],
    limit: int = 50,
    offset: int = 0,
    record_status: Optional[str] = None,
) -> list[dict]:
    if not scopes:
        return []
    placeholders = ",".join(["?" for _ in scopes])
    status_sql = " AND record_status = ?" if record_status else ""
    expires_sql = " AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))"
    params: list = list(scopes) + ([record_status] if record_status else []) + [limit, offset]
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT {MEMORY_RECORD_COLUMNS}
            FROM memory_records
            WHERE scope IN ({placeholders}){status_sql}{expires_sql}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            params,
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
