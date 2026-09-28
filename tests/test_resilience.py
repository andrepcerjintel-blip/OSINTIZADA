import pytest

from osintizada.resilience import (
    BreakerState,
    CircuitBreaker,
    InMemoryTTLCache,
    RateLimiter,
    TokenBucket,
    TransientError,
    backoff_delay,
    cache_key,
    retry_async,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


async def test_token_bucket_waits_when_empty():
    clock = FakeClock()
    bucket = TokenBucket(60, clock=clock, sleep=clock.sleep)  # 1/s, burst 1
    assert await bucket.acquire() == 0
    waited = await bucket.acquire()
    assert waited == pytest.approx(1.0, abs=0.01)


async def test_token_bucket_burst():
    clock = FakeClock()
    bucket = TokenBucket(600, burst=5, clock=clock, sleep=clock.sleep)
    for _ in range(5):
        assert await bucket.acquire() == 0
    assert await bucket.acquire() > 0


def test_rate_limiter_cooldown():
    clock = FakeClock()
    rl = RateLimiter(clock=clock)
    rl.set_cooldown("p", 30)
    assert rl.cooldown_remaining("p") == 30
    clock.now += 31
    assert rl.cooldown_remaining("p") == 0


async def test_rate_limiter_without_config_does_not_wait():
    assert await RateLimiter().acquire("unconfigured") == 0


def test_circuit_breaker_lifecycle():
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=3, recovery_seconds=60, clock=clock)
    for _ in range(2):
        cb.record_failure()
    assert cb.state == BreakerState.CLOSED and cb.allow()
    cb.record_failure()
    assert cb.state == BreakerState.OPEN and not cb.allow()
    clock.now += 61
    assert cb.state == BreakerState.HALF_OPEN
    assert cb.allow()        # uma chamada de teste
    assert not cb.allow()    # demais aguardam
    cb.record_failure()      # teste falhou → reabre
    assert cb.state == BreakerState.OPEN
    clock.now += 61
    assert cb.allow()
    cb.record_success()
    assert cb.state == BreakerState.CLOSED and cb.consecutive_failures == 0


def test_backoff_is_exponential_and_capped():
    no_jitter = dict(jitter=0.0)
    assert backoff_delay(1, 0.5, 8, **no_jitter) == 0.5
    assert backoff_delay(3, 0.5, 8, **no_jitter) == 2.0
    assert backoff_delay(10, 0.5, 8, **no_jitter) == 8.0
    assert 0.375 <= backoff_delay(1, 0.5, 8, jitter=0.25) <= 0.625


async def test_retry_recovers_from_transient():
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TransientError("503")
        return "ok"

    clock = FakeClock()
    retries = []
    result = await retry_async(flaky, max_retries=2, base=0.1, maximum=1, sleep=clock.sleep,
                               on_retry=lambda n, e, d: retries.append(n))
    assert result == "ok" and retries == [1, 2]


async def test_retry_gives_up_and_does_not_retry_other_errors():
    clock = FakeClock()

    async def always():
        raise TransientError("503")

    with pytest.raises(TransientError):
        await retry_async(always, max_retries=1, base=0.1, maximum=1, sleep=clock.sleep)

    calls = []

    async def fatal():
        calls.append(1)
        raise ValueError("bug")

    with pytest.raises(ValueError):
        await retry_async(fatal, max_retries=3, base=0.1, maximum=1, sleep=clock.sleep)
    assert len(calls) == 1


def test_cache_ttl_and_lru():
    clock = FakeClock()
    cache = InMemoryTTLCache(max_entries=2, clock=clock)
    cache.set("a", 1, 10)
    assert cache.get("a") == 1
    clock.now += 11
    assert cache.get("a") is None
    cache.set("a", 1, 10)
    cache.set("b", 2, 10)
    cache.get("a")
    cache.set("c", 3, 10)  # "b" é o menos usado recentemente
    assert cache.get("b") is None and cache.get("a") == 1
    cache.set("z", 1, 0)  # TTL 0 = não cachear
    assert cache.get("z") is None


def test_cache_key_components():
    base = cache_key("p", "1", "username", "x", "q")
    assert base == cache_key("p", "1", "username", "x", "q")
    assert base != cache_key("p", "2", "username", "x", "q")      # versão do parser
    assert base != cache_key("p", "1", "username", "x", "q2")     # consulta
    assert base != cache_key("p", "1", "username", "x", "q", {"n": 1})  # parâmetros
