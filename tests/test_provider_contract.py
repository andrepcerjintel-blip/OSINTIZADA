import asyncio

import httpx
import pytest

from osintizada.config import ProviderSettings, Settings, load_settings
from osintizada.core.enums import EntityType, IdentifierType, ProviderStatus
from osintizada.core.models import ProviderItem, ProviderResult
from osintizada.core.normalization import normalize
from osintizada.providers.base import (
    APIProvider,
    ProviderNotConfigured,
    ProviderTimeout,
)
from osintizada.resilience import ProviderRuntime

IDENT = normalize("example.com", IdentifierType.DOMAIN)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    async def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


class HTTPProbe(APIProvider):
    """Provider de teste que consulta um endpoint HTTP simulado."""

    name = "test.http_probe"
    supported_identifiers = frozenset({IdentifierType.DOMAIN})
    trusted_hosts = ("api.test",)

    async def _search(self, identifier, query):
        async with self.http_client() as client:
            result = await self.fetch(client, "GET", "https://api.test/x", not_found_ok=True)
        if result is None:
            return []
        return [ProviderResult(type=EntityType.IP, value="1.2.3.4", raw=result.json())]


def probe(settings, responses, clock=None):
    queue = list(responses)
    calls = []

    def handler(request):
        calls.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    clock = clock or Clock()
    rt = ProviderRuntime(transport=httpx.MockTransport(handler), clock=clock, sleep=clock.sleep)
    return HTTPProbe(settings, rt), calls, clock


OK = httpx.Response(200, json={"ok": True})


def test_provider_item_alias():
    assert ProviderItem is ProviderResult


@pytest.mark.parametrize("code", [400, 401, 403, 404, 500])
async def test_non_recoverable_errors_are_not_retried(settings, code):
    p, calls, _ = probe(settings, [httpx.Response(code), OK])
    resp = await p.search(IDENT)
    assert len(calls) == 1
    if code == 404:
        assert resp.status == ProviderStatus.NO_RESULTS  # 404 = "não existe" (resposta válida)
    elif code == 401:
        assert resp.status == ProviderStatus.AUTH_REQUIRED and resp.error_code == "HTTP_401"
    else:
        assert resp.status == ProviderStatus.FAILED and resp.error_code == f"HTTP_{code}"


@pytest.mark.parametrize("code", [502, 503, 504, 408])
async def test_recoverable_errors_are_retried_with_backoff(settings, code):
    p, calls, clock = probe(settings, [httpx.Response(code), OK])
    resp = await p.search(IDENT)
    assert resp.status == ProviderStatus.SUCCESS and len(calls) == 2
    assert clock.sleeps and resp.metadata["retries"][0]["attempt"] == 1


async def test_retries_are_bounded(settings):
    p, calls, clock = probe(settings, [httpx.Response(503)])
    resp = await p.search(IDENT)
    assert resp.status == ProviderStatus.FAILED and resp.error_code == "HTTP_503"
    assert len(calls) == settings.resilience.max_retries + 1
    assert clock.sleeps[1] > clock.sleeps[0] * 1.2  # backoff exponencial (com jitter)


async def test_429_short_retry_after_is_respected_and_retried_once(settings):
    p, calls, clock = probe(settings, [httpx.Response(429, headers={"Retry-After": "3"}), OK])
    resp = await p.search(IDENT)
    assert resp.status == ProviderStatus.SUCCESS and len(calls) == 2
    assert 3.0 in clock.sleeps


async def test_429_long_retry_after_is_not_retried(settings):
    p, calls, _ = probe(settings, [httpx.Response(429, headers={"Retry-After": "600"}), OK])
    resp = await p.search(IDENT)
    assert resp.status == ProviderStatus.RATE_LIMITED and resp.error_code == "HTTP_429"
    assert len(calls) == 1 and resp.metadata["retry_after"] == 600
    assert p.runtime.last_errors["test.http_probe"]["code"] == "HTTP_429"


async def test_429_does_not_insist_indefinitely(settings):
    p, calls, _ = probe(settings, [httpx.Response(429, headers={"Retry-After": "1"})])
    resp = await p.search(IDENT)
    assert resp.status == ProviderStatus.RATE_LIMITED
    assert len(calls) == settings.resilience.rate_limit_retries + 1


async def test_connection_error_code(settings):
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    clock = Clock()
    rt = ProviderRuntime(transport=httpx.MockTransport(down), clock=clock, sleep=clock.sleep)
    resp = await HTTPProbe(settings, rt).search(IDENT)
    assert resp.status == ProviderStatus.FAILED and resp.error_code == "CONNECTION_ERROR"


async def test_requests_per_second_is_applied_before_request(settings):
    settings.providers["test.http_probe"] = ProviderSettings(requests_per_second=2)
    clock = Clock()
    p, calls, _ = probe(settings, [OK], clock)
    assert p.rate_limit_per_minute == 120
    settings.providers["test.http_probe"].cache_ttl_seconds = 0
    for _ in range(4):
        await p.search(IDENT)
    assert len(calls) == 4
    assert clock.now == pytest.approx(1.0, abs=0.01)  # 2 req/s, rajada 2: 3ª e 4ª esperam 0,5s cada


async def test_provider_concurrency_limit(settings):
    settings.providers["test.slow"] = ProviderSettings(concurrency=2, cache_ttl=0)
    active, peak = 0, 0

    class Slow(APIProvider):
        name = "test.slow"
        supported_identifiers = frozenset({IdentifierType.DOMAIN})

        async def _search(self, identifier, query):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return []

    p = Slow(settings, ProviderRuntime())
    assert p.max_concurrency == 2 and p.cache_ttl == 0
    await asyncio.gather(*(p.search(IDENT, f"q{i}") for i in range(8)))
    assert peak == 2


async def test_typed_errors_map_to_status(settings):
    class Raiser(APIProvider):
        name = "test.raiser"
        supported_identifiers = frozenset({IdentifierType.DOMAIN})
        exc: Exception = ProviderNotConfigured("telethon ausente")

        async def _search(self, identifier, query):
            raise self.exc

    r = Raiser(settings, ProviderRuntime(sleep=Clock().sleep))
    assert (await r.search(IDENT)).status == ProviderStatus.NOT_CONFIGURED
    r.exc = ProviderTimeout("lento")
    resp = await r.search(IDENT, "outra")
    assert resp.status == ProviderStatus.TIMEOUT and resp.error_code == "TIMEOUT"


def test_config_short_names(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("providers:\n  rdap:\n    concurrency: 4\n    requests_per_second: 2\n    cache_ttl: 86400\n")
    s = load_settings(cfg)
    assert s.provider("rdap").max_concurrency == 4
    assert s.provider("rdap").cache_ttl_seconds == 86400
    assert s.provider("rdap").requests_per_second == 2


def test_unsupported_and_disabled_have_codes(settings):
    async def run():
        p, _, _ = probe(settings, [OK])
        a = await p.search(normalize("a@b.com", IdentifierType.EMAIL))
        settings.providers["test.http_probe"] = ProviderSettings(enabled=False)
        b = await p.search(IDENT)
        return a, b

    a, b = asyncio.run(run())
    assert a.error_code == "UNSUPPORTED_TYPE" and b.error_code == "DISABLED"
    assert isinstance(Settings(), Settings)
