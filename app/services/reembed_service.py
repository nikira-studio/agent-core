"""Re-embed memory records for the configured embedding model.

Every stored vector is tagged with the model that produced it, and search only
scores vectors from the current model. Records are embedded when they are
written, so after the model changes the existing records have no usable vector
until they are embedded again. This module does that in the background.

A run embeds every record without a vector for the current model, active
records first. It is resumable by construction: each record's vector replaces
the old one as soon as it is made, so a run that stops part-way leaves the
records it finished done, and the next run picks up the rest.

Job state lives in system_settings rather than in process memory, so that a
server with several workers reports the same progress whichever worker answers,
and so two runs cannot start at once. A run renews its lease after every record;
a lease nobody renewed for LEASE_SECONDS belongs to a run that died with its
process, and a new run may take over.

Run from a shell inside the container:  python3 -m app.services.reembed_service
"""

import json
import logging
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.database import get_db
from app.services import embedding_service, system_settings_service, vector_service
from app.services.vector_settings_service import get_vector_model, is_vector_search_enabled

logger = logging.getLogger(__name__)

STATE_KEY = "vector_reembed_job"
# The lease is renewed after every record. One record takes at most the two
# 5 s backend checks plus the 30 s embed timeout, plus the optional pause, so
# a live run always renews well inside this window.
LEASE_SECONDS = 120
BATCH_SIZE = 20
# An optional pause after each record lets a run share a slow backend (one
# CPU-bound model serving other clients too) instead of saturating it.
MAX_PAUSE_MS = 30_000
# Give up after this many failures in a row: the backend is down or rejecting
# the model, and pressing on would only mark every record failed.
MAX_CONSECUTIVE_FAILURES = 5

_STATUS_ORDER = "CASE record_status WHEN 'active' THEN 0 WHEN 'superseded' THEN 1 ELSE 2 END"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _read_state() -> Optional[dict]:
    raw = system_settings_service.read_string(STATE_KEY)
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _lease_live(state: Optional[dict]) -> bool:
    if not state or state.get("status") != "running":
        return False
    try:
        renewed = datetime.fromisoformat(state["lease_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return _now() - renewed < timedelta(seconds=LEASE_SECONDS)


def coverage(model: Optional[str] = None) -> dict:
    """How many records have a vector for `model` (default: the configured one)."""
    model = model or get_vector_model()
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT
              SUM(CASE WHEN r.record_status = 'active' THEN 1 ELSE 0 END) AS active_total,
              SUM(CASE WHEN r.record_status = 'active' AND e.record_id IS NOT NULL THEN 1 ELSE 0 END) AS active_embedded,
              COUNT(*) AS total,
              SUM(CASE WHEN e.record_id IS NOT NULL THEN 1 ELSE 0 END) AS embedded
            FROM memory_records r
            LEFT JOIN memory_embeddings e ON e.record_id = r.id AND e.model = ?
            """,
            (model,),
        ).fetchone()
    active_total = row["active_total"] or 0
    active_embedded = row["active_embedded"] or 0
    total = row["total"] or 0
    embedded = row["embedded"] or 0
    return {
        "model": model,
        "active_total": active_total,
        "active_embedded": active_embedded,
        "active_missing": active_total - active_embedded,
        "total": total,
        "embedded": embedded,
        "missing": total - embedded,
    }


def status() -> dict:
    """The last or current run, plus coverage for the configured model."""
    state = _read_state()
    if state and state.get("status") == "running" and not _lease_live(state):
        state = {**state, "status": "interrupted"}
    return {
        "vector_search_enabled": is_vector_search_enabled(),
        "job": state,
        "coverage": coverage(),
    }


def _claim(started_by: str, pause_ms: int = 0) -> tuple[Optional[dict], Optional[str]]:
    """Take the run lease. Returns (state, None) or (None, error_code)."""
    if not is_vector_search_enabled():
        return None, "VECTOR_SEARCH_DISABLED"
    with get_db() as conn:
        # BEGIN IMMEDIATE takes the write lock up front, so two workers that
        # both read "no live run" cannot both go on to claim it.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key = ?", (STATE_KEY,)
        ).fetchone()
        current = None
        if row and row["value"]:
            try:
                current = json.loads(row["value"])
            except ValueError:
                current = None
        if _lease_live(current):
            conn.rollback()
            return None, "REEMBED_ALREADY_RUNNING"
        now = _iso(_now())
        state = {
            "run_id": secrets.token_urlsafe(9),
            "status": "running",
            "model": get_vector_model(),
            "started_by": started_by,
            "started_at": now,
            "lease_at": now,
            "finished_at": None,
            "embedded": 0,
            "failed": 0,
            "error": None,
            "pause_ms": max(0, min(int(pause_ms or 0), MAX_PAUSE_MS)),
        }
        conn.execute(
            "INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
            (STATE_KEY, json.dumps(state)),
        )
        conn.commit()
    return state, None


def _save_progress(state: dict) -> bool:
    """Write the run's progress if it still owns the job. One locked read-write,
    so a stop request made while a batch ran is carried over, never lost."""
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key = ?", (STATE_KEY,)
        ).fetchone()
        try:
            stored = json.loads(row["value"]) if row and row["value"] else None
        except ValueError:
            stored = None
        if not stored or stored.get("run_id") != state["run_id"]:
            conn.rollback()
            return False
        if stored.get("status") == "cancelling" and state.get("status") == "running":
            state["status"] = "cancelling"
        conn.execute(
            "UPDATE system_settings SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?",
            (json.dumps(state), STATE_KEY),
        )
        conn.commit()
    return True


def _still_ours(run_id: str) -> Optional[dict]:
    """The stored state if this run still owns it and was not asked to stop."""
    state = _read_state()
    if not state or state.get("run_id") != run_id:
        return None
    return state


def _next_batch(model: str, skip: set[str]) -> list[tuple[str, str]]:
    params: list = [model]
    skip_sql = ""
    if skip:
        skip_sql = f" AND r.id NOT IN ({','.join('?' for _ in skip)})"
        params.extend(skip)
    params.append(BATCH_SIZE)
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT r.id, r.content FROM memory_records r
            LEFT JOIN memory_embeddings e ON e.record_id = r.id AND e.model = ?
            WHERE e.record_id IS NULL{skip_sql}
            ORDER BY {_STATUS_ORDER}, r.created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [(r["id"], r["content"] or "") for r in rows]


def run(state: dict) -> dict:
    """Embed every record missing a vector for the run's model. Blocking."""
    run_id = state["run_id"]
    model = state["model"]
    failed_ids: set[str] = set()
    consecutive_failures = 0
    error: Optional[str] = None
    final_status = "completed"

    try:
        while True:
            current = _still_ours(run_id)
            if current is None:
                return state  # another run took over; leave its state alone
            if current.get("status") == "cancelling":
                final_status = "cancelled"
                break
            if get_vector_model() != model:
                final_status, error = "failed", "The embedding model changed during the run; start it again."
                break
            if not is_vector_search_enabled():
                final_status, error = "failed", "Vector search was turned off during the run."
                break

            batch = _next_batch(model, failed_ids)
            if not batch:
                break
            for record_id, content in batch:
                vector, embed_status = embedding_service.generate_embedding(content)
                if vector is not None and vector_service.store_embedding(record_id, vector):
                    state["embedded"] += 1
                    consecutive_failures = 0
                else:
                    failed_ids.add(record_id)
                    state["failed"] += 1
                    consecutive_failures += 1
                    logger.warning("Re-embed of %s failed: %s", record_id, embed_status)
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        error = (
                            f"Stopped after {consecutive_failures} failures in a row "
                            f"(last: {embed_status}). Check the embedding backend and model."
                        )
                        break
                # Renew after every record, not every batch: on a slow backend
                # a batch can outlast the lease, and a second run would start.
                state["lease_at"] = _iso(_now())
                if not _save_progress(state):
                    return state  # another run took over; leave its state alone
                if state["status"] == "cancelling":
                    break
                if state.get("pause_ms"):
                    time.sleep(state["pause_ms"] / 1000)
            if error:
                final_status = "failed"
                break
            if state["status"] == "cancelling":
                final_status = "cancelled"
                break
    except Exception as e:  # never leave a dead run looking alive
        logger.exception("Re-embed run %s failed", run_id)
        final_status, error = "failed", f"{type(e).__name__}: {e}"

    state.update(
        status=final_status,
        error=error,
        finished_at=_iso(_now()),
        lease_at=_iso(_now()),
    )
    if not _save_progress(state):
        return state
    logger.info(
        "Re-embed run %s %s: %d embedded, %d failed",
        run_id, final_status, state["embedded"], state["failed"],
    )
    return state


def start(started_by: str, pause_ms: int = 0) -> tuple[Optional[dict], Optional[str]]:
    """Claim the lease and run in a background thread."""
    state, err = _claim(started_by, pause_ms)
    if err:
        return None, err
    threading.Thread(
        target=run, args=(dict(state),), name="vector-reembed", daemon=True
    ).start()
    return state, None


def cancel() -> bool:
    """Ask the running job to stop after the record it is on."""
    with get_db() as conn:
        # Locked like _save_progress, so the two cannot overwrite each other.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key = ?", (STATE_KEY,)
        ).fetchone()
        try:
            state = json.loads(row["value"]) if row and row["value"] else None
        except ValueError:
            state = None
        if not _lease_live(state):
            conn.rollback()
            return False
        state["status"] = "cancelling"
        conn.execute(
            "UPDATE system_settings SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?",
            (json.dumps(state), STATE_KEY),
        )
        conn.commit()
    return True


# How long a measurement may wait: the first call can include loading the model.
_MEASURE_TIMEOUT_SECONDS = 120


_SAMPLE_SENTENCE = (
    "The deploy procedure runs docker compose from the project checkout, "
    "rebuilds the image, and restarts the service; migrations run on start. "
)
_DEFAULT_SAMPLE_CHARS = 900


def _sample_text() -> str:
    """Synthetic text as long as an average active record.

    Never a real record: an estimate can be pointed at an endpoint that is not
    saved yet, possibly a hosted provider, and memory content must not leave
    the install that way. Only the average length is read, which is metadata.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT AVG(LENGTH(content)) AS n FROM memory_records "
            "WHERE record_status = 'active' AND content IS NOT NULL"
        ).fetchone()
    length = int(row["n"]) if row and row["n"] else _DEFAULT_SAMPLE_CHARS
    length = max(length, 1)
    repeats = length // len(_SAMPLE_SENTENCE) + 1
    return (_SAMPLE_SENTENCE * repeats)[:length]


def estimate(
    model: Optional[str] = None,
    url: Optional[str] = None,
    provider: Optional[str] = None,
    auth_type: Optional[str] = None,
    api_key: Optional[str] = None,
) -> dict:
    """Embed a typical-length sample with `model` and project what a switch costs.

    Defaults to the saved provider, model and URL, so it can also be asked about
    a model before it is saved. A first call warms the model (loading it can take
    a while), and the second, on the sample record, is the one timed.
    """
    import httpx

    from app.services.vector_settings_service import (
        get_vector_provider,
        get_vector_url,
        normalize_provider,
    )

    model = (model or get_vector_model()).strip()
    provider = normalize_provider(provider) if provider else get_vector_provider()
    url = (url or get_vector_url()).strip().rstrip("/")
    headers = embedding_service.headers_for_endpoint(url, auth_type, api_key)
    text = _sample_text()

    def call(prompt: str) -> tuple[float, int]:
        started = time.monotonic()
        vector = embedding_service.request_embedding(
            provider, url, model, prompt, headers, timeout=_MEASURE_TIMEOUT_SECONDS
        )
        return time.monotonic() - started, len(vector)

    try:
        call("warm up")
        seconds, dimension = call(text)
    except httpx.TimeoutException:
        return {"ok": False, "model": model,
                "error": f"No answer within {_MEASURE_TIMEOUT_SECONDS} s for one record."}
    except embedding_service.EmbeddingRequestError as e:
        return {"ok": False, "model": model,
                "error": f"Could not measure {model}: {embedding_service.describe_error(e.code, provider, url)}"}
    except (httpx.HTTPError, ValueError) as e:
        return {"ok": False, "model": model, "error": f"Could not measure {model}: {e}"}

    cov = coverage(model)
    timeout = embedding_service.EMBED_TIMEOUT_SECONDS
    # A memory write embeds twice: once for the duplicate check, once to store.
    write_delay = round(seconds * 2, 2)
    if seconds >= timeout:
        level = "danger"
        message = (
            f"One record takes {seconds:.1f} s, longer than the {timeout} s embedding timeout. "
            "New records would get no vector at all, and a re-embed would fail."
        )
    elif write_delay >= timeout * 0.5:
        level = "warning"
        message = (
            f"Each memory write would wait about {write_delay:.0f} s for embeddings, "
            f"close to the {timeout} s timeout; longer records may time out and get no vector."
        )
    elif write_delay >= 2:
        level = "notice"
        message = f"Each memory write would wait about {write_delay:.1f} s for embeddings."
    else:
        level = "ok"
        message = "Embedding cost is small."
    return {
        "ok": True,
        "model": model,
        "dimension": dimension,
        "sample_chars": len(text),
        "seconds_per_record": round(seconds, 2),
        "records_to_embed": cov["missing"],
        "backfill_seconds": round(seconds * cov["missing"]),
        "write_delay_seconds": write_delay,
        "embed_timeout_seconds": timeout,
        "level": level,
        "message": message,
    }


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Embed memory records that lack a vector for the configured model.")
    parser.add_argument("--pause-ms", type=int, default=0,
                        help=f"pause after each record, to share a slow backend (max {MAX_PAUSE_MS})")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    before = coverage()
    print(
        f"Model {before['model']}: {before['active_embedded']} of {before['active_total']} "
        f"active records embedded ({before['missing']} records missing in all)."
    )
    if before["missing"] == 0:
        print("Nothing to do.")
        return 0
    state, err = _claim("cli", args.pause_ms)
    if err:
        print(f"Cannot start: {err}", file=sys.stderr)
        return 1
    final = run(state)
    after = coverage()
    print(
        f"{final['status']}: {final['embedded']} embedded, {final['failed']} failed. "
        f"Now {after['active_embedded']} of {after['active_total']} active records embedded."
    )
    if final.get("error"):
        print(final["error"], file=sys.stderr)
    return 0 if final["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
