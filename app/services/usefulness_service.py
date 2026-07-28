"""Ask a language model whether a record is actionable for a future session.

This is the one rule that cannot be mechanical. Every other consolidation rule
reasons about a record's shape, age, or pointer, and none of them can see that a
record is perfectly accurate and completely useless — "a deploy failed because
of missing dependencies", without saying which dependencies.

Three constraints shape the design.

**Off by default, and it discloses.** Judging a record means sending its content
to whatever model is configured. On a local-first system that is a decision the
operator has to make deliberately and per installation, so nothing here runs
until a review model is configured and the feature is switched on. Point it at a
model on your own machine and nothing leaves it; point it at a hosted one and
memory content does.

**It does not own the model.** Reaching one is `model_service`, shared with
every other feature that needs judgement, so an installation configures a model
once rather than per feature.

**It proposes, and is never allowed to do more.** Usefulness is the most
subjective judgement in the system and the one where a wrong call costs most: a
model that reads "do not edit vendored dependencies directly" as trivia would propose
deleting a load-bearing constraint. Other rules could earn automation from a
good accuracy record; this one is listed in NEVER_AUTOMATED so a future
auto-apply feature has to exclude it regardless of how well it scores.
"""

import logging
import re
from typing import Optional

from app.database import get_db
from app.services import memory_service

logger = logging.getLogger(__name__)

# Rules that must stay operator-reviewed however good their numbers look.
NEVER_AUTOMATED = frozenset({"low_value"})

DEFAULT_REVIEW_LIMIT = 20

# Signals that a record names something concrete a later session could act on.
# Used as a pre-filter so model calls are spent on the ambiguous ones rather than
# on records that plainly carry an identifier, a command, or a decision.
CONCRETE_SIGNALS = (
    re.compile(r"`[^`]{2,}`"),                        # quoted symbol, path or command
    re.compile(r"\b[\w./-]+\.(py|js|ts|tsx|sh|sql|ya?ml|json|md|toml|conf)\b"),
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|\blocalhost:\d+|:\d{4,5}\b"),
    re.compile(r"\bv?\d+\.\d+\.\d+\b"),               # a version
    re.compile(r"\b(should|must|do not|don't|never|always|prefer)\b", re.I),
    # "root cause" reliably precedes specifics; a bare "because" does not —
    # "the deploy failed because of missing dependencies" explains nothing while
    # sounding like it does, and that is the archetypal record this rule exists
    # to catch.
    re.compile(r"\b(root cause|caused by)\b", re.I),
)

PROMPT = """You are auditing one record from an AI agent's long-term memory.

Decide whether a future agent, working on this project weeks from now, could DO
something with this record that it could not do without it.

Judge only what is written. Do not assume missing detail exists elsewhere.

Answer "low_value" when the record narrates that something happened without
saying what to do about it, or is too vague to act on — for example it reports a
failure without naming the cause or the fix, or reports that work was completed
without saying what the outcome means for anyone later.

Answer "keep" when it gives a future agent something to act on — a constraint, a
decision and its reason, a root cause, a specific configuration or location, or
a gotcha with enough detail to avoid it again.

Record ({memory_class}):
---
{content}
---

Reply with JSON only, no other text:
{{"verdict": "keep" | "low_value", "reason": "<one sentence naming what it gives a future agent, or what is missing>"}}"""


def _setting(key: str, default: str = "") -> str:
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM system_settings WHERE key = ?", (key,)
            ).fetchone()
        return (row["value"] if row else default) or default
    except Exception:
        return default


def review_config() -> dict:
    """Whether this feature can run: enabled by the operator, and a model reachable."""
    from app.services import model_service

    enabled = _setting("usefulness_review_enabled", "0").lower() in ("1", "true", "yes")
    config = {
        "enabled": enabled,
        "model_available": model_service.is_available(),
        "limit": int(
            _setting("usefulness_review_limit", str(DEFAULT_REVIEW_LIMIT))
            or DEFAULT_REVIEW_LIMIT
        ),
    }
    config["ready"] = bool(enabled and config["model_available"])
    return config


def looks_concrete(content: str) -> bool:
    """True when a record plainly names something actionable.

    A cheap pre-filter, not a verdict: it decides where a model call is worth
    spending, and errs toward skipping. Anything it lets through is still judged
    by the model, and anything it holds back is simply left alone.
    """
    return any(pattern.search(content or "") for pattern in CONCRETE_SIGNALS)


def candidates(scope: Optional[str] = None, limit: int = DEFAULT_REVIEW_LIMIT) -> list[dict]:
    """Records worth spending a judgement on: active, and not obviously concrete."""
    conditions = [
        "record_status = 'active'",
        "memory_class IN ('fact', 'decision')",
        # Already judged, by the person whose opinion the rule is guessing at.
        "COALESCE(pinned, 0) = 0",
    ]
    params: list = []
    if scope:
        conditions.append("scope = ?")
        params.append(scope)

    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {memory_service.MEMORY_RECORD_COLUMNS} FROM memory_records "
            f"WHERE {' AND '.join(conditions)} ORDER BY created_at",
            params,
        ).fetchall()

    picked = []
    for row in rows:
        record = dict(row)
        if looks_concrete(record.get("content") or ""):
            continue
        picked.append(record)
        if len(picked) >= max(limit, 0):
            break
    return picked


def _extract_verdict(text: str) -> Optional[dict]:
    """Parse the model's reply, or None.

    Unparseable output is skipped rather than guessed at. A judge that cannot
    say what it decided has not decided anything, and defaulting either way
    would put words in its mouth on a rule that proposes deletions.
    """
    from app.services import model_service

    payload = model_service.extract_json(text)
    if not payload:
        return None
    verdict = str(payload.get("verdict", "")).strip().lower()
    if verdict not in ("keep", "low_value"):
        return None
    reason = str(payload.get("reason", "")).strip()
    return {"verdict": verdict, "reason": reason[:400]}



def judge_record(record: dict, config: Optional[dict] = None) -> Optional[dict]:
    """Ask the configured model about one record. None when it could not judge."""
    from app.services import model_service

    config = config or review_config()
    if not config.get("ready"):
        return None

    prompt = PROMPT.format(
        memory_class=record.get("memory_class"),
        content=(record.get("content") or "")[:4000],
    )
    return _extract_verdict(model_service.complete(prompt))


def _model_label() -> str:
    from app.services import model_service

    config = model_service.get_config()
    return config.get("model") or config.get("binding_id") or config.get("provider") or "a model"


def _not_ready_reason(config: dict) -> str:
    from app.services import model_service

    if not config["model_available"]:
        return model_service.describe_unavailable()
    return (
        "A review model is configured but this feature is off. Set "
        "usefulness_review_enabled to 1 to turn it on; record content is sent to "
        "whatever model you configured."
    )


def review_scope(scope: Optional[str] = None, limit: Optional[int] = None) -> dict:
    """Judge a capped batch of candidates and queue the low-value ones."""
    from app.services import memory_proposal_service

    config = review_config()
    if not config["ready"]:
        return {
            "ready": False,
            "reason": _not_ready_reason(config),
            "reviewed": 0,
            "low_value": 0,
            "kept": 0,
            "unjudged": 0,
            "proposals_queued": 0,
        }

    batch = candidates(scope, limit if limit is not None else config["limit"])
    low_value = kept = unjudged = queued = 0

    for record in batch:
        verdict = judge_record(record, config)
        if not verdict:
            unjudged += 1
            continue
        if verdict["verdict"] == "keep":
            kept += 1
            continue
        low_value += 1
        if memory_proposal_service.queue_proposal(
            rule="low_value",
            action="confirm",
            scope=record["scope"],
            target_ids=[record["id"]],
            evidence={
                "reason": verdict["reason"],
                # Attribute the verdict, so a later reader knows whose opinion it was.
                "model": _model_label(),
                "records": [memory_proposal_service._preview(record)],
            },
        ):
            queued += 1

    return {
        "ready": True,
        "reviewed": len(batch),
        "low_value": low_value,
        "kept": kept,
        "unjudged": unjudged,
        "proposals_queued": queued,
    }
