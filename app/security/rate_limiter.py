import threading
import time
from dataclasses import dataclass, field
from collections import OrderedDict
from typing import Optional


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
        ("memory_write", "agent"): TokenBucket(60, 60, 1.0),
        ("memory_search", "agent"): TokenBucket(60, 60, 1.0),
        ("vault_create", "user"): TokenBucket(10, 10, 1.0 / 60),
        ("login_failed", "user"): TokenBucket(10, 10, 1.0 / 60),
        ("otp_failed", "user"): TokenBucket(5, 5, 1.0 / 300),
    }

    @classmethod
    def check(
        cls,
        key_type: str,
        key_id: str,
        limit_name: str,
        count: int = 1,
    ) -> tuple[bool, dict]:
        bucket_key = f"{limit_name}:{key_type}:{key_id}"
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
    def reset(cls) -> None:
        with cls._lock:
            cls._buckets.clear()


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
