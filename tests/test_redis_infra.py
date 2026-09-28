import json

import fakeredis
import pytest

from osintizada.config import Settings
from osintizada.core.enums import IdentifierType, ProviderStatus
from osintizada.core.models import ProviderResult
from osintizada.core.normalization import normalize
from osintizada.infrastructure.locks import DistributedLock, case_lock
from osintizada.infrastructure.redis_cache import RedisCacheBackend, build_cache
from osintizada.observability.metrics import Metrics
from osintizada.providers.base import APIProvider
from osintizada.resilience import MemoryCacheBackend, ProviderRuntime, RateLimiter, cache_key

IDENT = normalize("example.com", IdentifierType.DOMAIN)


class Counting(APIProvider):
    name = "test.counting"
    supported_identifiers = frozenset({IdentifierType.DOMAIN})
    default_cache_ttl_seconds = 60
    calls = 0

    async def _search(self, identifier, query):
        Counting.calls += 1
        return [ProviderResult(type="IP", value="1.2.3.4", raw={"n": Counting.calls})]


@pytest.fixture
def server():
    return fakeredis.FakeServer()


def redis_cache(server):
    return RedisCacheBackend(fakeredis.FakeRedis(server=server), prefix="osintizada")


def test_cache_key_format_and_normalization():
    key = cache_key("infra.rdap", "2", "domain", "example.com", '"a  b"', {"x": 1})
    assert key.startswith("osintizada:provider:infra.rdap:2:") and len(key.rsplit(":", 1)[1]) == 64
    assert key == cache_key("infra.rdap", "2", "domain", "example.com", '"a b"', {"x": 1})  # espaços normalizados
    assert key != cache_key("infra.rdap", "3", "domain", "example.com", '"a b"', {"x": 1})
    assert key != cache_key("infra.dns", "2", "domain", "example.com", '"a b"', {"x": 1})


def test_redis_cache_hit_miss_ttl_and_json(server):
    cache = redis_cache(server)
    assert cache.get("k") is None and cache.misses == 1
    cache.set("k", {"a": [1, 2], "b": "ç"}, 30)
    assert cache.get("k") == {"a": [1, 2], "b": "ç"} and cache.hits == 1
    assert 0 < cache.ttl("k") <= 30
    raw = fakeredis.FakeRedis(server=server).get("k")
    assert json.loads(raw) == {"a": [1, 2], "b": "ç"}  # JSON puro, nunca pickle
    cache.set("zero", {"x": 1}, 0)
    assert cache.get("zero") is None


def test_redis_cache_corrupted_payload_is_discarded(server):
    client = fakeredis.FakeRedis(server=server)
    client.set("bad", b"\x80\x04\x95pickle-bytes")
    cache = RedisCacheBackend(client)
    assert cache.get("bad") is None
    assert client.get("bad") is None  # removido


async def test_cache_shared_between_processes(settings, server):
    """Dois runtimes (processos distintos) com o mesmo Redis: a 2ª chamada vem do cache."""
    Counting.calls = 0
    rt_a = ProviderRuntime(cache=redis_cache(server))
    rt_b = ProviderRuntime(cache=redis_cache(server))
    first = await Counting(settings, rt_a).search(IDENT)
    second = await Counting(settings, rt_b).search(IDENT)
    assert Counting.calls == 1
    assert first.status == second.status == ProviderStatus.SUCCESS
    assert second.metadata["cache_hit"] is True and second.results[0].raw == {"n": 1}


async def test_invalid_cached_response_triggers_new_execution(settings, server):
    Counting.calls = 0
    cache = redis_cache(server)
    rt = ProviderRuntime(cache=cache)
    provider = Counting(settings, rt)
    await provider.search(IDENT)
    key = next(fakeredis.FakeRedis(server=server).scan_iter("osintizada:provider:test.counting:*"))
    fakeredis.FakeRedis(server=server).set(key, json.dumps({"provider": "x", "status": "NOT_A_STATUS"}))
    resp = await provider.search(IDENT)
    assert resp.status == ProviderStatus.SUCCESS and Counting.calls == 2 and not resp.metadata.get("cache_hit")


async def test_redis_down_falls_back_to_executing_provider(settings):
    Counting.calls = 0
    down = fakeredis.FakeServer()
    down.connected = False  # simula Redis fora do ar
    broken = fakeredis.FakeRedis(server=down)
    rt = ProviderRuntime(cache=RedisCacheBackend(broken))
    resp = await Counting(settings, rt).search(IDENT)
    assert resp.status == ProviderStatus.SUCCESS and Counting.calls == 1
    assert rt.cache.errors >= 1


def test_build_cache_selects_backend(server):
    s = Settings()
    assert isinstance(build_cache(s), MemoryCacheBackend)
    s.cache.backend = "redis"
    assert isinstance(build_cache(s, fakeredis.FakeRedis(server=server)), RedisCacheBackend)


def test_memory_cache_uses_json_semantics():
    cache = MemoryCacheBackend()
    value = {"a": [1]}
    cache.set("k", value, 10)
    value["a"].append(2)  # mutação externa não altera o cache
    assert cache.get("k") == {"a": [1]}


# --- lock distribuído -----------------------------------------------------------------


def test_lock_ownership_renew_and_safe_release(server):
    a = case_lock(fakeredis.FakeRedis(server=server), "case-1", ttl_seconds=30)
    b = case_lock(fakeredis.FakeRedis(server=server), "case-1", ttl_seconds=30)
    assert a.acquire() and a.token
    assert not b.acquire()                  # já tem dono
    assert not b.release() and a.owned()    # B não libera o lock de A
    assert a.renew()
    assert a.release()
    assert b.acquire()                      # liberado corretamente


def test_expired_lock_cannot_be_renewed_by_old_owner(server):
    client = fakeredis.FakeRedis(server=server)
    a = DistributedLock(client, "case:x", ttl_seconds=30)
    assert a.acquire()
    client.delete(a.name)                   # expirou
    b = DistributedLock(fakeredis.FakeRedis(server=server), "case:x", ttl_seconds=30)
    assert b.acquire()
    assert not a.renew() and not a.release()
    assert b.owned()


def test_metrics_render_and_shared_counters(server):
    m = Metrics()
    m.bind_redis(fakeredis.FakeRedis(server=server))
    m.inc("provider_errors_total", provider="infra.dns", code="TIMEOUT")
    other = Metrics()
    other.bind_redis(fakeredis.FakeRedis(server=server))
    other.inc("provider_errors_total", provider="infra.dns", code="TIMEOUT")
    m.observe("job_duration_seconds", 3.2)
    text = m.render({'jobs{status="RUNNING"}': 2})
    assert 'osintizada_provider_errors_total{code="TIMEOUT",provider="infra.dns"} 2.0' in text
    assert "osintizada_job_duration_seconds_count 1" in text and 'osintizada_jobs{status="RUNNING"} 2' in text
    assert "# TYPE osintizada_provider_errors_total counter" in text
    assert "# TYPE osintizada_jobs gauge" in text


# --- rate limit compartilhado ------------------------------------------------------------------------


def shared_limiter(server, **kwargs):
    from osintizada.infrastructure.redis_rate_limit import RedisRateLimiter

    return RedisRateLimiter(fakeredis.FakeRedis(server=server), **kwargs)


def test_shared_rate_limit_bucket_is_global_across_processes(server):
    """Dois processos com a mesma cota: o token consumido por um não existe para o outro."""
    a, b = shared_limiter(server), shared_limiter(server)
    a.configure("search.brave", 60)
    b.configure("search.brave", 60)
    assert a.try_acquire("search.brave") == 0
    wait = b.try_acquire("search.brave")
    assert 0.9 <= wait <= 1.0  # 60/min → próximo token em ~1 s, para QUALQUER processo
    local_a, local_b = RateLimiter(), RateLimiter()  # comparação: limitador local não coordena
    local_a.configure("search.brave", 60)
    local_b.configure("search.brave", 60)
    assert local_a._buckets["search.brave"].wait_time() == local_b._buckets["search.brave"].wait_time() == 0


async def test_shared_rate_limit_acquire_waits_for_other_process(server):
    a, b = shared_limiter(server), shared_limiter(server)
    a.configure("p", 1200)  # 20/s → 50 ms por token (capacidade 20)
    b.configure("p", 1200)
    for _ in range(20):
        assert await a.acquire("p") == 0  # rajada consome o bucket compartilhado
    waited = await b.acquire("p")
    assert waited > 0  # o outro processo precisou esperar


async def test_429_cooldown_is_shared_between_processes(settings, server):
    a, b = shared_limiter(server), shared_limiter(server)
    a.set_cooldown("test.counting", 30)
    assert 29 < b.cooldown_remaining("test.counting") <= 30
    a.set_cooldown("test.counting", 5)  # nunca encurta um cooldown mais longo
    assert b.cooldown_remaining("test.counting") > 25
    Counting.calls = 0
    rt = ProviderRuntime(rate_limiter=b)
    response = await Counting(settings, rt).search(IDENT)
    assert response.status == ProviderStatus.RATE_LIMITED and response.error_code == "RATE_LIMIT_COOLDOWN"
    assert Counting.calls == 0  # o provider não foi chamado neste processo


async def test_shared_rate_limit_degrades_to_local_when_redis_down(server):
    limiter = shared_limiter(server)
    limiter.configure("p", 60)
    server.connected = False
    assert await limiter.acquire("p") == 0  # bucket local assume; nada quebra
    assert limiter.cooldown_remaining("p") == 0
    limiter.set_cooldown("p", 10)  # registrado ao menos localmente
    assert limiter.cooldown_remaining("p") > 9


def test_build_runtime_selects_shared_limiter_only_with_redis(server):
    from osintizada.infrastructure.redis_cache import build_runtime
    from osintizada.infrastructure.redis_rate_limit import RedisRateLimiter

    s = Settings()
    s.cache.backend = "redis"
    rt = build_runtime(s, fakeredis.FakeRedis(server=server))
    assert isinstance(rt.rate_limiter, RedisRateLimiter) and isinstance(rt.cache, RedisCacheBackend)
    s.resilience.shared_rate_limit = False
    assert type(build_runtime(s, fakeredis.FakeRedis(server=server)).rate_limiter) is RateLimiter
    s2 = Settings()  # sem Redis: tudo local
    rt2 = build_runtime(s2, None)
    assert type(rt2.rate_limiter) is RateLimiter and isinstance(rt2.cache, MemoryCacheBackend)


def test_separate_cache_redis_url(monkeypatch, server):
    from osintizada.infrastructure.redis_client import redis_cache_url

    s = Settings()
    monkeypatch.setenv("REDIS_URL", "redis://queue:6379/0")
    monkeypatch.delenv("REDIS_CACHE_URL", raising=False)
    assert redis_cache_url(s) == "redis://queue:6379/0"
    monkeypatch.setenv("REDIS_CACHE_URL", "redis://cache:6379/1")
    assert redis_cache_url(s) == "redis://cache:6379/1"
