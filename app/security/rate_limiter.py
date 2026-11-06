import threading
import time
from dataclasses import dataclass, field
from collections import OrderedDict
from typing import NamedTuple


class RateLimitConfig(NamedTuple):
    max_tokens: float
    refill_rate: float


@dataclass
class TokenBucket:
    tokens: float
    max_tokens: float
    refill_rate: float
    last_refill: float = field(default_factory=time.time)

    def consume(self, count: int = 1) -> bool:
        self._refill()
        if self.tokens >= count:
            self.tokens -= count
            return True
        return False

    def _refill(self):
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    @property
    def remaining(self) -> int:
        self._refill()
        return int(self.tokens)

    @property
    def reset_at(self) -> int:
        self._refill()
        if self.tokens >= 1:
            return int(time.time())
        return int(time.time() + (1 - self.tokens) / self.refill_rate)


class RateLimiter:
    _lock = threading.Lock()
    _buckets: "OrderedDict[str, TokenBucket]" = OrderedDict()
    _max_buckets = 10_000

    _limits = {
        ("memory_write", "agent"): RateLimitConfig(60, 1.0),
        ("memory_search", "agent"): RateLimitConfig(60, 1.0),
        ("credential_create", "user"): RateLimitConfig(10, 1.0 / 60),
        ("login_failed", "user"): RateLimitConfig(10, 1.0 / 60),
        ("otp_failed", "user"): RateLimitConfig(5, 1.0 / 300),
        ("inbound_webhook", "ip"): RateLimitConfig(60, 1.0),
        ("approval_decide", "user"): RateLimitConfig(30, 1.0 / 2),
        # The credential broker: a runner resolves a handful of references at start-up.
        ("broker_resolve", "ip"): RateLimitConfig(60, 1.0),
    }

    # Limits that guard sign-in. Kept in the database so every worker and every
    # restart sees the same count; an in-memory bucket per worker multiplied
    # the allowed guesses by the worker count and reset on every restart.
    _shared = frozenset({"login_failed", "otp_failed"})

    @classmethod
    def _shared_bucket(cls, bucket_key: str, template: RateLimitConfig, count: int) -> tuple[bool, dict]:
        """Refill, then take `count` tokens (0 to only look), in one locked transaction."""
        from app.database import get_db

        now = time.time()
        with get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT tokens, last_refill FROM rate_limit_buckets WHERE bucket_key = ?",
                (bucket_key,),
            ).fetchone()
            tokens = template.max_tokens
            if row is not None:
                elapsed = max(0.0, now - row["last_refill"])
                tokens = min(template.max_tokens, row["tokens"] + elapsed * template.refill_rate)
            allowed = tokens >= max(count, 1)
            if allowed and count:
                tokens -= count
            if count:
                conn.execute(
                    "INSERT INTO rate_limit_buckets (bucket_key, tokens, last_refill) VALUES (?, ?, ?) "
                    "ON CONFLICT(bucket_key) DO UPDATE SET tokens = excluded.tokens, last_refill = excluded.last_refill",
                    (bucket_key, tokens, now),
                )
            conn.commit()
        reset = int(now) if tokens >= 1 else int(now + (1 - tokens) / template.refill_rate)
        return allowed, {"limit": int(template.max_tokens), "remaining": int(tokens), "reset": reset}

    @classmethod
    def check(
        cls,
        key_type: str,
        key_id: str,
        limit_name: str,
        count: int = 1,
    ) -> tuple[bool, dict]:
        bucket_key = f"{limit_name}:{key_type}:{key_id}"
        if limit_name in cls._shared and (limit_name, key_type) in cls._limits:
            return cls._shared_bucket(bucket_key, cls._limits[(limit_name, key_type)], count)
        with cls._lock:
            if bucket_key not in cls._buckets:
                template = cls._limits.get((limit_name, key_type))
                if template is None:
                    return True, {"limit": -1, "remaining": -1, "reset": 0}
                cls._buckets[bucket_key] = TokenBucket(
                    tokens=template.max_tokens,
                    max_tokens=template.max_tokens,
                    refill_rate=template.refill_rate,
                )
                if len(cls._buckets) > cls._max_buckets:
                    cls._buckets.popitem(last=False)
            else:
                cls._buckets.move_to_end(bucket_key)

            bucket = cls._buckets[bucket_key]
            allowed = bucket.consume(count)
            cls._buckets.move_to_end(bucket_key)

            return allowed, {
                "limit": int(bucket.max_tokens),
                "remaining": bucket.remaining,
                "reset": bucket.reset_at,
            }

    @classmethod
    def peek(
        cls,
        key_type: str,
        key_id: str,
        limit_name: str,
    ) -> tuple[bool, dict]:
        """Inspect a bucket without consuming a token.

        Authentication uses this before expensive password hashing so an
        already-exhausted caller cannot keep spending bcrypt CPU.
        """
        bucket_key = f"{limit_name}:{key_type}:{key_id}"
        if limit_name in cls._shared and (limit_name, key_type) in cls._limits:
            return cls._shared_bucket(bucket_key, cls._limits[(limit_name, key_type)], 0)
        with cls._lock:
            bucket = cls._buckets.get(bucket_key)
            template = cls._limits.get((limit_name, key_type))
            if bucket is None:
                if template is None:
                    return True, {"limit": -1, "remaining": -1, "reset": 0}
                return True, {
                    "limit": int(template.max_tokens),
                    "remaining": int(template.max_tokens),
                    "reset": int(time.time()),
                }
            cls._buckets.move_to_end(bucket_key)
            remaining = bucket.remaining
            return remaining >= 1, {
                "limit": int(bucket.max_tokens),
                "remaining": remaining,
                "reset": bucket.reset_at,
            }

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._buckets.clear()
        try:
            from app.database import get_db

            with get_db() as conn:
                conn.execute("DELETE FROM rate_limit_buckets")
                conn.commit()
        except Exception:
            pass  # no database yet (unit tests, early startup)


class ConcurrentSearchGuard:
    _lock = threading.Lock()
    _active: dict[str, int] = {}
    _max_concurrent = 5

    @classmethod
    def acquire(cls, agent_id: str) -> bool:
        with cls._lock:
            current = cls._active.get(agent_id, 0)
            if current >= cls._max_concurrent:
                return False
            cls._active[agent_id] = current + 1
            return True

    @classmethod
    def release(cls, agent_id: str):
        with cls._lock:
            current = cls._active.get(agent_id, 0)
            if current > 0:
                cls._active[agent_id] = current - 1

    @classmethod
    def get_active(cls, agent_id: str) -> int:
        with cls._lock:
            return cls._active.get(agent_id, 0)

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._active.clear()


RL = RateLimiter
CSG = ConcurrentSearchGuard
