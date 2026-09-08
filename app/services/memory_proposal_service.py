"""Consolidation proposals for the memory corpus, and the verdicts on them.

Rules scan active memory and suggest what to retract or re-confirm. Nothing is
applied automatically: a proposal sits in a queue until an operator accepts or
rejects it, and that verdict is stored against the rule that produced it.

The verdict trail is the product here, not bookkeeping. It gives each rule a
measured precision from real decisions, which is the only honest basis for ever
letting one act unattended — and it stops a rejected suggestion from being
re-proposed on the next pass, so the queue converges instead of nagging.

The `unconfirmed_inference` rule plus the cap machinery in `generate_proposals`
gate queue growth so that turning consolidation on unattended does not flood the
queue. See plan.md Workstreams 2 and 5 for the design.
"""

import json
import logging
import re
import secrets
import sqlite3
from collections import defaultdict
from typing import Callable, Optional

from app.database import get_db
from app.services import memory_service
from app.time_utils import utc_now, utc_now_iso, parse_utc_datetime

logger = logging.getLogger(__name__)

PROPOSAL_COLUMNS = (
    "id, rule, action, scope, target_ids_json, rationale, evidence_json, status, "
    "created_at, decided_at, decided_by, applied_count"
)

# Assertions that are true when written and quietly rot: pinned versions, host
# addresses, published ports, "currently"-shaped claims about running state.
VOLATILE_MARKERS = re.compile(
    r"\b(\d+\.\d+\.\d+|\d{1,3}(?:\.\d{1,3}){3}|127\.0\.0\.1|localhost:\d+|:\d{4,5}\b|"
    r"currently|right now|as of \d{4}-\d\d-\d\d|pinned to|image `)",
    re.I,
)

STALE_VOLATILE_DAYS_DEFAULT = 45
UNCONFIRMED_INFERENCE_DAYS_DEFAULT = 90
# Below this importance, an unconfirmed fact just ages out of ranking quietly
# instead of asking the operator to fact-check it. Confirmation review time is
# scarce and should go to the records that would actually be missed, not every
# agent-authored assertion regardless of how minor. See plan.md Workstream 2
# follow-up: importance-gating the review queue.
UNCONFIRMED_INFERENCE_MIN_IMPORTANCE_DEFAULT = 0.7
# Reuse memory_service.DUPLICATE_PREFIX_CHARS; the two checks (one in the
# proposal rule, one as a non-embedding fallback in memory_service) must agree
# on what "near-exact prefix" means.

# Three caps, working together, so unattended consolidation does not flood the
# queue. See plan.md Workstream 5.
PROPOSAL_PENDING_CAP_PER_RULE_DEFAULT = 20  # per-(rule, scope) ceiling
PROPOSAL_PENDING_CAP_TOTAL_DEFAULT = 50  # installation-wide pending ceiling
PROPOSAL_GENERATION_BUDGET_PER_RUN_DEFAULT = 20  # max new rows per call

# A closeout announces itself in its opening words: "STA-594 closed done 2026-07-21".
# Anchored to the lead of the record for the same reason the expiry rule is —
# a record that merely mentions a closed ticket somewhere in its body is a
# record about work, not a closeout of it. Broad advisory matching is fine for
# a warning at write time but far too loose to propose retracting anything.
TICKET_CLOSEOUT = re.compile(
    r"^\s*(?:#+\s*)?[A-Z]{2,6}-\d+\b[^.\n]{0,60}\bclosed\s+(?:done|in_review)\b", re.I
)
CLOSEOUT_LEAD_CHARS = 200


def _rows_to_dicts(rows) -> list[dict]:
    return [dict(row) for row in rows]


def _active_records(scope: Optional[str] = None) -> list[dict]:
    # Pinned records are excluded from every rule. The operator has already
    # answered the question these rules ask, and a queue that keeps proposing
    # to remove standing context is a queue that trains people to stop reading
    # it.
    conditions = ["record_status = 'active'", "COALESCE(pinned, 0) = 0"]
    params: list = []
    if scope:
        conditions.append("scope = ?")
        params.append(scope)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {memory_service.MEMORY_RECORD_COLUMNS} FROM memory_records "
            f"WHERE {' AND '.join(conditions)} ORDER BY created_at DESC",
            params,
        ).fetchall()
    return _rows_to_dicts(rows)


def _preview(record: dict, limit: int = 180) -> dict:
    return {
        "id": record["id"],
        "scope": record.get("scope"),
        "topic": record.get("topic"),
        "memory_class": record.get("memory_class"),
        "created_at": record.get("created_at"),
        "content_preview": " ".join((record.get("content") or "").split())[:limit],
    }


# --- rules -----------------------------------------------------------------
#
# Each rule takes the active corpus and returns candidate proposals. A rule
# never touches the database; generate_proposals decides what actually gets
# queued, so a rule can be added or tuned without any write-path risk.


def _rule_episodic_log(records: list[dict]) -> list[dict]:
    """Per-occurrence logs that predate automatic expiry."""
    proposals = []
    for record in records:
        if record.get("expires_at"):
            continue
        reason = memory_service.detect_expiring_episodic_shape(
            record.get("content") or "", record.get("topic")
        )
        if not reason:
            continue
        proposals.append(
            {
                "rule": "episodic_log",
                "action": "retract",
                "scope": record["scope"],
                "target_ids": [record["id"]],
                "rationale": _rationale_episodic_log({}),
                "evidence": {"records": [_preview(record)]},
            }
        )
    return proposals


def _rule_ticket_closeout(records: list[dict]) -> list[dict]:
    """Ticket-closeout narration that the strict expiry rule deliberately skips.

    Split from episodic_log rather than merged into it because these often carry
    a durable payload in the same record, so they need a human read. Keeping it
    a separate rule means its precision is measured separately too.
    """
    proposals = []
    for record in records:
        if record.get("expires_at"):
            continue
        if memory_service.detect_expiring_episodic_shape(
            record.get("content") or "", record.get("topic")
        ):
            continue  # already covered by episodic_log
        head = " ".join((record.get("content") or "").split())[:CLOSEOUT_LEAD_CHARS]
        if not TICKET_CLOSEOUT.match(head):
            continue
        proposals.append(
            {
                "rule": "ticket_closeout",
                "action": "retract",
                "scope": record["scope"],
                "target_ids": [record["id"]],
                "rationale": _rationale_ticket_closeout({}),
                "evidence": {"records": [_preview(record)]},
            }
        )
    return proposals


def _rule_duplicate_cluster(records: list[dict]) -> list[dict]:
    """Records that open with the same text — near-certain restatements.

    Deliberately a prefix match rather than an embedding search: this rule
    proposes retraction, so it only fires where duplication is obvious on its
    face. Semantic near-duplicates are surfaced at write time instead, where
    the cost of being wrong is a warning rather than a deletion.
    """
    clusters: dict[tuple, list[dict]] = {}
    for record in records:
        normalized = " ".join((record.get("content") or "").split()).lower()
        if len(normalized) < memory_service.DUPLICATE_PREFIX_CHARS:
            continue
        key = (record["scope"], normalized[: memory_service.DUPLICATE_PREFIX_CHARS])
        clusters.setdefault(key, []).append(record)

    proposals = []
    for (scope, _prefix), group in clusters.items():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda r: r.get("created_at") or "", reverse=True)
        keep, drop = ordered[0], ordered[1:]
        proposals.append(
            {
                "rule": "duplicate_cluster",
                "action": "retract",
                "scope": scope,
                "target_ids": [r["id"] for r in drop],
                "rationale": _rationale_duplicate_cluster(
                    {"records": drop, "keep": keep}
                ),
                "evidence": {
                    "keep": _preview(keep),
                    "records": [_preview(r) for r in drop],
                },
            }
        )
    return proposals


def _rule_stale_volatile(records: list[dict]) -> list[dict]:
    """Old assertions about state that drifts, never re-confirmed since.

    Proposes confirmation, not retraction. A stale record is not a wrong one —
    it is one nobody has checked, and the fix is to look, not to delete.
    """
    cutoff_days = memory_service._system_setting_int(
        "stale_volatile_days", STALE_VOLATILE_DAYS_DEFAULT
    )
    now = utc_now()
    proposals = []
    for record in records:
        # Facts only. A fact asserts observed state, which perishes when the
        # world moves; a decision records what was chosen, which does not stop
        # being true because a version number changed — it gets superseded by a
        # later decision instead. On the live corpus every decision this rule
        # caught was a durable choice (a pinned image, a session protocol, a
        # collaboration model) that merely happened to quote a version.
        if record.get("memory_class") != "fact":
            continue
        content = record.get("content") or ""
        if not VOLATILE_MARKERS.search(content):
            continue
        reference = record.get("last_confirmed_at") or record.get("created_at")
        if not reference:
            continue
        try:
            age_days = (now - parse_utc_datetime(reference)).days
        except (ValueError, TypeError):
            continue
        if age_days < cutoff_days:
            continue
        proposals.append(
            {
                "rule": "stale_volatile",
                "action": "confirm",
                "scope": record["scope"],
                "target_ids": [record["id"]],
                "rationale": _rationale_stale_volatile({"age_days": age_days}),
                "evidence": {"records": [_preview(record)], "age_days": age_days},
            }
        )
    return proposals


def _rule_unconfirmed_inference(records: list[dict]) -> list[dict]:
    """Facts nobody has ever confirmed, beyond a short grace period.

    A non-human-authored, unconfirmed fact takes the ranking penalty in
    Workstream 2 (`_provenance_penalty`). This rule asks the operator to do
    something about it on a much shorter cycle than `stale_volatile` —
    `_provenance_penalty` is not staleness, the underlying problem is the same
    and the question to the reviewer is the same ("is this still current?"),
    but the rule keeps its own precision score so its quality is measured
    separately. See plan.md Workstream 2.
    """
    cutoff_days = _unconfirmed_inference_days_setting()
    min_importance = _unconfirmed_inference_min_importance_setting()
    now = utc_now()
    proposals = []
    for record in records:
        if record.get("memory_class") != "fact":
            continue
        # Provably human-authored tiers do not take the penalty and so are not
        # in this rule's candidate set; every other tier does, including
        # tool_output and external_import.
        if record.get("source_kind") in memory_service.HUMAN_PROVENANCE_KINDS:
            continue
        if record.get("last_confirmed_at"):
            continue
        # Below-threshold importance ages out of ranking quietly rather than
        # spending review attention on it. Missing importance reads as 0.0
        # (unset, not "average") so it does not slip through the default gate.
        importance = record.get("importance")
        if (importance if importance is not None else 0.0) < min_importance:
            continue
        created_at = record.get("created_at")
        if not created_at:
            continue
        try:
            age_days = (now - parse_utc_datetime(created_at)).days
        except (ValueError, TypeError):
            continue
        if age_days < cutoff_days:
            continue
        proposals.append(
            {
                "rule": "unconfirmed_inference",
                "action": "confirm",
                "scope": record["scope"],
                "target_ids": [record["id"]],
                "rationale": _rationale_unconfirmed_inference(
                    {"age_days": age_days, "source_kind": record.get("source_kind")}
                ),
                "evidence": {
                    "records": [_preview(record)],
                    "age_days": age_days,
                    "source_kind": record.get("source_kind"),
                },
            }
        )
    return proposals


# Per-record eligibility predicate shared by generate_proposals (for re-checking
# inside the write lock, see Workstream 5 step 4) and tests. The rule body
# itself is the single source of truth — this predicate just re-asks the same
# per-record question against the current row state, so a record that drifted
# out of eligibility between the read phase and the lock is caught here.
def _is_unconfirmed_inference_candidate(record: dict) -> bool:
    if record.get("memory_class") != "fact":
        return False
    if record.get("source_kind") in memory_service.HUMAN_PROVENANCE_KINDS:
        return False
    if record.get("last_confirmed_at"):
        return False
    importance = record.get("importance")
    min_importance = _unconfirmed_inference_min_importance_setting()
    if (importance if importance is not None else 0.0) < min_importance:
        return False
    return True


def _is_stale_volatile_candidate(record: dict) -> bool:
    if record.get("memory_class") != "fact":
        return False
    content = record.get("content") or ""
    if not VOLATILE_MARKERS.search(content):
        return False
    reference = record.get("last_confirmed_at") or record.get("created_at")
    if not reference:
        return False
    try:
        age_days = (utc_now() - parse_utc_datetime(reference)).days
    except (ValueError, TypeError):
        return False
    cutoff = memory_service._system_setting_int(
        "stale_volatile_days", STALE_VOLATILE_DAYS_DEFAULT
    )
    return age_days >= cutoff


# Maps a rule name to the per-record eligibility predicate used for insert-time
# revalidation. A rule absent from this map falls back to "active and unpinned"
# — the existing behavior — which is correct for the read-only shape rules
# (episodic_log, ticket_closeout, duplicate_cluster) whose only eligibility
# requirement is membership in the active corpus.
_RULE_ELIGIBILITY: dict[str, Callable[[dict], bool]] = {
    "unconfirmed_inference": _is_unconfirmed_inference_candidate,
    "stale_volatile": _is_stale_volatile_candidate,
}


RULES: dict[str, Callable[[list[dict]], list[dict]]] = {
    "episodic_log": _rule_episodic_log,
    "ticket_closeout": _rule_ticket_closeout,
    "duplicate_cluster": _rule_duplicate_cluster,
    "stale_volatile": _rule_stale_volatile,
    "unconfirmed_inference": _rule_unconfirmed_inference,
}

# Shown to whoever is reviewing the queue, so they say what the memory looks
# like in plain terms rather than naming the mechanism that found it.
RULE_DESCRIPTIONS = {
    "episodic_log": "Looks like a one-off status update from a scheduled job",
    "ticket_closeout": "Looks like a note about a ticket being closed",
    "duplicate_cluster": "Looks like a repeat of another memory",
    "stale_volatile": "Mentions things that change over time, and hasn't been checked in a while",
    "unconfirmed_inference": "A fact written by an agent that nobody has ever checked",
    "anchor_missing": "Describes a file or service that is no longer there",
    "pin_request": "An agent asked for this to be shown to every session",
    "low_value": "A model read this and could not find anything a future agent could act on",
}

# What a reviewer found when answering a confirm proposal. Two of these retract
# the record but for different reasons, and the difference is worth keeping:
# "no_longer_current" means the world moved, while "not_useful" means the memory
# was never actionable enough to be worth carrying. The second is the signal a
# usefulness check would need to learn from — age and shape cannot detect it.
OUTCOMES = ("still_current", "no_longer_current", "not_useful")
RETRACTING_OUTCOMES = ("no_longer_current", "not_useful")

# The question the reviewer is actually being asked. Keyed by action, with a
# per-rule override where the action's generic question is the wrong one: an
# anchor_missing card is a confirm proposal mechanically, but asking "is this
# still current?" about a file that no longer exists invites the reader to
# answer a question nobody asked.
ACTION_PROMPTS = {
    "retract": "Should agents stop using this memory?",
    "confirm": "Is this still current?",
}

RULE_PROMPTS = {
    "pin_request": "Should this be shown to every session, whatever the task?",
    "anchor_missing": "This points at something that is no longer there — is the memory still worth keeping?",
    "low_value": "Is there anything here a future session could act on?",
}


def prompt_for(proposal: dict) -> str:
    return RULE_PROMPTS.get(proposal.get("rule")) or ACTION_PROMPTS.get(
        proposal.get("action"), ""
    )


def _rationale_episodic_log(evidence: dict) -> str:
    return (
        "This reads like a single run of a recurring job, not something worth "
        "remembering later. That kind of note is kept in the activity trail "
        "instead, where it ages out on its own."
    )


def _rationale_ticket_closeout(evidence: dict) -> str:
    return (
        "This starts by reporting that a ticket was closed, which the ticket "
        "system already tracks. Worth keeping only if the rest of it says "
        "something that stays useful after the ticket is forgotten."
    )


def _rationale_duplicate_cluster(evidence: dict) -> str:
    count = len(evidence.get("records") or []) + (1 if evidence.get("keep") else 0)
    return (
        f"{count} memories here start with exactly the same text. The newest one "
        "is kept and the older copies are removed."
    )


def _rationale_stale_volatile(evidence: dict) -> str:
    age = evidence.get("age_days")
    when = f"in {age} days" if age else "in a long time"
    return (
        "This states something that tends to change — a version, address, port "
        f"or what is running where — and nobody has checked it {when}. Agents "
        "are still treating it as current."
    )


def _rationale_unconfirmed_inference(evidence: dict) -> str:
    age = evidence.get("age_days")
    when = f"after {age} days" if age is not None else "after a while"
    source_kind = evidence.get("source_kind") or "agent_inference"
    # Plain language about what answering will and won't do, so the reviewer is
    # not surprised: a "yes, still current" verdict stops the queue asking
    # about this record, but only a follow-up confirm with evidence clears the
    # ranking penalty. See plan.md Workstream 2.
    return (
        f"This is a fact written by an agent ({source_kind}) that nobody has "
        f"checked against the world yet, even {when}. It is being treated as "
        "trustworthy by ranking. Answering 'yes, still current' stops the "
        "queue from asking again, but only a 'Confirm with evidence' step "
        "clears that — reading the record is not the same as checking it."
    )


# Rationales are rebuilt from the rule and its evidence when a proposal is read,
# not replayed from the copy that was stored when it was queued. Wording gets
# revised; a proposal sitting in the queue for a week should not still be
# explaining itself in last week's words.
def _rationale_low_value(evidence: dict) -> str:
    reason = (evidence.get("reason") or "").strip()
    judged_by = evidence.get("model") or "the configured reviewer"
    opinion = f' It said: "{reason}"' if reason else ""
    return (
        f"This was read by {judged_by}, which judged that a future session could not "
        f"act on it.{opinion} That is an opinion, not a measurement — keep the record "
        "if it is useful to you."
    )


def _rationale_pin_request(evidence: dict) -> str:
    agent = evidence.get("requested_by") or "an agent"
    if evidence.get("pin") is False:
        return (
            f"{agent} asked to stop showing this to every session. Unpinning is how a "
            "standing rule stops applying, so it is worth confirming you agree it no "
            "longer should."
        )
    return (
        f"{agent} asked for this to become standing context — shown to every session in "
        "this scope, including other agents', without anyone searching for it. That is "
        "the most influential thing a record can be, so it is yours to grant rather "
        "than an agent's to take."
    )


def _rationale_anchor_missing(evidence: dict) -> str:
    detail = evidence.get("detail") or "the thing it describes could not be found"
    if evidence.get("looks_like_runtime_state"):
        return (
            f"A check found that {detail} — but that path looks like a runtime file "
            "(a database, log or backup) rather than something in the repository, so "
            "it was probably never the right thing to point at. The memory itself may "
            "be perfectly good. Fixing the pointer is usually the right answer here."
        )
    return (
        f"A check against what this memory points at found that {detail}. That "
        "usually means the memory is out of date, but not always — code gets "
        "moved and renamed while what the memory says about the system stays "
        "true, and the pointer can simply be wrong. Worth a look."
    )


RATIONALE_BUILDERS = {
    "episodic_log": _rationale_episodic_log,
    "ticket_closeout": _rationale_ticket_closeout,
    "duplicate_cluster": _rationale_duplicate_cluster,
    "stale_volatile": _rationale_stale_volatile,
    "unconfirmed_inference": _rationale_unconfirmed_inference,
    "anchor_missing": _rationale_anchor_missing,
    "pin_request": _rationale_pin_request,
    "low_value": _rationale_low_value,
}


# --- queue -----------------------------------------------------------------


def _decided_targets(rule: str) -> set[str]:
    """Target sets already ruled on, so a pass does not re-ask a settled question.

    Best-effort, read outside any lock. The lock-time recheck in
    `_decide_target_outcome` covers the race between this read and the
    subsequent INSERT.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT target_ids_json FROM memory_proposals "
            "WHERE rule = ? AND status IN ('pending', 'accepted', 'rejected')",
            (rule,),
        ).fetchall()
    return {row["target_ids_json"] for row in rows}


# SQLite raises the same `sqlite3.IntegrityError` class for UNIQUE-index
# violations and CHECK-constraint violations. We catch only the UNIQUE case
# (the one our race can produce) and re-raise CHECK violations — those
# indicate a real bug in a rule or a caller passing a bad value, and
# silently reporting them as `skipped_at_cap` would hide the bug. The error
# message format is stable across CPython's SQLite binding.
_UNIQUE_TARGET_ERROR_SUBSTRING = (
    "UNIQUE constraint failed: memory_proposals.rule, memory_proposals.target_ids_json"
)


def _decide_target_outcome(conn, rule: str, target_key: str) -> str:
    """Inside the lock: classify this (rule, target_ids_json) into one of:

    * ``"insert"``     — no proposal exists with this key in any settled
      status (pending/accepted/rejected); safe to insert a fresh
      pending row.
    * ``"skipped"``    — a proposal exists with this key in a settled
      status. The decision is already made; the caller reports it as
      skipped.

    The status filter is explicit because the schema also has
    `stale`, which is not a "settled" state — a `stale` row is one the
    maintenance pass marked inactive but did not retire, and a new
    proposal for the same target SHOULD proceed (the staleness was
    operational, not a verdict). Pre-fix the query had no status
    filter and would falsely report `stale` rows as settled.
    """
    row = conn.execute(
        "SELECT status FROM memory_proposals "
        "WHERE rule = ? AND target_ids_json = ? "
        "AND status IN ('pending', 'accepted', 'rejected') "
        "LIMIT 1",
        (rule, target_key),
    ).fetchone()
    if row is None:
        return "insert"
    return "skipped"


def _atomic_queue_one(
    conn,
    rule: str,
    action: str,
    scope: str,
    target_ids: list[str],
    evidence: dict,
    rationale: str,
) -> Optional[str]:
    """Insert one proposal under an already-held lock.

    Returns the new proposal id, or ``None`` if this `(rule, target_ids_json)`
    is already settled. Used by both ``queue_proposal`` (a one-shot insert
    from outside the scan) and ``generate_proposals`` (a per-candidate
    insert from inside the round-robin loop). Doing the
    pending/accepted/rejected check under the same lock as the INSERT is
    what guarantees convergence — the read-phase ``_decided_targets``
    call is now best-effort only, and the lock-time check is the truth.
    """
    target_key = json.dumps(sorted(target_ids))
    if _decide_target_outcome(conn, rule, target_key) != "insert":
        return None
    proposal_id = secrets.token_urlsafe(12)
    try:
        conn.execute(
            """
            INSERT INTO memory_proposals
            (id, rule, action, scope, target_ids_json, rationale, evidence_json,
             status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                proposal_id,
                rule,
                action,
                scope,
                target_key,
                rationale,
                json.dumps(evidence or {}),
                utc_now_iso(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        # The UNIQUE case is the only one our race can produce. A CHECK
        # violation looks identical to Python, and silently reporting it as
        # "skipped at cap" would hide a real bug — re-raise those.
        if _UNIQUE_TARGET_ERROR_SUBSTRING not in str(exc):
            raise
        # A peer thread inside its own IMMEDIATE lock slipped a row in
        # between our recheck and our INSERT. The check above is the
        # common path; this catch is the belt-and-braces for the
        # narrow window where a peer's commit lands between SELECT
        # and INSERT. Report as already settled.
        return None
    return proposal_id


def queue_proposal(
    rule: str,
    action: str,
    scope: str,
    target_ids: list[str],
    evidence: Optional[dict] = None,
) -> Optional[str]:
    """Queue one proposal from outside the scan, e.g. the verification pass.

    Returns the new proposal id, or ``None`` if this rule has already asked
    about exactly these records — the same convergence guarantee the scan
    gets, so a pass that runs nightly does not re-ask a settled question
    every night.

    Holds ``BEGIN IMMEDIATE`` for the duration of the recheck + insert so
    a peer writer can't slip a row between our check and our insert. A
    pre-lock read against ``_decided_targets`` runs first as a fast
    path; the lock-time check inside ``_atomic_queue_one`` is the
    authoritative one.
    """
    target_key = json.dumps(sorted(target_ids))
    if target_key in _decided_targets(rule):
        return None
    rationale = RATIONALE_BUILDERS.get(rule, lambda _e: "")(evidence or {})
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            return _atomic_queue_one(
                conn,
                rule,
                action,
                scope,
                target_ids,
                evidence or {},
                rationale,
            )
        finally:
            conn.commit()


def _proposal_pending_cap_per_rule() -> int:
    return memory_service._system_setting_int(
        "proposal_pending_cap_per_rule", PROPOSAL_PENDING_CAP_PER_RULE_DEFAULT
    )


def _proposal_pending_cap_total() -> int:
    return memory_service._system_setting_int(
        "proposal_pending_cap_total", PROPOSAL_PENDING_CAP_TOTAL_DEFAULT
    )


def _proposal_generation_budget_per_run() -> int:
    return memory_service._system_setting_int(
        "proposal_generation_budget_per_run",
        PROPOSAL_GENERATION_BUDGET_PER_RUN_DEFAULT,
    )


def _unconfirmed_inference_days_setting() -> int:
    return memory_service._system_setting_int(
        "unconfirmed_inference_days", UNCONFIRMED_INFERENCE_DAYS_DEFAULT
    )


def _unconfirmed_inference_min_importance_setting() -> float:
    return memory_service._system_setting_float(
        "unconfirmed_inference_min_importance",
        UNCONFIRMED_INFERENCE_MIN_IMPORTANCE_DEFAULT,
    )


def _pending_count_for(conn, rule: str, scope: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM memory_proposals "
        "WHERE status = 'pending' AND rule = ? AND scope = ?",
        (rule, scope),
    ).fetchone()
    return int(row["n"]) if row else 0


def _pending_count_total(conn) -> int:
    """Installation-wide pending count — never scope-filtered.

    Bypassing the aggregate cap by calling generate_proposals one scope at a
    time would let the queue grow past `proposal_pending_cap_total` because
    each call would only see its own scope's pending count. The aggregate
    check is always over the whole `memory_proposals` table. See plan.md
    Workstream 5 round-3 review.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM memory_proposals WHERE status = 'pending'"
    ).fetchone()
    return int(row["n"]) if row else 0


def _last_served_at(conn, rule: str, scope: str) -> Optional[str]:
    """Cross-run fairness sort key.

    `MAX(created_at)` across every proposal ever created for this
    `(rule, scope)` group, regardless of status. A group that has never been
    served returns None and sorts first; a group served at any point returns
    its last-served instant and sorts later. This is monotonic — once a
    group is served its timestamp advances to "now" and cannot tie the way a
    pending count of zero (after a reviewer clears the queue) repeatedly
    does. See plan.md Workstream 5 round-4 and round-5 reviews.
    """
    row = conn.execute(
        "SELECT MAX(created_at) AS last_at FROM memory_proposals "
        "WHERE rule = ? AND scope = ?",
        (rule, scope),
    ).fetchone()
    if row is None or row["last_at"] is None:
        return None
    return row["last_at"]


def _target_key(candidate: dict) -> str:
    return json.dumps(sorted(candidate["target_ids"]))


def _candidate_record_active(conn, candidate: dict) -> Optional[dict]:
    """Re-read every target row for insert-time revalidation.

    Returns the current row of the FIRST target if and only if every target
    in the candidate's `target_ids` list still exists, is active, and is
    not pinned. Returns None otherwise — the candidate must not be queued.

    A candidate can carry multiple targets (duplicate_cluster proposals list
    every record in the cluster; the partial unique index on
    `(rule, target_ids_json)` then protects the entire set as one unit).
    Validating only the first target would let the other members of the
    set slip through with a retracted/superseded/pinned status between the
    read phase and the lock — and would still queue the cluster, with the
    partial index then making the second-target drift invisible until
    something later tries to act on a row that's no longer eligible.

    Returns a list-shaped view (`first_row, all_ok`) so the caller can keep
    the eligibility predicate working without re-fetching. The first row is
    used downstream for the eligibility recheck (e.g., the
    `_RULE_ELIGIBILITY` predicates that key off the candidate's own
    properties, which only matter for the first target in a multi-target
    candidate — the other targets either pass the same status/pinned check
    or the whole candidate is dropped).
    """
    target_ids = candidate.get("target_ids") or []
    if not target_ids:
        return None
    placeholders = ",".join("?" for _ in target_ids)
    rows = conn.execute(
        f"SELECT id, record_status, COALESCE(pinned, 0) AS pinned, "
        f"       memory_class, source_kind, last_confirmed_at, created_at, "
        f"       topic, scope, content "
        f"FROM memory_records WHERE id IN ({placeholders})",
        tuple(target_ids),
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    # Every target must still exist and pass the active+unpinned check.
    for tid in target_ids:
        row = by_id.get(tid)
        if row is None:
            return None
        if row["record_status"] != "active":
            return None
        if row["pinned"]:
            return None
    # Re-fetch the first row in full so downstream callers (the
    # eligibility predicate) see the same shape as before.
    full_first = conn.execute(
        f"SELECT {memory_service.MEMORY_RECORD_COLUMNS} FROM memory_records "
        "WHERE id = ?",
        (target_ids[0],),
    ).fetchone()
    return dict(full_first) if full_first else None


def generate_proposals(
    scope: Optional[str] = None, rules: Optional[list[str]] = None
) -> dict:
    """Run the rules and queue anything not already proposed or decided.

    Split into a read phase (outside any lock) and a short write phase
    (inside `BEGIN IMMEDIATE`). The lock is held only for the count recheck
    and the inserts — it never covers the corpus scan, so unrelated writers
    against `memory_records` are not blocked for the duration of a slow pass.

    Capping and fairness (see plan.md Workstream 5):
    * `proposal_pending_cap_per_rule` — per-`(rule, scope)` pending ceiling.
    * `proposal_pending_cap_total` — installation-wide pending ceiling.
    * `proposal_generation_budget_per_run` — max new rows per call.
    * Insert order: groups are sorted by `MAX(created_at)` across every
      proposal ever created for them, ascending — a group with no history
      sorts first, and a group whose latest proposal is "now" sorts last.
    * Within a single run, candidates are taken round-robin from each
      non-empty group's sorted head until the budget or any cap is hit.
    """
    cap_per_rule = _proposal_pending_cap_per_rule()
    cap_total = _proposal_pending_cap_total()
    budget = _proposal_generation_budget_per_run()
    selected = rules or list(RULES)
    unknown_rules = [r for r in selected if r not in RULES]

    # --- read phase (no lock) ----------------------------------------------
    # Read every active record once, then let each selected rule produce its
    # own candidate list. Filtering out already-decided targets up front is
    # the same convergence guarantee the previous implementation gave.
    records = _active_records(scope)
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    already_known: set[str] = set()
    for rule_name in selected:
        rule = RULES.get(rule_name)
        if not rule:
            continue
        try:
            candidates = rule(records)
        except Exception:
            logger.exception("Proposal rule %s failed; skipping", rule_name)
            continue
        seen_for_rule = _decided_targets(rule_name)
        for candidate in candidates:
            target_key = _target_key(candidate)
            if target_key in seen_for_rule:
                already_known.add(target_key)
                continue
            seen_for_rule.add(target_key)
            groups[(rule_name, candidate["scope"])].append(candidate)

    # Cross-run fairness sort: never-served first, then by last-served time
    # ascending. We read this in its own transaction (no lock) — it is a
    # hint, not the truth; the recheck inside the lock is the truth.
    with get_db() as conn:
        group_keys = sorted(
            groups.keys(),
            key=lambda key: (
                _last_served_at(conn, key[0], key[1]) or "",
                key[0],
                key[1],
            ),
        )

    inserted_count = 0
    skipped_at_cap = 0
    created: list[dict] = []
    if not group_keys or not groups:
        return {
            "created": 0,
            "skipped_already_known": len(already_known),
            "skipped_at_cap": 0,
            "proposals": created,
            "unknown_rules": unknown_rules,
        }

    # --- write phase (BEGIN IMMEDIATE) -------------------------------------
    # The lock covers only the final count recheck and the inserts. Anything
    # between the read phase and this point that affected counts (e.g. another
    # call's inserts) is accounted for here.
    try:
        with get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                total_pending = _pending_count_total(conn)
                # Per-group recheck: pending count may have grown since the
                # read phase, the row may have been changed since the scan,
                # and the rule's own eligibility predicate must still pass.
                trimmed_groups: list[tuple[str, str, list[dict]]] = []
                for key in group_keys:
                    rule_name, group_scope = key
                    eligibility = _RULE_ELIGIBILITY.get(rule_name)
                    # Single pass over the candidates: drop already-settled
                    # rows FIRST so they don't waste a per-(rule, scope)
                    # cap slot that could have gone to a fresh target whose
                    # peer is also competing in the same group. A settled
                    # candidate is the same outcome `queue_proposal` would
                    # have produced — count it as `skipped_already_known`,
                    # not as a cap skip.
                    candidates_remaining = []
                    for candidate in groups[key]:
                        if (
                            _decide_target_outcome(
                                conn, rule_name, _target_key(candidate)
                            )
                            != "insert"
                        ):
                            already_known.add(_target_key(candidate))
                            continue
                        candidates_remaining.append(candidate)
                    if not candidates_remaining:
                        continue
                    room = cap_per_rule - _pending_count_for(
                        conn, rule_name, group_scope
                    )
                    if room <= 0:
                        skipped_at_cap += len(candidates_remaining)
                        continue
                    survivors: list[dict] = []
                    for candidate in candidates_remaining:
                        if len(survivors) >= room:
                            skipped_at_cap += 1
                            continue
                        current = _candidate_record_active(conn, candidate)
                        if current is None:
                            continue
                        if eligibility is not None and not eligibility(current):
                            continue
                        survivors.append(candidate)
                    if survivors:
                        trimmed_groups.append((rule_name, group_scope, survivors))

                # Round-robin insert over the (already fairness-sorted)
                # groups: take one candidate from each group in turn, repeat,
                # until budget or aggregate ceiling is exhausted.
                cursors = [0] * len(trimmed_groups)
                progressed = True
                while (
                    progressed and inserted_count < budget and total_pending < cap_total
                ):
                    progressed = False
                    for i, (rule_name, group_scope, survivors) in enumerate(
                        trimmed_groups
                    ):
                        if cursors[i] >= len(survivors):
                            continue
                        if (
                            _pending_count_for(conn, rule_name, group_scope)
                            >= cap_per_rule
                        ):
                            skipped_at_cap += len(survivors) - cursors[i]
                            cursors[i] = len(survivors)
                            continue
                        if total_pending >= cap_total:
                            break
                        candidate = survivors[cursors[i]]
                        cursors[i] += 1
                        target_key = _target_key(candidate)
                        proposal_id = _atomic_queue_one(
                            conn,
                            rule=rule_name,
                            action=candidate["action"],
                            scope=group_scope,
                            target_ids=sorted(candidate.get("target_ids") or []),
                            evidence=candidate.get("evidence") or {},
                            rationale=candidate.get("rationale") or "",
                        )
                        if proposal_id is None:
                            # Already settled (pending/accepted/rejected
                            # row exists for this (rule, target_ids_json)).
                            # This is the "no re-ask a settled question"
                            # guarantee, and the path through
                            # `_atomic_queue_one` is what makes it hold
                            # under contention — the recheck and the
                            # INSERT are inside the same lock.
                            skipped_at_cap += 1
                            continue
                        inserted_count += 1
                        total_pending += 1
                        created.append({"id": proposal_id, "rule": rule_name})
                        progressed = True
                        if inserted_count >= budget or total_pending >= cap_total:
                            break
                    if total_pending >= cap_total:
                        break
                # Anything we did not get to in this pass is also a cap skip.
                leftover = sum(
                    max(0, len(s[2]) - c)
                    for s, c in zip(trimmed_groups, cursors, strict=False)
                )
                skipped_at_cap += leftover
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except sqlite3.Error:
        logger.exception("generate_proposals failed; rolling back")
        raise

    return {
        "created": inserted_count,
        "skipped_already_known": len(already_known),
        "skipped_at_cap": skipped_at_cap,
        "proposals": created,
        "unknown_rules": unknown_rules,
    }


def _hydrate(row: dict) -> dict:
    proposal = dict(row)
    try:
        proposal["target_ids"] = json.loads(proposal.pop("target_ids_json") or "[]")
    except (TypeError, ValueError):
        proposal["target_ids"] = []
    try:
        proposal["evidence"] = json.loads(proposal.pop("evidence_json") or "{}")
    except (TypeError, ValueError):
        proposal["evidence"] = {}
    proposal["rule_description"] = RULE_DESCRIPTIONS.get(proposal["rule"], "")
    proposal["prompt"] = prompt_for(proposal)
    builder = RATIONALE_BUILDERS.get(proposal["rule"])
    if builder:
        # Stored rationale is kept as the historical record of what was said at
        # the time; what the reviewer sees is current wording.
        proposal["rationale"] = builder(proposal["evidence"])
    return proposal


def list_proposals(
    status: str = "pending",
    scope: Optional[str] = None,
    rule: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    conditions = []
    params: list = []
    if status and status != "all":
        conditions.append("status = ?")
        params.append(status)
    if scope:
        conditions.append("scope = ?")
        params.append(scope)
    if rule:
        conditions.append("rule = ?")
        params.append(rule)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([max(limit, 0), max(offset, 0)])

    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {PROPOSAL_COLUMNS} FROM memory_proposals {where} "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
    return [_hydrate(dict(row)) for row in rows]


def get_proposal(proposal_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            f"SELECT {PROPOSAL_COLUMNS} FROM memory_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
    return _hydrate(dict(row)) if row else None


def decide_proposal(
    proposal_id: str,
    verdict: str,
    decided_by: str,
    outcome: Optional[str] = None,
) -> dict:
    """Record a verdict, applying the action only on accept.

    The verdict answers "was this worth asking about?", which is what a rule's
    precision is measured on. `outcome` answers the separate question of what
    the reviewer actually found, and only applies to a confirm proposal: asking
    "is this still true?" is a useful question whether the answer is yes or no,
    so a record that turns out to be out of date is a hit for the rule, not a
    miss. Without this the reviewer had no way to say "no, it isn't" at all.

    Accepting a retraction retracts rather than deletes, so an accepted-in-error
    proposal stays recoverable for the retracted-retention window instead of
    being gone the moment the button is pressed.
    """
    if verdict not in ("accepted", "rejected"):
        raise ValueError("verdict must be 'accepted' or 'rejected'")
    if outcome is not None and outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(sorted(OUTCOMES))}")

    proposal = get_proposal(proposal_id)
    if not proposal:
        raise LookupError("Proposal not found")
    if proposal["status"] != "pending":
        raise ValueError(f"Proposal already {proposal['status']}")
    if outcome and proposal["action"] != "confirm":
        raise ValueError("outcome only applies to a confirm proposal")

    applied = 0
    if verdict == "accepted":
        if proposal["action"] == "retract" or outcome in RETRACTING_OUTCOMES:
            for record_id in proposal["target_ids"]:
                if memory_service.retract_memory(record_id):
                    applied += 1
        elif proposal["action"] == "pin":
            desired = proposal["evidence"].get("pin", True)
            # A refusal here (usually the scope being at its cap) is surfaced
            # rather than swallowed, and the proposal stays pending. Marking it
            # accepted while nothing happened would tell the operator they had
            # granted something they had not.
            for record_id in proposal["target_ids"]:
                if memory_service.set_pinned(record_id, bool(desired)):
                    applied += 1
        elif proposal["action"] == "confirm":
            # Answering "looks right" deliberately does NOT touch
            # last_confirmed_at. That field means the record was checked
            # against the world, and reading a record on screen is not
            # checking it — on the first real review pass thirteen records
            # were stamped as confirmed on the strength of a guess, and one of
            # them turned out to be wrong. The nag is already handled: a decided
            # proposal is never re-proposed, so the queue leaves the record
            # alone without anyone having to claim they verified it.
            applied = len(
                [
                    rid
                    for rid in proposal["target_ids"]
                    if memory_service.get_memory_record(rid)
                ]
            )

    with get_db() as conn:
        conn.execute(
            "UPDATE memory_proposals SET status = ?, decided_at = ?, decided_by = ?, "
            "applied_count = ? WHERE id = ?",
            (verdict, utc_now_iso(), decided_by, applied, proposal_id),
        )
        conn.commit()

    proposal.update(
        {
            "status": verdict,
            "decided_by": decided_by,
            "applied_count": applied,
            "outcome": outcome,
        }
    )
    return proposal


def reanchor_proposal(proposal_id: str, anchor: str, decided_by: str) -> dict:
    """Fix the pointer instead of judging the record, then re-check it.

    An anchor_missing proposal has three honest answers, not two: the memory is
    stale, the memory is fine and the pointer was wrong, or leave it. Without
    the middle one the queue pushes people to retract good memories because the
    only alternative on offer is "do nothing".
    """
    from app.services import verification_service

    proposal = get_proposal(proposal_id)
    if not proposal:
        raise LookupError("Proposal not found")
    if proposal["status"] != "pending":
        raise ValueError(f"Proposal already {proposal['status']}")

    updated = 0
    for record_id in proposal["target_ids"]:
        if memory_service.set_subject_anchor(record_id, anchor, changed_by=decided_by):
            updated += 1

    # Re-check immediately: if the new anchor is also wrong, say so now rather
    # than waiting for tonight's pass to raise the same question again.
    recheck = [
        verification_service.verify_record(memory_service.get_memory_record(rid))
        for rid in proposal["target_ids"]
        if memory_service.get_memory_record(rid)
    ]

    with get_db() as conn:
        conn.execute(
            "UPDATE memory_proposals SET status = 'accepted', decided_at = ?, "
            "decided_by = ?, applied_count = ? WHERE id = ?",
            (utc_now_iso(), decided_by, updated, proposal_id),
        )
        conn.commit()

    proposal.update(
        {
            "status": "accepted",
            "decided_by": decided_by,
            "applied_count": updated,
            "outcome": "anchor_fixed",
            "recheck": recheck,
        }
    )
    return proposal


def rule_stats() -> list[dict]:
    """Per-rule verdict history — the evidence for trusting a rule further.

    Precision is reported as None until a rule has been decided on at all,
    rather than as 0 or 1, so an untested rule is never mistaken for a bad or a
    perfect one.
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT rule,
                   SUM(status = 'pending') AS pending,
                   SUM(status = 'accepted') AS accepted,
                   SUM(status = 'rejected') AS rejected,
                   SUM(applied_count) AS records_affected
            FROM memory_proposals GROUP BY rule
            """
        ).fetchall()

    stats = []
    by_rule = {row["rule"]: row for row in rows}
    for rule_name in RULES:
        row = by_rule.get(rule_name)
        accepted = int(row["accepted"] or 0) if row else 0
        rejected = int(row["rejected"] or 0) if row else 0
        decided = accepted + rejected
        stats.append(
            {
                "rule": rule_name,
                "description": RULE_DESCRIPTIONS.get(rule_name, ""),
                "pending": int(row["pending"] or 0) if row else 0,
                "accepted": accepted,
                "rejected": rejected,
                "decided": decided,
                "precision": round(accepted / decided, 3) if decided else None,
                "records_affected": int(row["records_affected"] or 0) if row else 0,
            }
        )
    return stats
