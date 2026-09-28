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
from osintizada.resilience import MemoryCacheBackend, ProviderRuntime, cache_key

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
