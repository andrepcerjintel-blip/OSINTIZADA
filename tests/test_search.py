import httpx
import pytest

from osintizada.config import ProviderSettings, Settings
from osintizada.core.enums import EntityType, IdentifierType, ProviderStatus, SearchMode
from osintizada.core.normalization import normalize
from osintizada.orchestration.search_manager import SearchManager
from osintizada.orchestration.source_orchestrator import SourceOrchestrator
from osintizada.providers.search.brave import BraveSearchProvider
from osintizada.providers.search.google_cse import GoogleCSEProvider
from osintizada.resilience import ProviderRuntime

BRAVE_KEY = "BSA-test-secret-key-0000"
USER = normalize("fearless1999", IdentifierType.USERNAME)


def brave_payload(*items):
    return {"web": {"results": [
        {"url": url, "title": title, "description": desc, "page_age": "2024-05-01T10:00:00"}
        for url, title, desc in items
    ]}}


DEFAULT_ITEMS = (
    ("https://forum.example/u/fearless1999", "Perfil <strong>fearless1999</strong>",
     "Contato fearless1999@gmail.com e telegram: @DarkFearless"),
    ("https://blog.example/post", "Post qualquer", "Sem menção direta"),
)


class Server:
    """Transport falso que registra requisições e devolve respostas programadas."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return item(request) if callable(item) else item


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def runtime_with(server: Server, clock: FakeClock | None = None) -> ProviderRuntime:
    clock = clock or FakeClock()

    async def resolver(host):
        return ["93.184.216.34"]

    return ProviderRuntime(transport=httpx.MockTransport(server), resolver=resolver, clock=clock, sleep=clock.sleep)


@pytest.fixture
def brave_env(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", BRAVE_KEY)


def brave(settings, server):
    return BraveSearchProvider(settings, runtime_with(server))


async def test_brave_parses_results_and_extracts_entities(settings, brave_env):
    server = Server(httpx.Response(200, json=brave_payload(*DEFAULT_ITEMS)))
    resp = await brave(settings, server).search(USER, '"fearless1999"')
    assert resp.status == ProviderStatus.SUCCESS

    req = server.requests[0]
    assert req.url.host == "api.search.brave.com"
    assert req.url.params["q"] == '"fearless1999"'
    assert req.headers["x-subscription-token"] == BRAVE_KEY

    pages = [r for r in resp.results if r.type == EntityType.URL]
    assert [p.source_url for p in pages] == ["https://forum.example/u/fearless1999", "https://blog.example/post"]
    first = pages[0]
    assert first.title == "Perfil fearless1999"  # HTML removido
    assert first.raw["rank"] == 1 and first.raw["identifier_in_snippet"] is True
    assert first.confidence > pages[1].confidence
    assert first.observed_at.year == 2024
    assert first.raw["published_at"].startswith("2024-05-01")  # data da fonte preservada p/ a timeline

    extracted = {(r.type, r.value) for r in resp.results if r.type != EntityType.URL}
    assert (EntityType.EMAIL, "fearless1999@gmail.com") in extracted
    assert (EntityType.TELEGRAM_USER, "darkfearless") in extracted
    assert not any(v == "fearless1999" for _, v in extracted)  # o próprio identificador não é "descoberta"


async def test_empty_results_is_no_results(settings, brave_env):
    server = Server(httpx.Response(200, json={"web": {"results": []}}))
    assert (await brave(settings, server).search(USER)).status == ProviderStatus.NO_RESULTS


async def test_rate_limit_sets_cooldown_and_blocks_next_call(settings, brave_env):
    server = Server(httpx.Response(429, headers={"Retry-After": "30"}))
    p = brave(settings, server)
    first = await p.search(USER, "q1")
    assert first.status == ProviderStatus.RATE_LIMITED and first.metadata["retry_after"] == 30
    second = await p.search(USER, "q2")
    assert second.status == ProviderStatus.RATE_LIMITED
    assert len(server.requests) == 1  # não insiste durante o cooldown


async def test_auth_error(settings, brave_env):
    server = Server(httpx.Response(401, json={"error": "invalid token"}))
    resp = await brave(settings, server).search(USER)
    assert resp.status == ProviderStatus.AUTH_REQUIRED
    assert BRAVE_KEY not in " ".join(resp.errors)


async def test_transient_errors_are_retried(settings, brave_env):
    server = Server(httpx.Response(503), httpx.Response(200, json=brave_payload(*DEFAULT_ITEMS)))
    resp = await brave(settings, server).search(USER)
    assert resp.status == ProviderStatus.SUCCESS
    assert len(resp.metadata["retries"]) == 1 and len(server.requests) == 2


async def test_connection_errors_are_retried_then_fail(settings, brave_env):
    def down(request):
        raise httpx.ConnectError("connection refused", request=request)

    server = Server(down)
    resp = await brave(settings, server).search(USER)
    assert resp.status == ProviderStatus.FAILED
    assert len(server.requests) == settings.resilience.max_retries + 1


async def test_circuit_breaker_opens_after_repeated_failures(settings, brave_env):
    settings.resilience.max_retries = 0
    settings.resilience.breaker_failure_threshold = 2
    server = Server(httpx.Response(500))
    p = brave(settings, server)
    assert (await p.search(USER, "a")).status == ProviderStatus.FAILED
    assert (await p.search(USER, "b")).status == ProviderStatus.FAILED
    third = await p.search(USER, "c")
    assert third.status == ProviderStatus.SKIPPED and "Circuit breaker" in third.errors[0]
    assert len(server.requests) == 2


async def test_cache_avoids_repeated_external_query(settings, brave_env):
    server = Server(httpx.Response(200, json=brave_payload(*DEFAULT_ITEMS)))
    p = brave(settings, server)
    first = await p.search(USER, '"fearless1999"')
    second = await p.search(USER, '"fearless1999"')
    assert len(server.requests) == 1
    assert second.metadata["cache_hit"] is True and len(second.results) == len(first.results)
    await p.search(USER, '"outra consulta"')
    assert len(server.requests) == 2


async def test_failures_are_not_cached(settings, brave_env):
    settings.resilience.max_retries = 0
    server = Server(httpx.Response(500), httpx.Response(200, json=brave_payload(*DEFAULT_ITEMS)))
    p = brave(settings, server)
    assert (await p.search(USER, "q")).status == ProviderStatus.FAILED
    assert (await p.search(USER, "q")).status == ProviderStatus.SUCCESS


async def test_unsupported_operator_is_skipped_not_altered(settings, brave_env):
    server = Server(httpx.Response(200, json=brave_payload()))
    resp = await brave(settings, server).search(USER, 'inurl:admin "fearless1999"')
    assert resp.status == ProviderStatus.SKIPPED and "inurl" in resp.errors[0]
    assert server.requests == []


async def test_timeout(settings, brave_env):
    import asyncio

    settings.providers["search.brave"] = ProviderSettings(timeout_seconds=0.05)

    async def slow_handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=brave_payload())

    async def resolver(host):
        return ["93.184.216.34"]

    rt = ProviderRuntime(transport=httpx.MockTransport(slow_handler), resolver=resolver)
    resp = await BraveSearchProvider(settings, rt).search(USER)
    assert resp.status == ProviderStatus.TIMEOUT


# --- Google CSE -------------------------------------------------------------------


@pytest.fixture
def google_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CSE_API_KEY", "AIza-google-secret-9999")
    monkeypatch.setenv("GOOGLE_CSE_CX", "cx123")


async def test_google_cse_parses_items(settings, google_env):
    server = Server(httpx.Response(200, json={"items": [
        {"link": "https://forum.example/u/fearless1999", "title": "Perfil", "snippet": "fearless1999 no fórum",
         "pagemap": {"metatags": [{"article:published_time": "2023-01-02T00:00:00Z"}]}},
    ]}))
    p = GoogleCSEProvider(settings, runtime_with(server))
    resp = await p.search(USER, 'site:forum.example inurl:u "fearless1999"')
    assert resp.status == ProviderStatus.SUCCESS
    assert server.requests[0].url.params["cx"] == "cx123"
    assert resp.results[0].observed_at.year == 2023


async def test_google_quota_is_rate_limited_and_key_not_leaked(settings, google_env):
    body = {"error": {"code": 403, "errors": [{"reason": "dailyLimitExceeded"}]}}
    server = Server(httpx.Response(403, json=body))
    resp = await GoogleCSEProvider(settings, runtime_with(server)).search(USER)
    assert resp.status == ProviderStatus.RATE_LIMITED
    assert "AIza-google-secret-9999" not in str(resp.model_dump())


async def test_google_invalid_key(settings, google_env):
    body = {"error": {"code": 400, "errors": [{"reason": "keyInvalid"}]}}
    resp = await GoogleCSEProvider(settings, runtime_with(Server(httpx.Response(400, json=body)))).search(USER)
    assert resp.status == ProviderStatus.AUTH_REQUIRED


# --- SearchManager + Orchestrator -------------------------------------------------------


def multi_engine_setup(settings: Settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.search.brave.com":
            return httpx.Response(200, json=brave_payload(*DEFAULT_ITEMS))
        return httpx.Response(200, json={"items": [
            {"link": "http://www.forum.example/u/fearless1999/?utm_source=g", "title": "Perfil",
             "snippet": "fearless1999"},
        ]})

    server = Server(handler)
    rt = runtime_with(server)
    providers = [BraveSearchProvider(settings, rt), GoogleCSEProvider(settings, rt)]
    return server, SourceOrchestrator(settings=settings, providers=providers, runtime=rt)


async def test_orchestrator_runs_planned_queries_and_dedups_across_engines(settings, brave_env, google_env):
    settings.modes[SearchMode.QUICK].max_queries = 3
    server, orch = multi_engine_setup(settings)
    run = await orch.run([USER], mode=SearchMode.QUICK)

    executed = [r for r in run.responses if r.metadata.get("planned_query_id")]
    assert {r.query for r in executed} == {q.query for q in run.planned_queries[:3]}
    assert all(r.metadata["reason"] for r in executed)
    assert any("além do limite" in e for r in run.responses for e in r.errors)

    hits = run.search_hits()
    forum = next(h for h in hits if "forum.example" in h.url)
    assert forum.engines == ["search.brave", "search.google_cse"]
    # mesma página vista por dois mecanismos → uma evidência, com avistamentos duplicados
    page_evs = [e for e in run.evidences if e.entity_type == EntityType.URL and "forum.example" in e.normalized_value]
    assert len(page_evs) == 1 and page_evs[0].duplicate_sightings
    assert len(server.requests) == 6  # 3 consultas × 2 mecanismos (cache evita repetições)
    assert run.budget_spent == 6


async def test_orchestrator_budget_limits_queries(settings, brave_env, google_env):
    settings.query_budget.max_cost_per_search = 2
    server, orch = multi_engine_setup(settings)
    run = await orch.run([USER], mode=SearchMode.DEEP)
    assert len(server.requests) == 2
    assert any("orçamento esgotado" in e for r in run.responses for e in r.errors)


async def test_raw_search_sends_query_unchanged(settings, brave_env, google_env):
    server, orch = multi_engine_setup(settings)
    run = await orch.run_raw('"usuario123" "proton.me"')
    assert {r.url.params["q"] for r in server.requests} == {'"usuario123" "proton.me"'}
    assert run.planned_queries[0].category.value == "raw"
    assert run.mode == SearchMode.RAW


async def test_incompatible_queries_are_reported(settings, brave_env):
    server = Server(httpx.Response(200, json=brave_payload()))
    rt = runtime_with(server)
    orch = SourceOrchestrator(settings=settings, providers=[BraveSearchProvider(settings, rt)], runtime=rt)
    run = await orch.run([normalize("example.com", IdentifierType.DOMAIN)], mode=SearchMode.DEEP_SWEEP)
    assert any("operadores não suportados" in e for r in run.responses for e in r.errors)
    assert not any("inurl:" in r.url.params["q"] for r in server.requests)


def test_search_manager_aggregate_orders_by_engine_agreement():
    from osintizada.core.models import ProviderResponse, ProviderResult

    def resp(engine, url, rank):
        return ProviderResponse(provider=engine, query="q", status=ProviderStatus.SUCCESS, results=[
            ProviderResult(type=EntityType.URL, value=url, source_url=url, raw={"engine": engine, "rank": rank})])

    hits = SearchManager.aggregate([
        resp("a", "https://one.example/", 1),
        resp("a", "https://two.example/", 2),
        resp("b", "https://two.example", 5),
    ])
    assert hits[0].canonical_url == "https://two.example/"
    assert hits[0].engines == ["a", "b"] and hits[0].best_rank == 2


async def test_rate_limit_spacing_uses_runtime_clock(settings, brave_env):
    clock = FakeClock()
    server = Server(httpx.Response(200, json=brave_payload(*DEFAULT_ITEMS)))
    p = BraveSearchProvider(settings, runtime_with(server, clock))
    await p.search(USER, "a")
    second = await p.search(USER, "b")
    assert clock.now == pytest.approx(1.0, abs=0.01)  # 60/min → 1 req/s
    assert second.metadata["rate_limit_wait_s"] == pytest.approx(1.0, abs=0.01)


async def test_rate_limit_can_be_disabled_by_config(settings, brave_env):
    settings.providers["search.brave"] = ProviderSettings(rate_limit_per_minute=0)
    assert BraveSearchProvider(settings).rate_limit_per_minute is None
