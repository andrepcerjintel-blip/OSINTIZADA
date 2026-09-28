import asyncio

import pytest

from osintizada.config import ProviderSettings
from osintizada.core.enums import EntityOrigin, EntityType, IdentifierType, ProviderStatus, SearchMode, SourceTier
from osintizada.core.models import ProviderResult
from osintizada.core.normalization import normalize
from osintizada.orchestration.source_orchestrator import SourceOrchestrator
from osintizada.providers.base import (
    AuthRequiredError,
    HTTPProvider,
    LocalProvider,
    ProviderRegistry,
    RateLimitedError,
    TorProvider,
    load_builtin_providers,
)


class FakeWeb(HTTPProvider):
    """Provider de teste (mock) – nunca registrado no registry global."""

    name = "test.fake_web"
    supported_identifiers = frozenset({IdentifierType.USERNAME})
    behaviour = "ok"

    async def _search(self, identifier, query):
        if self.behaviour == "empty":
            return []
        if self.behaviour == "boom":
            raise RuntimeError("HTTP 403 api_key=SHOULDNOTLEAK")
        if self.behaviour == "429":
            raise RateLimitedError("HTTP 429", retry_after=60)
        if self.behaviour == "auth":
            raise AuthRequiredError("sessão expirada")
        if self.behaviour == "slow":
            await asyncio.sleep(5)
        return [ProviderResult(type=EntityType.URL, value="https://forum.example/u/x",
                               source_url="https://forum.example/u/x")]


class NeedsKey(HTTPProvider):
    name = "test.needs_key"
    supported_identifiers = frozenset({IdentifierType.USERNAME})
    required_secrets = ("OSINTIZADA_TEST_KEY_THAT_DOES_NOT_EXIST",)

    async def _search(self, identifier, query):  # pragma: no cover - não deve rodar
        raise AssertionError("não deveria executar sem credencial")


class FakeTor(TorProvider):
    name = "test.tor"
    supported_identifiers = frozenset({IdentifierType.USERNAME})

    async def _search(self, identifier, query):  # pragma: no cover
        return []


def make(cls, settings, behaviour="ok"):
    p = cls(settings)
    p.behaviour = behaviour
    return p


USER = normalize("fearless1999", IdentifierType.USERNAME)


@pytest.mark.parametrize(
    "behaviour,status",
    [
        ("ok", ProviderStatus.SUCCESS),
        ("empty", ProviderStatus.NO_RESULTS),
        ("boom", ProviderStatus.FAILED),
        ("429", ProviderStatus.RATE_LIMITED),
        ("auth", ProviderStatus.AUTH_REQUIRED),
    ],
)
async def test_status_mapping(settings, behaviour, status):
    resp = await make(FakeWeb, settings, behaviour).search(USER)
    assert resp.status == status
    assert resp.finished_at is not None


async def test_errors_are_sanitized(settings):
    resp = await make(FakeWeb, settings, "boom").search(USER)
    assert "SHOULDNOTLEAK" not in " ".join(resp.errors)


async def test_rate_limit_keeps_retry_after(settings):
    resp = await make(FakeWeb, settings, "429").search(USER)
    assert resp.metadata["retry_after"] == 60


async def test_timeout(settings):
    settings.providers["test.fake_web"] = ProviderSettings(timeout_seconds=0.05)
    resp = await make(FakeWeb, settings, "slow").search(USER)
    assert resp.status == ProviderStatus.TIMEOUT


async def test_unsupported_type_is_skipped(settings):
    resp = await make(FakeWeb, settings).search(normalize("a@b.com", IdentifierType.EMAIL))
    assert resp.status == ProviderStatus.SKIPPED


async def test_missing_credentials_not_configured(settings):
    p = NeedsKey(settings)
    assert not p.is_configured()
    assert (await p.search(USER)).status == ProviderStatus.NOT_CONFIGURED
    assert p.masked_credentials() == {"OSINTIZADA_TEST_KEY_THAT_DOES_NOT_EXIST": "NOT CONFIGURED"}
    assert (await p.healthcheck()).status == "not_configured"


def test_tor_disabled_by_default(settings):
    assert FakeTor(settings).enabled is False
    settings.tor.mode = "TOR_PASSIVE"
    assert FakeTor(settings).enabled is True


def test_registry_rejects_duplicates_and_filters_by_type():
    reg = ProviderRegistry()
    reg.register(FakeWeb)
    reg.register(FakeWeb)  # idempotente para a mesma classe

    class Other(FakeWeb):
        name = "test.fake_web"

    with pytest.raises(ValueError):
        reg.register(Other)
    assert reg.classes_for(IdentifierType.USERNAME) == [FakeWeb]
    assert reg.classes_for(IdentifierType.EMAIL) == []


def test_builtin_providers_load():
    reg = load_builtin_providers()
    assert "local.identifier_analysis" in reg


async def test_orchestrator_isolates_failures(settings):
    ok = make(FakeWeb, settings)

    class Broken(FakeWeb):
        name = "test.broken"

    broken = make(Broken, settings, "boom")
    orch = SourceOrchestrator(settings=settings, providers=[ok, broken, NeedsKey(settings)])
    run = await orch.run([USER], mode=SearchMode.DEEP)
    by = {r.provider: r.status for r in run.responses}
    assert by == {"test.fake_web": ProviderStatus.SUCCESS, "test.broken": ProviderStatus.FAILED,
                  "test.needs_key": ProviderStatus.NOT_CONFIGURED}
    assert len(run.evidences) == 1
    assert run.seeds[0].origin == EntityOrigin.SEED
    assert run.entities[0].origin == EntityOrigin.DISCOVERED
    assert {e.provider for e in run.search_log} == set(by)


async def test_orchestrator_respects_block_and_tier(settings):
    class Scraper(FakeWeb):
        name = "test.scraper"
        tier = SourceTier.TIER_4

    orch = SourceOrchestrator(settings=settings, providers=[make(FakeWeb, settings), make(Scraper, settings)])
    run = await orch.run([USER], mode=SearchMode.QUICK, blocked={"test.fake_web"})
    by = {s.provider: s for s in run.selections}
    assert not by["test.fake_web"].selected and "Bloqueado" in by["test.fake_web"].reason
    assert not by["test.scraper"].selected and "Tier" in by["test.scraper"].reason
    assert all(r.status == ProviderStatus.SKIPPED for r in run.responses)


async def test_orchestrator_cancel(settings):
    orch = SourceOrchestrator(settings=settings, providers=[make(FakeWeb, settings)])
    orch.cancel()
    run = await orch.run([USER])
    assert run.cancelled
    assert run.responses[0].status == ProviderStatus.CANCELLED


async def test_budget_limits_execution(settings):
    settings.query_budget.max_cost_per_search = 0
    orch = SourceOrchestrator(settings=settings, providers=[make(FakeWeb, settings)])
    run = await orch.run([USER])
    assert run.responses[0].status == ProviderStatus.SKIPPED
    assert "Orçamento" in run.responses[0].errors[0]


async def test_local_provider_derivations(settings):
    orch = SourceOrchestrator(settings=settings)
    email = normalize("fearless1999@gmail.com", IdentifierType.EMAIL)
    url = normalize("https://www.github.com/torvalds/linux", IdentifierType.URL)
    run = await orch.run([email, url], mode=SearchMode.QUICK)
    values = {(e.type, e.value) for e in run.entities}
    assert (EntityType.DOMAIN, "gmail.com") in values
    assert (EntityType.USERNAME, "fearless1999") in values
    assert (EntityType.SOCIAL_ACCOUNT, "github:torvalds") in values
    assert (EntityType.DOMAIN, "github.com") in values
    assert all(e.origin == EntityOrigin.DERIVED for e in run.entities)
    local_part = next(ev for ev in run.evidences if ev.normalized_value == "fearless1999")
    assert local_part.confidence < 0.5  # hipótese, não confirmação


def test_local_provider_is_local_and_free(settings):
    from osintizada.providers.local.identifier_analysis import IdentifierAnalysisProvider

    p = IdentifierAnalysisProvider(settings)
    assert isinstance(p, LocalProvider)
    assert p.cost == 0
