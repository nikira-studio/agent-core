import logging
import re
import secrets
import json
import sqlite3
from datetime import timedelta
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
    "id, content, memory_class, scope, topic, confidence, importance, "
    "source_kind, created_at, record_status, superseded_by_id, "
    "supersedes_id, provenance_json, slot_key, valid_from, valid_to, last_confirmed_at, "
    "expires_at, status_changed_at, subject_anchor, recall_count, last_recalled_at, "
    "helpful_count, unhelpful_count, last_verify_attempt_at, pinned"
)

# What a search result actually needs to be useful to the caller. Everything
# else is bookkeeping the reader cannot act on: self-assigned scores that do not
# vary, provenance blobs, and lifecycle columns that only matter when inspecting one
# record. On a 20-result page that bookkeeping was 37% of the payload against
# 35% for the content itself — charged to the context window memory exists to
# protect. Full records remain one memory_get away.
MEMORY_RESULT_FIELDS = (
    "id",
    "content",
    "memory_class",
    "scope",
    "topic",
    "subject_anchor",
    "created_at",
    "last_confirmed_at",
    "record_status",
    "pinned",
)

# How long a record keeps full standing before age starts counting against it.
# Beyond this it is not treated as wrong — just as something nobody has checked
# in a while, which the reader is told outright rather than left to infer from a
# frozen confidence of 0.95.
#
# Deliberately later than the 45 days at which the clean-up queue asks about a
# volatile fact: the operator gets asked first, and ranking only starts pushing
# a record down if that question went unanswered for another six weeks.
CONFIRMATION_HALF_LIFE_DAYS = 90


def days_since_confirmed(record: dict) -> Optional[int]:
    reference = record.get("last_confirmed_at") or record.get("created_at")
    if not reference:
        return None
    try:
        return max(int((utc_now() - parse_utc_datetime(reference)).days), 0)
    except (ValueError, TypeError):
        return None


def lean_record(record: dict) -> dict:
    """Projection used for search results. See MEMORY_RESULT_FIELDS.

    Adds the one derived field worth its bytes: how long since anyone confirmed
    this. A reader can act on "checked 140 days ago" — it cannot act on a
    self-assigned confidence that every record shares.
    """
    projected = {field: record.get(field) for field in MEMORY_RESULT_FIELDS}
    projected["days_since_confirmed"] = days_since_confirmed(record)
    return projected


# A subject anchor names the thing a later session would go and look at to check
# the record. Typed rather than free text because the whole point is that a
# verifier can dispatch on it — `domain` was free text and decayed into 100
# values, half of them used once.
#
# The vocabulary is deliberately open. `repo`, `host` and `service` are what this
# system can check by itself, but what settles a record depends entirely on what
# the record is about: a URL, a document, a contact, a ticket. Installations add
# verifiers for their own types through connector bindings, so the type list is
# configuration rather than a fixed set of domains someone thought of first.
BUILTIN_ANCHOR_TYPES = ("repo", "host", "service")
ANCHOR_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,23}$")
SUBJECT_ANCHOR_MAX_VALUE = 200


def normalize_subject_anchor(value: Optional[str]) -> Optional[str]:
    """Validate a 'type:value' anchor, or None. Raises ValueError on a bad one.

    Rejecting rather than silently dropping a malformed anchor: a verifier that
    dispatches on the type cannot do anything sensible with 'the memory service'
    or 'repo:', and a quietly ignored anchor looks identical to one that was
    never set.
    """
    if value is None:
        return None
    text = value.strip()
    if not text or text.lower() == "none":
        return None
    anchor_type, _, anchor_value = text.partition(":")
    anchor_type = anchor_type.strip().lower()
    anchor_value = anchor_value.strip()
    if not ANCHOR_TYPE_PATTERN.match(anchor_type):
        raise ValueError(
            "subject_anchor must be 'type:value', where type is a short lowercase "
            f"word — built-in types are {', '.join(BUILTIN_ANCHOR_TYPES)}, and an "
            f"installation can add its own. Got '{text[:40]}'"
        )
    if not anchor_value:
        raise ValueError(f"subject_anchor '{anchor_type}:' is missing a value")
    if anchor_type == "repo" and anchor_value.startswith("/"):
        # An absolute path is a fact about one container's mount table, not
        # about the repository. The same checkout sits at a different absolute
        # path on the host than inside an agent's container, so an
        # absolute anchor is unresolvable by anyone except whoever wrote it —
        # which defeats the point of memory that outlives the agent.
        raise ValueError(
            "subject_anchor repo: paths must be relative to the workspace root, "
            f"not absolute ('{anchor_value[:48]}'). The same directory has a "
            "different absolute path in every agent's container; the server "
            "resolves the relative path against this installation's root."
        )
    if len(anchor_value) > SUBJECT_ANCHOR_MAX_VALUE:
        raise ValueError(
            f"subject_anchor value must be {SUBJECT_ANCHOR_MAX_VALUE} characters or fewer"
        )
    return f"{anchor_type}:{anchor_value}"


# Paths that exist at runtime rather than in a repository: databases, logs,
# caches, backups. An anchor pointing at one cannot be checked out, so it will
# read as "missing" on any machine that has not happened to create the file —
# which is how a bad anchor turns into false evidence that a true record is stale.
RUNTIME_PATH_HINTS = re.compile(
    r"(\.db(\.bak)?$|\.sqlite3?$|\.log$|\.bak$|\.pid$|\.lock$|^/?(data|tmp|var|logs|cache)/)",
    re.I,
)


def anchor_looks_like_runtime_state(anchor: Optional[str]) -> bool:
    if not anchor or not anchor.startswith("repo:"):
        return False
    return bool(RUNTIME_PATH_HINTS.search(anchor.partition(":")[2]))


def set_subject_anchor(
    record_id: str, anchor: Optional[str], changed_by: str = "unknown"
) -> Optional[dict]:
    """Repoint a record at what actually describes it.

    A wrong anchor is worse than none: it produces confident negative evidence
    on every verification pass. Correcting one has to be as easy as retracting
    the record, or the queue teaches people to delete good memories.
    """
    normalized = normalize_subject_anchor(anchor)
    record = get_memory_record(record_id)
    if not record or record["record_status"] != "active":
        return None

    provenance = _annotate_provenance(
        record.get("provenance_json") or json.dumps({}, separators=(",", ":")),
        anchor_corrected={
            "at": utc_now_iso(),
            "by": changed_by,
            "from": record.get("subject_anchor"),
            "to": normalized,
        },
    )
    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET subject_anchor = ?, provenance_json = ? "
            "WHERE id = ? AND record_status = 'active'",
            (normalized, provenance, record_id),
        )
        conn.commit()
    return get_memory_record(record_id)


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


def provenance_for_write(
    *,
    actor_type: str,
    actor_id: str,
    channel: str,
    route: str,
    source_kind: str,
    scope: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    extras: Optional[dict] = None,
) -> str:
    """Provenance for a memory write, including the work that produced it.

    Shared by every write path. It used to be duplicated per transport, and the
    copies drifted: the activity citation was added to the MCP one and silently
    never reached REST writes, so the same write produced different provenance
    depending on which door it came through.

    The activity is resolved server-side rather than taken as a parameter, for
    the same reason the rest of provenance is: a client-supplied citation is not
    evidence.
    """
    payload = dict(extras or {})
    payload["route"] = route
    if agent_id:
        try:
            from app.services import activity_service

            active = activity_service.get_active_activity_for_agent(agent_id, user_id)
            if active:
                payload["activity_id"] = active["id"]
        except Exception:
            logger.debug("Could not resolve active activity for provenance", exc_info=True)
    return build_provenance(
        actor_type=actor_type,
        actor_id=actor_id,
        channel=channel,
        source_kind=source_kind,
        scope=scope,
        user_id=user_id,
        agent_id=agent_id,
        extras=payload,
    )


def _annotate_provenance(provenance_json: Optional[str], **extras) -> Optional[str]:
    """Merge server-derived facts into an existing provenance blob.

    Used to record why a record was given an expiry it did not ask for, so the
    decision is auditable later instead of looking like the writer's own choice.
    Leaves malformed or absent provenance alone rather than inventing one.
    """
    if not provenance_json:
        return provenance_json
    try:
        payload = json.loads(provenance_json)
        if not isinstance(payload, dict):
            return provenance_json
    except (TypeError, ValueError):
        return provenance_json
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


# Shapes that describe one occurrence of recurring work rather than something
# durable: monitor fires, heartbeat ticks, per-run reviews, ticket closeouts.
# Derived from an audit of the live corpus, where records matching these made up
# the overwhelming majority of everything an operator later had to retract by
# hand. They are not wrong to record — they are just episodic, and the activity
# trail is where episodic belongs.
#
# Two tiers, because the two consequences are not equally reversible. Advising
# wrongly costs a line of noise in a response; expiring wrongly deletes real
# knowledge months later, silently, when nobody is looking. So the advisory set
# is generous and the expiring set is strict — measured against the live corpus,
# where the strict set covers 79% of records an operator had already retracted
# by hand while flagging none of the durable ones.
EPISODIC_ADVISORY_PATTERNS = (
    (re.compile(r"\bheartbeat\b", re.I), "reads as a heartbeat tick"),
    (re.compile(r"\bsilent[- ]run\b", re.I), "reads as a per-run review"),
    (re.compile(r"\bcontinuation tick\b", re.I), "reads as a continuation tick"),
    (re.compile(r"\bfire #\s*\d+", re.I), "reads as a numbered routine fire"),
    (re.compile(r"\bper-fire\b", re.I), "reads as a per-fire log"),
    (re.compile(r"\bidle closeout\b", re.I), "reads as an idle closeout"),
    (
        re.compile(r"\b[A-Z]{2,6}-\d+\b.{0,80}\bclosed\s+(done|in_review)\b", re.I | re.S),
        "reads as a ticket closeout",
    ),
)

# Deliberately absent here: a bare ticket closeout ("STA-47 closed done ...").
# In the live corpus those routinely carry a durable payload in the same record
# ("...Built reusable Python client package at ..."), so they are worth a
# warning but must not silently expire.
EPISODIC_EXPIRY_PATTERNS = (
    (re.compile(r"\bsweep heartbeat\b", re.I), "reads as a routine sweep heartbeat"),
    (
        re.compile(r"\b(idle|closeout|continuation)\s+(heartbeat|tick)\b", re.I),
        "reads as a continuation tick",
    ),
    (re.compile(r"\bcontinuation tick\b", re.I), "reads as a continuation tick"),
    (re.compile(r"\bfire #\s*\d+", re.I), "reads as a numbered routine fire"),
    (re.compile(r"\bsilent[- ]run review for\b", re.I), "reads as a per-run review"),
    (
        re.compile(r"\b[A-Z]{2,6}-\d+\s+silent[- ]run\b", re.I),
        "reads as a per-run review",
    ),
    (re.compile(r"\bidle closeout\b", re.I), "reads as an idle closeout"),
    (
        re.compile(r"\b[A-Z]{2,6}-\d+\s+(wake[- ]recovery|routine reuse)\b", re.I),
        "reads as a per-fire monitor log",
    ),
)

# A per-occurrence log states when it happened, right at the top. Requiring both
# a timestamp and a lead-position match is what separates "SAG-638 sweep
# heartbeat (2026-06-07 15:16 UTC)" from a durable fact that merely discusses
# heartbeats, e.g. "the scheduler config is read once at startup ... heartbeat
# recovery runs scanSilentActiveRuns()".
EPISODIC_LEAD_CHARS = 200
EPISODIC_TIMESTAMP = re.compile(r"\b(20\d\d-\d\d-\d\d|\d\d:\d\d\s*(UTC|Z)|fire #\s*\d+)")
# Records *about* a cleanup quote the very phrases they are describing.
EPISODIC_META = re.compile(r"\bconsolidation note\b|\bretracted \d+\b", re.I)

EPISODIC_TTL_DAYS_DEFAULT = 30
DEDUPE_SIMILARITY_DEFAULT = 0.92


def detect_episodic_shape(content: str, topic: Optional[str] = None) -> Optional[str]:
    """Return why this write looks episodic, or None if it looks durable.

    Advisory only — nothing here rejects a write. An agent mid-task should not
    have its work refused because the phrasing tripped a regex; it gets told
    where the content belongs.
    """
    haystack = f"{content or ''}\n{topic or ''}"
    for pattern, reason in EPISODIC_ADVISORY_PATTERNS:
        if pattern.search(haystack):
            return reason
    return None


def detect_expiring_episodic_shape(
    content: str, topic: Optional[str] = None
) -> Optional[str]:
    """Return why this write should get an automatic expiry, or None.

    Strictly narrower than detect_episodic_shape: this one deletes things later,
    so it only fires on the unambiguous per-occurrence log shapes, and only when
    the marker appears up front alongside a timestamp.
    """
    head = f"{topic or ''}\n{(content or '')[:EPISODIC_LEAD_CHARS]}"
    if EPISODIC_META.search(head) or not EPISODIC_TIMESTAMP.search(head):
        return None
    for pattern, reason in EPISODIC_EXPIRY_PATTERNS:
        if pattern.search(head):
            return reason
    return None


def _system_setting_int(key: str, default: int) -> int:
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM system_settings WHERE key = ?", (key,)
            ).fetchone()
        return int(row["value"]) if row else default
    except (ValueError, TypeError, sqlite3.Error):
        return default


def _system_setting_float(key: str, default: float) -> float:
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM system_settings WHERE key = ?", (key,)
            ).fetchone()
        return float(row["value"]) if row else default
    except (ValueError, TypeError, sqlite3.Error):
        return default


def episodic_ttl_days() -> int:
    return _system_setting_int("episodic_memory_ttl_days", EPISODIC_TTL_DAYS_DEFAULT)


def find_near_duplicates(
    content: str,
    scope: str,
    memory_class: Optional[str] = None,
    threshold: Optional[float] = None,
    limit: int = 3,
    exclude_id: Optional[str] = None,
) -> list[dict]:
    """Find active records in the same scope that already say roughly this.

    Semantic-only: without embeddings there is no reliable way to tell a
    near-duplicate from a record that merely shares vocabulary, and a false
    "you already wrote this" is worse than no warning at all. Returns [] when
    vector search is unavailable rather than guessing from keyword overlap.
    """
    if not content.strip():
        return []
    if not (_EMBEDDING_AVAILABLE and vector_settings_service.is_vector_search_enabled()):
        return []

    cutoff = (
        threshold
        if threshold is not None
        else _system_setting_float("memory_dedupe_similarity", DEDUPE_SIMILARITY_DEFAULT)
    )

    try:
        vector_bytes, _ = embedding_service.generate_embedding(content)
    except Exception:
        logger.debug("Embedding failed during duplicate check", exc_info=True)
        return []
    if vector_bytes is None:
        return []

    conditions = ["scope = ?", "record_status = 'active'"]
    params: list = [_normalize_scope(scope)]
    if memory_class:
        conditions.append("memory_class = ?")
        params.append(memory_class)
    # The check runs after the write, so without this the record just written is
    # in the corpus and matches itself at similarity 1.0 — every write with
    # vector search enabled would report itself as a duplicate of itself.
    if exclude_id:
        conditions.append("id != ?")
        params.append(exclude_id)

    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {MEMORY_RECORD_COLUMNS} FROM memory_records "
            f"WHERE {' AND '.join(conditions)}",
            params,
        ).fetchall()

    candidate_ids = [row["id"] for row in rows]
    if not candidate_ids:
        return []

    try:
        scored = vector_service.cosine_search_top_k(
            vector_bytes, max(limit * 4, 20), candidate_ids
        )
    except Exception:
        logger.debug("Vector duplicate search failed", exc_info=True)
        return []

    by_id = {row["id"]: dict(row) for row in rows}
    duplicates = []
    for record_id, score in scored:
        if score < cutoff:
            continue
        record = by_id.get(record_id)
        if not record:
            continue
        duplicates.append(
            {
                "id": record["id"],
                "similarity": round(float(score), 4),
                "topic": record.get("topic"),
                "created_at": record.get("created_at"),
                "content_preview": (record.get("content") or "")[:200],
            }
        )
        if len(duplicates) >= limit:
            break
    return duplicates


# The two directions need different tests, because they ask different questions.
#
# For a fact, the question is whether the record's MAIN claim is a choice, so the
# match has to land in the first sentence — plenty of good facts mention a rule
# further down ("connector interop gotcha ... so callers should send an array")
# without being one.
#
# For a decision, the question is whether the record contains a choice ANYWHERE.
# A real decision usually states its rule somewhere, often mid-sentence ("the
# agent scope is intentionally non-shared and should not be used for handoff"),
# and a lead-anchored test flagged records like that as misfiled when they were
# filed exactly right.
CHOICE_IN_LEAD = re.compile(
    r"\b(should|must|shall|do not|don't|never |always |prefer )", re.I
)
CHOICE_ANYWHERE = re.compile(
    r"\b(should|must|shall|do not|don't|never|always|prefer|intentionally|policy|"
    r"rule|going forward)\b",
    re.I,
)
# Reporting on the world — including reporting a change that was made, which is
# the shape of a changelog entry rather than of a choice.
OBSERVED_STATE = re.compile(
    r"\b(is|are|was|were|runs?|listens?|lives?|resolves?|returns?|defaults? to|"
    r"located at|added|removed|fixed|implemented|now )\b",
    re.I,
)
CLASS_LEAD_CHARS = 200


def _first_sentence(text: str) -> str:
    """The record's main claim, for deciding what kind of statement it is.

    A character window cuts mid-thought and drags in whatever follows, which
    made a fact whose second sentence happened to say "callers should ..." look
    like a rule. The first sentence is what the record is actually asserting.
    """
    head = (text or "")[:CLASS_LEAD_CHARS]
    match = re.search(r"[.!?](?:\s|$)", head)
    return head[: match.start() + 1] if match else head


def detect_class_mismatch(content: str, memory_class: str) -> Optional[str]:
    """Flag a record whose wording does not match the class it was filed under.

    Only the two unambiguous directions are reported, because the classes are
    meant to be a routing decision an automated pass can rely on: a fact can be
    re-checked against the world, a decision can only be revised by a person. A
    corpus that mixes them makes that routing worthless — which is what the
    corpus looked like before the classes were documented.

    Advisory, and quiet in the ambiguous middle: plenty of good records state a
    choice and the observation behind it in the same breath.
    """
    text = content or ""
    claim = _first_sentence(text)
    if memory_class == "fact":
        if CHOICE_IN_LEAD.search(claim) and not OBSERVED_STATE.search(claim):
            return (
                "This states a choice ('should', 'must', 'do not') but is filed as a "
                "fact. A fact is something a later session could re-check against the "
                "code or a host; a choice that only a person can revise is a decision."
            )
    if memory_class == "decision":
        if not CHOICE_ANYWHERE.search(text) and OBSERVED_STATE.search(
            text[:CLASS_LEAD_CHARS]
        ):
            return (
                "This reads as an observation about how things are, but is filed as a "
                "decision. If a later session could confirm it by looking at the code "
                "or a host, it is a fact; decisions are choices nothing can verify."
            )
    return None


def assess_memory_write(
    content: str,
    scope: str,
    memory_class: str,
    topic: Optional[str] = None,
    check_duplicates: bool = True,
    exclude_id: Optional[str] = None,
    subject_anchor: Optional[str] = None,
) -> list[dict]:
    """Advisory checks a route can surface on the write response.

    Kept out of write_memory so its signature stays stable for its 28 callers,
    and so the advice is presentational: the write always succeeds, the caller
    is told what looked off.
    """
    warnings: list[dict] = []

    episodic_reason = detect_episodic_shape(content, topic)
    if episodic_reason and memory_class in ("fact", "decision"):
        warnings.append(
            {
                "code": "EPISODIC_CONTENT",
                "message": (
                    f"This {episodic_reason} — per-occurrence work belongs in the activity "
                    "trail, not durable memory. Record it with activity_update "
                    "(task_note/task_result) and reserve memory_write for what stays true "
                    "across sessions. Write one record per insight, superseding it when "
                    "the situation changes, rather than one per occurrence."
                ),
            }
        )

    if anchor_looks_like_runtime_state(subject_anchor):
        warnings.append(
            {
                "code": "ANCHOR_LOOKS_RUNTIME",
                "message": (
                    f"'{subject_anchor}' looks like a runtime file (a database, log or "
                    "backup) rather than something in the repository. A later check will "
                    "not find it and will report this record as stale. Anchor it to the "
                    "code that owns the behaviour, or leave the anchor off."
                ),
            }
        )

    if subject_anchor:
        anchor_type = normalize_subject_anchor(subject_anchor).partition(":")[0]
        from app.services import verification_service

        if anchor_type not in verification_service.available_anchor_types():
            warnings.append(
                {
                    "code": "ANCHOR_TYPE_UNVERIFIABLE",
                    "message": (
                        f"Nothing here can currently check a '{anchor_type}:' anchor, so this "
                        "record will report as unverifiable. That is fine — the anchor still "
                        "says what would settle it — but a connector binding can be "
                        "registered to verify this type if you want it checked automatically."
                    ),
                }
            )

    class_mismatch = detect_class_mismatch(content, memory_class)
    if class_mismatch:
        warnings.append({"code": "CLASS_MISMATCH", "message": class_mismatch})

    if check_duplicates and memory_class != "scratchpad":
        duplicates = find_near_duplicates(
            content, scope, memory_class, exclude_id=exclude_id
        )
        if duplicates:
            warnings.append(
                {
                    "code": "POSSIBLE_DUPLICATE",
                    "message": (
                        "Records in this scope already say something very similar. "
                        "Prefer superseding one of them (supersedes_id) over adding a "
                        "near-duplicate."
                    ),
                    "candidates": duplicates,
                }
            )

    return warnings


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


def _evidence_bonus(record: dict) -> float:
    """Everything the system observed about a record, as one ranking adjustment.

    Replaces self-assessment with evidence: whether the record is inside its
    validity window, how recently anyone confirmed it, how often it has actually
    been recalled, and whether a caller said it helped.
    """
    return (
        _freshness_bonus(record)
        + _usefulness_bonus(record)
        + _staleness_penalty(record)
    )


def _usefulness_bonus(record: dict) -> float:
    """Observed usefulness: how often a record is recalled, and whether it helped.

    Deliberately small and saturating. This is a weak signal early on — a corpus
    with no usage history must not rank randomly — so it nudges rather than
    dominates, and a record cannot climb indefinitely by being returned a lot.
    Explicit feedback counts for more than a recall, because being returned is
    the retriever's opinion while feedback is the caller's.
    """
    recalls = record.get("recall_count") or 0
    helpful = record.get("helpful_count") or 0
    unhelpful = record.get("unhelpful_count") or 0
    bonus = min(recalls, 10) * 0.004          # up to +0.04
    bonus += min(helpful, 5) * 0.02           # up to +0.10
    bonus -= min(unhelpful, 5) * 0.04         # down to -0.20, so a marked-unhelpful
    return bonus                              # record falls below an unrated one


def _staleness_penalty(record: dict) -> float:
    """Push down records nobody has confirmed in a long time.

    Only for facts. A decision does not go stale because time passed — it stands
    until someone revises it — and penalising decisions for age would bury
    exactly the constraints that are most expensive to rediscover.
    """
    if record.get("memory_class") != "fact":
        return 0.0
    age_days = days_since_confirmed(record)
    if age_days is None or age_days <= CONFIRMATION_HALF_LIFE_DAYS:
        return 0.0
    over = age_days - CONFIRMATION_HALF_LIFE_DAYS
    return -min(0.12, 0.12 * (over / (CONFIRMATION_HALF_LIFE_DAYS * 2)))


def record_recall(record_ids: list[str]) -> None:
    """Note that these records were returned to a caller.

    Best-effort: a failure here must never turn a successful search into an
    error, and the count is a ranking nudge rather than an audit figure.
    """
    if not record_ids:
        return
    placeholders = ",".join("?" for _ in record_ids)
    try:
        with get_db() as conn:
            conn.execute(
                f"UPDATE memory_records SET recall_count = COALESCE(recall_count, 0) + 1, "
                f"last_recalled_at = ? WHERE id IN ({placeholders})",
                [utc_now_iso(), *record_ids],
            )
            conn.commit()
    except sqlite3.Error:
        logger.debug("Could not record recall counts", exc_info=True)


def record_feedback(record_id: str, helpful: bool) -> Optional[dict]:
    """Record a caller's verdict on whether a recalled record actually helped."""
    column = "helpful_count" if helpful else "unhelpful_count"
    with get_db() as conn:
        cursor = conn.execute(
            f"UPDATE memory_records SET {column} = COALESCE({column}, 0) + 1 WHERE id = ?",
            (record_id,),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
    return get_memory_record(record_id)


def confirm_memory(record_id: str, evidence: str, verified_by: str = "unknown") -> Optional[dict]:
    """Mark an active record as checked against the world, as of now.

    `evidence` is required and says what was actually looked at. Confirmation
    without evidence is just an opinion wearing a timestamp, and the ranking
    treats last_confirmed_at as a claim about reality — the one place where a
    guess does measurable damage. Recording the basis also makes a later
    reviewer able to judge the check itself, not merely that one happened.
    """
    detail = (evidence or "").strip()
    if not detail:
        raise ValueError(
            "evidence is required: say what you checked, e.g. "
            "'adapter.json reports version 1.0.1' or 'ssh router: /etc/version = v3.0.1'"
        )

    record = get_memory_record(record_id)
    if not record or record["record_status"] != "active":
        return None

    now = utc_now_iso()
    verification = {"at": now, "by": verified_by, "evidence": detail[:500]}
    # Records written before provenance was stamped have none at all; the
    # evidence is the point of this call, so create a blob rather than drop it.
    provenance = record.get("provenance_json") or json.dumps({}, separators=(",", ":"))
    provenance = _annotate_provenance(provenance, verified=verification)
    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET last_confirmed_at = ?, provenance_json = ? "
            "WHERE id = ? AND record_status = 'active'",
            (now, provenance, record_id),
        )
        conn.commit()
    return get_memory_record(record_id)


def _current_record_priority(record: dict) -> int:
    return 0 if record.get("record_status") == "active" else 1


def write_memory(
    content: str,
    memory_class: str,
    scope: str,
    topic: Optional[str] = None,
    confidence: float = 0.5,
    importance: float = 0.5,
    source_kind: str = "agent_inference",
    supersedes_id: Optional[str] = None,
    provenance_json: Optional[str] = None,
    slot_key: Optional[str] = None,
    valid_from: Optional[str] = None,
    valid_to: Optional[str] = None,
    last_confirmed_at: Optional[str] = None,
    expires_at: Optional[str] = None,
    subject_anchor: Optional[str] = None,
    allow_pii_shared: bool = False,
) -> tuple[dict, str | None]:
    if memory_class not in MEMORY_CLASSES:
        raise ValueError(f"Invalid memory_class: {memory_class}")
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"Invalid source_kind: {source_kind}")
    if not content.strip():
        raise ValueError("Content cannot be empty")

    normalized_scope = _normalize_scope(scope)
    normalized_subject_anchor = normalize_subject_anchor(subject_anchor)
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

    # Deliberately NOT baselined to now. last_confirmed_at means "checked
    # against the world", and writing a record is asserting it, not checking it.
    # Nothing is lost by leaving it null: days_since_confirmed falls back to
    # created_at, so a fresh record still reads as 0 days old, while null stays
    # available to mean the honest thing — nobody has ever verified this.

    # Episodic content gets an expiry it did not ask for, so per-occurrence
    # records age out instead of accumulating until someone retracts them by
    # hand. An explicit expires_at from the caller always wins, and durable
    # classes are untouched: scratchpad already has its own retention sweep,
    # and preferences are never treated as episodic.
    auto_expiry_reason = None
    if normalized_expires_at is None and memory_class in ("fact", "decision"):
        auto_expiry_reason = detect_expiring_episodic_shape(content, topic)
        if auto_expiry_reason:
            ttl_days = episodic_ttl_days()
            if ttl_days > 0:
                normalized_expires_at = (
                    parse_utc_datetime(now) + timedelta(days=ttl_days)
                ).isoformat()
                provenance_json = _annotate_provenance(
                    provenance_json,
                    auto_expiry_reason=auto_expiry_reason,
                    auto_expiry_days=ttl_days,
                )

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
                # Close the old record's validity window as well as marking it
                # superseded. These are two different clocks: status_changed_at
                # says when the system learned, valid_to says when the fact
                # stopped being true. Without the second one the corpus cannot
                # answer "what was true in March" — both the old and the new
                # record look equally current to anything reading history.
                #
                # An explicit valid_to from the writer is never overwritten: they
                # know something the supersession does not.
                "UPDATE memory_records SET record_status = 'superseded', "
                "superseded_by_id = ?, status_changed_at = ?, "
                "valid_to = COALESCE(valid_to, ?) "
                "WHERE id = ? AND record_status = 'active'",
                (record_id, now, normalized_valid_from or now, effective_supersedes_id),
            )

        conn.execute(
            """
            INSERT INTO memory_records
            (id, content, memory_class, scope, topic, confidence, importance,
             source_kind, created_at, record_status, supersedes_id,
             provenance_json, slot_key, valid_from, valid_to, last_confirmed_at, expires_at,
             subject_anchor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                content,
                memory_class,
                normalized_scope,
                topic,
                confidence,
                importance,
                source_kind,
                now,
                effective_supersedes_id,
                provenance_json,
                normalized_slot_key,
                normalized_valid_from,
                normalized_valid_to,
                normalized_last_confirmed_at,
                normalized_expires_at,
                normalized_subject_anchor,
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
            "topic": topic,
            "confidence": confidence,
            "importance": importance,
            "source_kind": source_kind,
            "created_at": now,
            "record_status": "active",
            "supersedes_id": effective_supersedes_id,
            "provenance_json": provenance_json,
            "slot_key": normalized_slot_key,
            "valid_from": normalized_valid_from,
            "valid_to": normalized_valid_to,
            "last_confirmed_at": normalized_last_confirmed_at,
            "expires_at": normalized_expires_at,
            "subject_anchor": normalized_subject_anchor,
        }, None


# A pinned set is only useful while it stays small enough to read in full. The
# cap is what keeps it a short list of standing rules rather than a second,
# unranked copy of the corpus.
MAX_PINNED_PER_SCOPE = 10
PINNABLE_CLASSES = ("fact", "decision", "preference")


def pinned_limit() -> int:
    return _system_setting_int("max_pinned_per_scope", MAX_PINNED_PER_SCOPE)


def set_pinned(record_id: str, pinned: bool) -> Optional[dict]:
    """Mark a record as always-shown, or clear it.

    Pinning is the answer to a specific failure: a standing constraint has to
    win a search to be seen, and if it loses it may as well not exist. A pinned
    record is loaded rather than retrieved.

    Restricted to the durable classes and capped per scope. A scratchpad is
    temporary by definition, and an uncapped pin list stops being readable,
    which is the whole point of it.
    """
    record = get_memory_record(record_id)
    if not record or record["record_status"] != "active":
        return None
    if pinned and record["memory_class"] not in PINNABLE_CLASSES:
        raise ValueError(
            f"only {', '.join(PINNABLE_CLASSES)} records can be pinned, "
            f"not {record['memory_class']}"
        )

    if pinned and not record.get("pinned"):
        limit = pinned_limit()
        with get_db() as conn:
            current = conn.execute(
                "SELECT COUNT(*) FROM memory_records "
                "WHERE scope = ? AND pinned = 1 AND record_status = 'active'",
                (record["scope"],),
            ).fetchone()[0]
        if current >= limit:
            raise ValueError(
                f"{record['scope']} already has {current} pinned records (limit {limit}). "
                "Unpin something first — a pinned list only works while it is short "
                "enough to read in full."
            )

    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET pinned = ? WHERE id = ? AND record_status = 'active'",
            (1 if pinned else 0, record_id),
        )
        conn.commit()
    return get_memory_record(record_id)


def pinned_records(authorized_scopes: list[str], limit: int = 50) -> list[dict]:
    """The standing context for these scopes, newest last so it reads in order."""
    if not authorized_scopes:
        return []
    placeholders = ",".join("?" for _ in authorized_scopes)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {MEMORY_RECORD_COLUMNS} FROM memory_records "
            f"WHERE pinned = 1 AND record_status = 'active' "
            f"AND scope IN ({placeholders}) ORDER BY scope, created_at LIMIT ?",
            [*authorized_scopes, max(limit, 0)],
        ).fetchall()
    return [dict(row) for row in rows]


def records_for_activity(
    activity_id: str,
    authorized_scopes: Optional[list[str]] = None,
    include_inactive: bool = False,
    limit: int = 50,
) -> list[dict]:
    """Records written while this activity was the caller's open work.

    The forward link — a record citing the activity that produced it — has been
    stamped server-side since provenance was introduced. This is the traversal
    the other way, which is the one a handoff needs: not "what was that session
    doing" but "what did it conclude".

    Scope-filtered like any other read: producing a record does not entitle a
    later reader to see it.
    """
    conditions = ["json_extract(provenance_json, '$.activity_id') = ?"]
    params: list = [activity_id]
    if not include_inactive:
        conditions.append("record_status = 'active'")
    if authorized_scopes is not None:
        if not authorized_scopes:
            return []
        conditions.append(f"scope IN ({','.join('?' for _ in authorized_scopes)})")
        params.extend(authorized_scopes)
    params.append(max(limit, 0))

    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {MEMORY_RECORD_COLUMNS} FROM memory_records "
            f"WHERE {' AND '.join(conditions)} ORDER BY created_at LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


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
            "UPDATE memory_records SET record_status = 'retracted', status_changed_at = ? "
            "WHERE id = ? AND record_status = 'active'",
            (utc_now_iso(), record_id),
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
    (preserving class/topic/slot_key/confidence/importance, stamping
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
            (id, content, memory_class, scope, topic, confidence, importance,
             source_kind, created_at, record_status, supersedes_id,
             provenance_json, slot_key, valid_from, valid_to, last_confirmed_at, expires_at,
             subject_anchor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id,
                old["content"],
                old["memory_class"],
                normalized_new_scope,
                old.get("topic"),
                old.get("confidence"),
                old.get("importance"),
                old.get("source_kind"),
                now,
                old["id"],
                new_provenance_json,
                old.get("slot_key"),
                old.get("valid_from"),
                old.get("valid_to"),
                old.get("last_confirmed_at"),
                old.get("expires_at"),
                # A record that moves scope still describes the same subject, so
                # the anchor travels with it.
                old.get("subject_anchor"),
            ),
        )
        # Deliberately leaves valid_to alone: moving a record to another scope
        # changes where it lives, not whether what it says is true.
        conn.execute(
            "UPDATE memory_records SET record_status = 'retracted', "
            "superseded_by_id = ?, provenance_json = ?, status_changed_at = ? "
            "WHERE id = ? AND record_status = 'active'",
            (new_id, old_provenance_json, now, old["id"]),
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
            "UPDATE memory_records SET record_status = 'active', status_changed_at = NULL "
            "WHERE id = ? AND record_status = 'retracted'",
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
    topic: Optional[str] = None,
    memory_class: Optional[str] = None,
    min_confidence: float = 0.0,
    limit: int = 20,
    offset: int = 0,
    include_retracted: bool = False,
    include_superseded: bool = False,
    subject_anchor: Optional[str] = None,
    activity_id: Optional[str] = None,
    as_of: Optional[str] = None,
) -> tuple[list[dict], str]:
    sanitized = _sanitize_fts_query(query)

    status_filter = " AND (mr.expires_at IS NULL OR datetime(mr.expires_at) > datetime('now'))"
    if not include_retracted:
        status_filter += " AND mr.record_status != 'retracted'"

    as_of_instant = _normalize_optional_timestamp(as_of, "as_of") if as_of else None
    if as_of_instant:
        # Point-in-time: what did we hold to be true then, not what is current.
        # Superseded records are exactly what this question is about, so they
        # come back in — but only while their validity window covers the
        # instant. Retracted records stay out either way: retraction means the
        # record was never worth standing behind, which is a different statement
        # from "it stopped being true".
        status_filter += (
            " AND datetime(COALESCE(mr.valid_from, mr.created_at)) <= datetime(?)"
            " AND (mr.valid_to IS NULL OR datetime(mr.valid_to) > datetime(?))"
        )
    elif not include_superseded:
        status_filter += " AND mr.record_status != 'superseded'"

    scope_placeholders = ",".join(["?" for _ in authorized_scopes])
    status_params = [as_of_instant, as_of_instant] if as_of_instant else []
    params = [sanitized] + authorized_scopes + status_params
    extra = []
    extra_vals = []
    if topic:
        extra.append("mr.topic = ?")
        extra_vals.append(topic)
    if memory_class:
        extra.append("mr.memory_class = ?")
        extra_vals.append(memory_class)
    if activity_id:
        extra.append("json_extract(mr.provenance_json, '$.activity_id') = ?")
        extra_vals.append(activity_id)
    if subject_anchor:
        # Prefix match so "repo:app/services" finds everything anchored under it,
        # which is the question a verifier actually asks ("what did we record
        # about this area of the code?").
        extra.append("(mr.subject_anchor = ? OR mr.subject_anchor LIKE ?)")
        extra_vals.extend([subject_anchor, f"{subject_anchor}%"])
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
            if vector_bytes is not None:
                with get_db() as conn:
                    candidate_rows = conn.execute(
                        f"""
                        SELECT mr.{MEMORY_RECORD_COLUMNS.replace(', ', ', mr.')}
                        FROM memory_records mr
                        WHERE mr.scope IN ({scope_placeholders}){status_filter}{extra_sql}
                        """,
                        authorized_scopes + status_params + extra_vals,
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
                        r["_freshness_score"] = _evidence_bonus(r)
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
        # Whole-table MATCH (not fts.content MATCH) so a query token can hit the
        # content OR the topic — both are indexed and trigger-maintained for
        # exactly this purpose. Column-restricting to content made records
        # unfindable by their own topic (e.g. topic="database", query "database"
        # → zero results), breaking the documented "retry with exact topic
        # values" recall workflow.
        sql = f"""
            SELECT mr.{MEMORY_RECORD_COLUMNS.replace(', ', ', mr.')}
            FROM memory_records mr
            JOIN memory_records_fts fts ON fts.rowid = mr.rowid
            WHERE memory_records_fts MATCH ? AND mr.scope IN ({scope_placeholders}){status_filter}{extra_sql}
            ORDER BY mr.importance DESC, mr.created_at DESC
            LIMIT ? OFFSET ?
        """
        # Fetch limit+offset rows with no SQL offset so the final merged[start:end]
        # slice can apply pagination correctly for both fts_only and hybrid paths.
        fts_sql_limit = limit + max(offset, 0) if limit >= 0 else limit
        fts_params = params + extra_vals + [fts_sql_limit, 0]
        with get_db() as conn:
            fts_rows = conn.execute(sql, fts_params).fetchall()
            fts_results = [dict(row) for row in fts_rows]
            for r in fts_results:
                r["_freshness_score"] = _evidence_bonus(r)
    elif not semantic_candidates:
        return [], "fts_only"

    merged: list[dict] = []
    seen_ids = set()
    if retrieval_mode == "hybrid" and semantic_candidates:
        # An FTS hit matched EVERY sanitized query token exactly (AND semantics)
        # — a high-precision signal the ranking must respect. Without this floor,
        # an FTS-only hit fell back to importance*0.5 (e.g. 0.15) and got buried
        # below dozens of loosely-related semantic candidates, so exact
        # topic/keyword lookups ("retry with exact topic values") missed records
        # that matched the query perfectly. The floor slots exact matches above
        # weak semantic hits while letting strong semantic scores still win.
        fts_ids = {r["id"] for r in fts_results}
        _EXACT_MATCH_FLOOR = 0.6
        for r in semantic_candidates:
            if r["id"] not in seen_ids:
                merged.append(r)
                seen_ids.add(r["id"])
        for r in fts_results:
            if r["id"] not in seen_ids:
                merged.append(r)
                seen_ids.add(r["id"])

        def _hybrid_score(x: dict) -> float:
            score = (
                x.get("_semantic_score", 0.0)
                if "_semantic_score" in x
                else (x.get("importance", 0.5) * 0.5)
            )

            if x["id"] in fts_ids:
                score = max(score, _EXACT_MATCH_FLOOR)
            return score + x.get("_freshness_score", 0.0)

        merged.sort(
            key=lambda x: (
                _current_record_priority(x),
                -_hybrid_score(x),
                x.get("created_at", ""),
            )
        )
    else:
        merged = fts_results
        merged.sort(
            key=lambda x: (
                _current_record_priority(x),
                # Self-assigned importance is kept as the weakest term rather
                # than removed outright: on a corpus with no usage history yet
                # it is the only ordering signal there is, and dropping it now
                # would make ranking worse before the evidence-based terms have
                # anything to say. It is scaled down so a single piece of real
                # feedback outweighs the gap between two self-assigned scores.
                -(x.get("importance", 0.5) * 0.25 + x.get("_freshness_score", 0.0)),
                x.get("created_at", ""),
            )
        )

    for r in merged:
        r.pop("_semantic_score", None)
        r.pop("_freshness_score", None)

    start = max(offset, 0)
    end = start + max(limit, 0) if limit >= 0 else None
    page = merged[start:end]
    # Only the page the caller actually sees counts as a recall — the candidate
    # set is an implementation detail and would inflate every record in scope.
    record_recall([r["id"] for r in page])
    return page, retrieval_mode


def get_memory_by_scope(
    scope: str,
    limit: int = 50,
    offset: int = 0,
    record_status: Optional[str] = None,
) -> list[dict]:
    """record_status=None (the default) means 'active' -- callers browsing a
    scope should see current truth by default, not retracted/superseded
    records mixed in silently. Pass record_status="all" to deliberately see
    every status, or a specific status ("retracted", "superseded") to inspect
    just that one."""
    normalized_scope = _normalize_scope(scope)
    effective_status = "active" if record_status is None else record_status
    status_sql = "" if effective_status == "all" else " AND record_status = ?"
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
                + ([effective_status] if status_sql else [])
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
    """See get_memory_by_scope: record_status=None defaults to 'active'; pass
    "all" to deliberately see every status."""
    if not scopes:
        return []
    placeholders = ",".join(["?" for _ in scopes])
    effective_status = "active" if record_status is None else record_status
    status_sql = "" if effective_status == "all" else " AND record_status = ?"
    expires_sql = " AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))"
    params: list = (
        list(scopes) + ([effective_status] if status_sql else []) + [limit, offset]
    )
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
