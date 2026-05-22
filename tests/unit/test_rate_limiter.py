from app.security.rate_limiter import TokenBucket, RateLimiter, ConcurrentSearchGuard


def test_token_bucket_consume():
    tb = TokenBucket(tokens=5.0, max_tokens=5.0, refill_rate=1.0)
    assert tb.remaining == 5
    assert tb.consume(2) == True
    assert tb.remaining == 3


def test_token_bucket_reject_when_empty():
    tb = TokenBucket(tokens=0.0, max_tokens=5.0, refill_rate=1.0)
    assert tb.consume(1) == False


def test_token_bucket_refill():
    import time
    tb = TokenBucket(tokens=0.0, max_tokens=5.0, refill_rate=10.0)
    time.sleep(0.1)
    tb._refill()
    assert tb.tokens > 0


def test_rate_limiter_check_allows():
    RL = RateLimiter()
    allowed, info = RL.check("agent", "testagent", "memory_write")
    assert allowed == True
    assert info["limit"] == 60


def test_rate_limiter_bucket_per_key():
    RL = RateLimiter()
    allowed1, _ = RL.check("agent", "agent1", "memory_write")
    allowed2, _ = RL.check("agent", "agent2", "memory_write")
    assert allowed1 == True
    assert allowed2 == True


def test_rate_limiter_prunes_old_buckets(monkeypatch):
    monkeypatch.setattr(RateLimiter, "_max_buckets", 2)
    RateLimiter._buckets.clear()
    try:
        RateLimiter.check("agent", "agent1", "memory_write")
        RateLimiter.check("agent", "agent2", "memory_write")
        RateLimiter.check("agent", "agent3", "memory_write")

        assert len(RateLimiter._buckets) == 2
        assert "memory_write:agent:agent1" not in RateLimiter._buckets
        assert "memory_write:agent:agent2" in RateLimiter._buckets
        assert "memory_write:agent:agent3" in RateLimiter._buckets
    finally:
        RateLimiter._buckets.clear()


def test_concurrent_search_guard_acquire_release():
    CSG = ConcurrentSearchGuard()
    assert CSG.acquire("testagent") == True
    assert CSG.get_active("testagent") == 1
    CSG.release("testagent")
    assert CSG.get_active("testagent") == 0


def test_concurrent_search_guard_limit():
    CSG = ConcurrentSearchGuard()
    for _ in range(5):
        assert CSG.acquire("testagent") == True
    assert CSG.acquire("testagent") == False
    CSG.release("testagent")
    assert CSG.acquire("testagent") == True
