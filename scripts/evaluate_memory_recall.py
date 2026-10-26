"""Evaluate real memory-search endpoints in a disposable database.

Offline vectors are deterministic mechanism checks, not semantic quality evidence.
Use --embedding-mode real with an explicit URL/model to measure actual embeddings.
"""

import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import struct
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def offline_vector(text):
    words = text.lower().split()
    aliases = {
        "cleanup": "maintenance",
        "passwords": "credential",
        "resuming": "briefing",
    }
    counts = Counter(aliases.get(w.strip(".,:"), w.strip(".,:")) for w in words)
    values = [0.0] * 64
    for word, count in counts.items():
        index = int.from_bytes(hashlib.sha256(word.encode()).digest()[:2], "big") % 64
        values[index] += count
    return struct.pack("64f", *values)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    parser.add_argument(
        "--evaluation-time", default=datetime.now(timezone.utc).isoformat()
    )
    parser.add_argument(
        "--embedding-mode", choices=["fts", "offline", "real"], default="fts"
    )
    parser.add_argument("--vector-url")
    parser.add_argument("--vector-model")
    parser.add_argument("--embedding-cache", type=Path)
    parser.add_argument("--samples", type=int, default=5)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")
    if args.embedding_mode == "real" and not (
        args.vector_url and args.vector_model and args.embedding_cache
    ):
        parser.error(
            "real mode needs --vector-url, --vector-model and --embedding-cache"
        )
    now = datetime.fromisoformat(args.evaluation_time).astimezone(timezone.utc)
    corpus_path = ROOT / "tests/fixtures/memory_recall.json"
    corpus = json.loads(corpus_path.read_text())
    cache = (
        json.loads(args.embedding_cache.read_text())
        if args.embedding_cache and args.embedding_cache.exists()
        else {}
    )
    with (
        tempfile.TemporaryDirectory(prefix="agent-core-recall-") as directory,
        ExitStack() as stack,
    ):
        from app.branding import ENV_PREFIX

        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    f"{ENV_PREFIX}DATA_PATH": directory,
                    f"{ENV_PREFIX}TEST_DB": str(Path(directory) / "eval.db"),
                },
            )
        )
        import app.database as database
        from app.config import settings

        stack.enter_context(
            patch.object(database, "DB_PATH_OVERRIDE", str(Path(directory) / "eval.db"))
        )
        stack.enter_context(patch.object(settings, "DATA_PATH", directory))
        database.init_db()
        from app.services import (
            embedding_service,
            memory_service,
            vector_settings_service,
        )
        from app.security.rate_limiter import RateLimiter

        stack.enter_context(patch.object(memory_service, "utc_now", return_value=now))
        vector_settings_service.save_vector_settings(
            enabled=args.embedding_mode != "fts",
            url=args.vector_url or "http://offline.invalid",
            model=args.vector_model or "offline-fixture",
        )
        embedding_service._reset_cache()
        real_embed = embedding_service.generate_embedding

        def embed(text):
            if args.embedding_mode == "offline":
                return offline_vector(text), "healthy"
            key = hashlib.sha256(
                (args.vector_url + "\n" + args.vector_model + "\n" + text).encode()
            ).hexdigest()
            if key not in cache:
                vector, status = real_embed(text)
                if vector is None:
                    raise RuntimeError(f"Embedding provider unavailable: {status}")
                cache[key] = vector.hex()
            return bytes.fromhex(cache[key]), "healthy"

        if args.embedding_mode != "fts":
            # Generate/cache before timings so local ranking excludes provider latency.
            embedding_digest = hashlib.sha256()
            for text in [
                *(r["content"] for r in corpus["records"]),
                *(q["query"] for q in corpus["queries"]),
            ]:
                vector = embed(text)[0]
                embedding_digest.update(len(vector).to_bytes(8, "big"))
                embedding_digest.update(vector)
            if args.embedding_cache:
                args.embedding_cache.write_text(
                    json.dumps(cache, sort_keys=True) + "\n"
                )
            stack.enter_context(
                patch.object(embedding_service, "generate_embedding", side_effect=embed)
            )
        stack.enter_context(
            patch.object(
                embedding_service,
                "safe_backend_status",
                return_value={
                    "backend": "healthy"
                    if args.embedding_mode != "fts"
                    else "disabled",
                    "model_configured": args.embedding_mode != "fts",
                },
            )
        )
        # Seed declared state directly, including history and feedback, without LLM writes.
        with database.get_db() as conn:
            for record in corpus["records"]:
                row = {k: v for k, v in record.items() if not k.endswith("_days")}
                row["created_at"] = (
                    now - timedelta(days=record.get("age_days", 1))
                ).isoformat()
                row.pop("age_days", None)
                for field, relative in [
                    ("valid_from", "valid_from_days"),
                    ("valid_to", "valid_to_days"),
                    ("last_confirmed_at", "last_confirmed_days"),
                    ("expires_at", "expires_days"),
                ]:
                    if relative in record:
                        row[field] = (
                            now + timedelta(days=record[relative])
                        ).isoformat()
                columns = ",".join(row)
                conn.execute(
                    f"INSERT INTO memory_records ({columns}) VALUES ({','.join('?' for _ in row)})",
                    list(row.values()),
                )
                if args.embedding_mode != "fts":
                    conn.execute(
                        "INSERT INTO memory_embeddings(record_id,vector,model) VALUES (?,?,?)",
                        (
                            row["id"],
                            embed(row["content"])[0],
                            args.vector_model or "offline-fixture",
                        ),
                    )
            conn.commit()
        from fastapi.testclient import TestClient
        from app.main import app

        # No lifespan: maintenance must never run in the evaluation database.
        client = TestClient(app)
        stack.callback(client.close)
        response = client.post(
            "/api/auth/register",
            json={
                "email": "eval@example.invalid",
                "password": "Fixture-password-123",
                "display_name": "Eval",
            },
        )
        response.raise_for_status()
        response = client.post(
            "/api/auth/login",
            json={"email": "eval@example.invalid", "password": "Fixture-password-123"},
        )
        response.raise_for_status()
        admin = response.json()["data"]["session_id"]
        headers = {"Authorization": f"Bearer {admin}"}
        response = client.post(
            "/api/agents",
            headers=headers,
            json={"id": "recall-eval", "display_name": "Recall eval"},
        )
        response.raise_for_status()
        response = client.post(
            "/api/integrations/generate-connection",
            headers=headers,
            json={"user_id": "eval", "agent_id": "recall-eval", "output_type": "env"},
        )
        response.raise_for_status()
        token = response.json()["data"]["api_key"]
        search_durations = []
        recall_durations = []
        original_search = memory_service.search_memory
        original_record_recall = memory_service.record_recall

        def timed_record_recall(*values, **options):
            start = time.perf_counter()
            try:
                return original_record_recall(*values, **options)
            finally:
                recall_durations.append((time.perf_counter() - start) * 1000)

        stack.enter_context(
            patch.object(
                memory_service, "record_recall", side_effect=timed_record_recall
            )
        )

        def timed_search(*values, **options):
            start = time.perf_counter()
            first_recall = len(recall_durations)
            try:
                return original_search(*values, **options)
            finally:
                # Older revisions account inside search, newer ones after the
                # response budget. Compare retrieval without either placement's
                # accounting cost; endpoint timing still includes that work.
                elapsed = (time.perf_counter() - start) * 1000
                search_durations.append(elapsed - sum(recall_durations[first_recall:]))

        stack.enter_context(
            patch.object(memory_service, "search_memory", side_effect=timed_search)
        )
        results = []
        for query in corpus["queries"]:
            params = {"query": query["query"], "limit": 5, **query["params"]}
            if "as_of_days" in params:
                params["as_of"] = (
                    now + timedelta(days=params.pop("as_of_days"))
                ).isoformat()
            times = []
            ranking_times = []
            for sample in range(args.samples + 1):
                with database.get_db() as conn:
                    conn.execute(
                        "UPDATE memory_records SET recall_count=0, last_recalled_at=NULL"
                    )
                    conn.commit()
                RateLimiter.reset()
                search_durations.clear()
                recall_durations.clear()
                start = time.perf_counter()
                response = client.post(
                    "/mcp",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"tool": "memory_search", "params": params},
                )
                elapsed = (time.perf_counter() - start) * 1000
                if sample:
                    times.append(elapsed)
                    ranking_times.extend(search_durations)
            expected_status = query.get("expected_status", 200)
            if response.status_code != expected_status:
                raise RuntimeError(
                    f"{query['id']}: {response.status_code}: {response.text}"
                )
            data = response.json().get("data", {})
            ids = [r["id"] for r in data.get("records", [])]
            expected = set(query["expected_ids"])
            forbidden = set(query.get("forbidden_ids", [])) & set(ids)
            ranks = [ids.index(id) + 1 for id in expected if id in ids]
            missing = expected - set(ids)
            probe_ids = []
            if missing and expected_status == 200:
                RateLimiter.reset()
                with database.get_db() as conn:
                    conn.execute(
                        "UPDATE memory_records SET recall_count=0, last_recalled_at=NULL"
                    )
                    conn.commit()
                probe = client.post(
                    "/mcp",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"tool": "memory_search", "params": {**params, "limit": 100}},
                )
                probe.raise_for_status()
                probe_ids = [r["id"] for r in probe.json()["data"]["records"]]
            results.append(
                dict(
                    id=query["id"],
                    category=query["category"],
                    returned_ids=ids,
                    missing_ids=sorted(missing),
                    found_below_requested_page=sorted(missing & set(probe_ids)),
                    not_found_in_100_result_probe=sorted(missing - set(probe_ids)),
                    hit=bool(ranks) if expected else not ids,
                    recall_at_5=len(expected & set(ids)) / len(expected)
                    if expected
                    else float(not ids),
                    reciprocal_rank=1 / min(ranks)
                    if ranks
                    else float(not expected and not ids),
                    forbidden_ids=sorted(forbidden),
                    retrieval_mode=data.get("retrieval_mode", "denied"),
                    latency_ms=times,
                    search_latency_ms=ranking_times,
                )
            )
        groups = defaultdict(list)
        for result in results:
            groups[result["category"]].append(result)

        def metrics(rows):
            times = sorted(t for r in rows for t in r["latency_ms"])
            ranking_times = sorted(t for r in rows for t in r["search_latency_ms"])
            return dict(
                hit_rate_at_5=statistics.mean(r["hit"] for r in rows),
                recall_at_5=statistics.mean(r["recall_at_5"] for r in rows),
                mean_reciprocal_rank=statistics.mean(
                    r["reciprocal_rank"] for r in rows
                ),
                forbidden_records=sum(len(r["forbidden_ids"]) for r in rows),
                p95_endpoint_ms=times[math.ceil(len(times) * 0.95) - 1],
                p95_search_ms=ranking_times[math.ceil(len(ranking_times) * 0.95) - 1]
                if ranking_times
                else None,
            )

        report = dict(
            evaluation_time=now.isoformat(),
            embedding_mode=args.embedding_mode,
            vector_model=args.vector_model,
            search_timing="retrieval_excluding_recall_accounting",
            embeddings_sha256=embedding_digest.hexdigest()
            if args.embedding_mode != "fts"
            else None,
            corpus_sha256=hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
            corpus_size=len(corpus["records"]),
            query_count=len(results),
            metrics=metrics(results),
            categories={k: metrics(v) for k, v in groups.items()},
            results=results,
        )
        if args.compare:
            baseline = json.loads(args.compare.read_text())
            for key in (
                "evaluation_time",
                "embedding_mode",
                "vector_model",
                "embeddings_sha256",
                "corpus_sha256",
                "search_timing",
            ):
                if baseline.get(key) != report[key]:
                    raise ValueError(f"Cannot compare reports with different {key}")
            report["delta"] = {
                key: report["metrics"][key] - baseline["metrics"][key]
                for key in report["metrics"]
            }
            report["regressed_queries"] = [
                row["id"]
                for row, old in zip(report["results"], baseline["results"], strict=True)
                if row["reciprocal_rank"] < old["reciprocal_rank"]
                or row["recall_at_5"] < old["recall_at_5"]
            ]
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {
                    k: report[k]
                    for k in ("embedding_mode", "corpus_size", "query_count", "metrics")
                },
                indent=2,
            )
        )
        if report["metrics"]["forbidden_records"]:
            raise SystemExit("Forbidden records returned")


if __name__ == "__main__":
    main()
