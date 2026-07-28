"""Consolidation proposals for the memory corpus, and the verdicts on them.

Rules scan active memory and suggest what to retract or re-confirm. Nothing is
applied automatically: a proposal sits in a queue until an operator accepts or
rejects it, and that verdict is stored against the rule that produced it.

The verdict trail is the product here, not bookkeeping. It gives each rule a
measured precision from real decisions, which is the only honest basis for ever
letting one act unattended — and it stops a rejected suggestion from being
re-proposed on the next pass, so the queue converges instead of nagging.
"""

import json
import logging
import re
import secrets
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
DUPLICATE_PREFIX_CHARS = 90

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
        if len(normalized) < DUPLICATE_PREFIX_CHARS:
            continue
        key = (record["scope"], normalized[:DUPLICATE_PREFIX_CHARS])
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


RULES: dict[str, Callable[[list[dict]], list[dict]]] = {
    "episodic_log": _rule_episodic_log,
    "ticket_closeout": _rule_ticket_closeout,
    "duplicate_cluster": _rule_duplicate_cluster,
    "stale_volatile": _rule_stale_volatile,
}

# Shown to whoever is reviewing the queue, so they say what the memory looks
# like in plain terms rather than naming the mechanism that found it.
RULE_DESCRIPTIONS = {
    "episodic_log": "Looks like a one-off status update from a scheduled job",
    "ticket_closeout": "Looks like a note about a ticket being closed",
    "duplicate_cluster": "Looks like a repeat of another memory",
    "stale_volatile": "Mentions things that change over time, and hasn't been checked in a while",
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
    "anchor_missing": _rationale_anchor_missing,
    "pin_request": _rationale_pin_request,
    "low_value": _rationale_low_value,
}


# --- queue -----------------------------------------------------------------


def _decided_targets(rule: str) -> set[str]:
    """Target sets already ruled on, so a pass does not re-ask a settled question."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT target_ids_json FROM memory_proposals "
            "WHERE rule = ? AND status IN ('pending', 'accepted', 'rejected')",
            (rule,),
        ).fetchall()
    return {row["target_ids_json"] for row in rows}


def queue_proposal(
    rule: str,
    action: str,
    scope: str,
    target_ids: list[str],
    evidence: Optional[dict] = None,
) -> Optional[str]:
    """Queue one proposal from outside the scan, e.g. the verification pass.

    Returns the new proposal id, or None if this rule has already asked about
    exactly these records — the same convergence guarantee the scan gets, so a
    pass that runs nightly does not re-ask a settled question every night.
    """
    target_key = json.dumps(sorted(target_ids))
    if target_key in _decided_targets(rule):
        return None
    proposal_id = secrets.token_urlsafe(12)
    with get_db() as conn:
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
                RATIONALE_BUILDERS.get(rule, lambda _e: "")(evidence or {}),
                json.dumps(evidence or {}),
                utc_now_iso(),
            ),
        )
        conn.commit()
    return proposal_id


def generate_proposals(scope: Optional[str] = None, rules: Optional[list[str]] = None) -> dict:
    """Run the rules and queue anything not already proposed or decided."""
    records = _active_records(scope)
    selected = rules or list(RULES)
    created: list[dict] = []
    skipped = 0

    for rule_name in selected:
        rule = RULES.get(rule_name)
        if not rule:
            continue
        try:
            candidates = rule(records)
        except Exception:
            logger.exception("Proposal rule %s failed; skipping", rule_name)
            continue

        seen = _decided_targets(rule_name)
        for candidate in candidates:
            target_key = json.dumps(sorted(candidate["target_ids"]))
            if target_key in seen:
                skipped += 1
                continue
            seen.add(target_key)
            proposal_id = secrets.token_urlsafe(12)
            with get_db() as conn:
                conn.execute(
                    """
                    INSERT INTO memory_proposals
                    (id, rule, action, scope, target_ids_json, rationale, evidence_json,
                     status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        proposal_id,
                        rule_name,
                        candidate["action"],
                        candidate["scope"],
                        target_key,
                        candidate["rationale"],
                        json.dumps(candidate.get("evidence") or {}),
                        utc_now_iso(),
                    ),
                )
                conn.commit()
            created.append({"id": proposal_id, "rule": rule_name})

    return {
        "created": len(created),
        "skipped_already_known": skipped,
        "proposals": created,
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
                [rid for rid in proposal["target_ids"] if memory_service.get_memory_record(rid)]
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
