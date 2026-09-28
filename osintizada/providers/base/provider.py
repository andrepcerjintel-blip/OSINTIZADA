"""Interface base de providers.

Um provider concreto implementa apenas ``_search`` (e opcionalmente
``_healthcheck``). O método público ``search`` aplica, de forma uniforme para
todas as fontes, na ordem:

  1. suporte ao tipo de identificador / habilitação   → SKIPPED
  2. credenciais                                        → NOT_CONFIGURED
  3. cache (provider + parser + consulta + parâmetros)  → resposta cacheada
  4. cooldown pedido pelo serviço (429 anterior)        → RATE_LIMITED (sem chamar)
  5. circuit breaker aberto                             → SKIPPED
  6. concorrência por provider + rate limit (token bucket)
  7. retry com backoff exponencial para falhas transitórias
  8. timeout global da chamada                          → TIMEOUT
  9. erros tipados → RATE_LIMITED / AUTH_REQUIRED / FAILED; vazio → NO_RESULTS

Assim nenhuma lógica específica de site vaza para o Core e uma falha nunca
interrompe a investigação inteira.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from abc import ABC, abstractmethod
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, Field

from osintizada.config import ProviderSettings, Settings, get_settings
from osintizada.core.enums import (
    DataClassification,
    IdentifierType,
    ProviderStatus,
    ProviderType,
    SourceAccess,
    SourceTier,
)
from osintizada.core.models import Entity, NormalizedIdentifier, ProviderResponse, ProviderResult, utcnow
from osintizada.core.secrets import get_secret, mask_secret, sanitize
from osintizada.net.http_client import HTTPResult, SafeHTTPClient, parse_retry_after
from osintizada.net.ssrf import UnsafeURLError
from osintizada.resilience import ProviderRuntime, TransientError, cache_key, retry_async

# --- Erros tipados -----------------------------------------------------------


class ProviderError(Exception):
    """Falha genérica do provider (→ FAILED)."""


class RateLimitedError(ProviderError):
    def __init__(self, message: str = "Rate limited", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AuthRequiredError(ProviderError):
    """Credencial ausente, inválida ou expirada (→ AUTH_REQUIRED)."""


class SkippedError(ProviderError):
    """O provider decidiu não executar esta consulta (→ SKIPPED), ex.: operador não suportado."""


# --- Healthcheck --------------------------------------------------------------


class HealthStatus(BaseModel):
    provider: str
    status: str  # healthy | degraded | unhealthy | not_configured | unknown
    configured: bool
    latency_ms: float | None = None
    quota_remaining: int | None = None
    detail: str | None = None
    circuit: str | None = None
    credentials: dict[str, str] = Field(default_factory=dict)


# --- Base ---------------------------------------------------------------------


class BaseProvider(ABC):
    name: ClassVar[str]
    display_name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    provider_type: ClassVar[ProviderType]
    supported_identifiers: ClassVar[frozenset[IdentifierType]] = frozenset()
    requires_auth: ClassVar[bool] = False
    required_secrets: ClassVar[tuple[str, ...]] = ()  # nomes de variáveis de ambiente
    access: ClassVar[SourceAccess] = SourceAccess.PUBLIC_SOURCE
    classification: ClassVar[DataClassification] = DataClassification.PUBLIC
    tier: ClassVar[SourceTier] = SourceTier.TIER_3
    source_tag: ClassVar[str] = "WEB"  # tag exibida na interface: [TELEGRAM], [GITHUB]...
    default_timeout_seconds: ClassVar[float] = 30.0
    # Versão do parser: mudar invalida o cache deste provider.
    parser_version: ClassVar[str] = "1"
    # Padrões por provider (sobrescritos por ``providers.<nome>`` na configuração).
    default_rate_limit_per_minute: ClassVar[float | None] = None
    default_cache_ttl_seconds: ClassVar[int | None] = None
    # Hosts fixos de API declarados no código: dispensam resolução DNS no SSRF check.
    trusted_hosts: ClassVar[tuple[str, ...]] = ()
    # Providers que executam consultas textuais planejadas (search engines).
    consumes_planned_queries: ClassVar[bool] = False
    # Categoria de custo no query budget (padrão: provider_type).
    cost_category: ClassVar[str | None] = None

    def __init__(self, settings: Settings | None = None, runtime: ProviderRuntime | None = None) -> None:
        self.settings = settings or get_settings()
        self.runtime = runtime or ProviderRuntime()
        self.runtime.rate_limiter.configure(self.name, self.rate_limit_per_minute)

    # --- metadados / configuração -----------------------------------------------

    @property
    def config(self) -> ProviderSettings:
        return self.settings.provider(self.name)

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def effective_tier(self) -> int:
        return self.config.tier or int(self.tier)

    @property
    def cost(self) -> int:
        return self.settings.query_budget.costs.get(self.cost_category or self.provider_type.value, 1)

    @property
    def timeout(self) -> float:
        return self.config.timeout_seconds or self.default_timeout_seconds

    @property
    def rate_limit_per_minute(self) -> float | None:
        """Limite efetivo; ``0`` na configuração desabilita o limite padrão do provider."""
        if self.config.rate_limit_per_minute is not None:
            return self.config.rate_limit_per_minute or None
        return self.default_rate_limit_per_minute

    @property
    def cache_ttl(self) -> int:
        for value in (self.config.cache_ttl_seconds, self.default_cache_ttl_seconds):
            if value is not None:
                return value
        return self.settings.resilience.default_cache_ttl_seconds

    def secret(self, env_var: str) -> str | None:
        return get_secret(env_var)

    def is_configured(self) -> bool:
        return all(get_secret(var) for var in self.required_secrets)

    def supports(self, identifier_type: IdentifierType) -> bool:
        return identifier_type in self.supported_identifiers

    def masked_credentials(self) -> dict[str, str]:
        return {var: mask_secret(get_secret(var)) for var in self.required_secrets}

    def cache_params(self) -> dict[str, Any]:
        """Parâmetros que alteram o resultado e portanto entram na chave de cache."""
        return {"max_results": self.settings.search.max_results_per_provider}

    def _breaker(self):
        res = self.settings.resilience
        return self.runtime.breaker(self.name, res.breaker_failure_threshold, res.breaker_recovery_seconds)

    # --- API pública -------------------------------------------------------------

    async def search(self, identifier: NormalizedIdentifier, query: str | None = None) -> ProviderResponse:
        response = ProviderResponse(
            provider=self.name, query=query or identifier.value, identifier_type=identifier.type,
            status=ProviderStatus.SKIPPED,
        )
        if not self.supports(identifier.type):
            return self._finish(response, ProviderStatus.SKIPPED, f"Tipo {identifier.type.value} não suportado")
        if not self.enabled:
            return self._finish(response, ProviderStatus.SKIPPED, "Provider desabilitado na configuração")
        if not self.is_configured():
            return self._finish(response, ProviderStatus.NOT_CONFIGURED,
                                "Credenciais ausentes: " + ", ".join(self.required_secrets))

        rt = self.runtime
        key = cache_key(self.name, self.parser_version, identifier.type.value, identifier.value, query,
                        self.cache_params())
        if self.cache_ttl > 0 and (cached := rt.cache.get(key)) is not None:
            rt.count("cache_hits")
            return self._from_cache(response, cached)

        if (cooldown := rt.rate_limiter.cooldown_remaining(self.name)) > 0:
            response.metadata["retry_after"] = round(cooldown, 1)
            return self._finish(response, ProviderStatus.RATE_LIMITED,
                                f"Em pausa por limite do serviço; nova tentativa em {cooldown:.0f}s")

        breaker = self._breaker()
        if not breaker.allow():
            return self._finish(
                response, ProviderStatus.SKIPPED,
                f"Circuit breaker aberto após {breaker.consecutive_failures} falhas consecutivas; "
                f"nova tentativa em {breaker.seconds_until_retry():.0f}s",
            )

        semaphore = rt.semaphore(self.name, self.config.max_concurrency)
        rt.count("provider_calls")
        try:
            async with semaphore or contextlib.nullcontext():
                results = await asyncio.wait_for(self._run_with_retry(identifier, query, response),
                                                 timeout=self.timeout)
        except TimeoutError:
            breaker.record_failure()
            return self._finish(response, ProviderStatus.TIMEOUT, f"Tempo limite de {self.timeout}s excedido")
        except RateLimitedError as exc:
            wait = exc.retry_after if exc.retry_after is not None else self.settings.resilience.default_cooldown_seconds
            rt.rate_limiter.set_cooldown(self.name, wait)
            rt.count("rate_limited")
            response.metadata["retry_after"] = wait
            return self._finish(response, ProviderStatus.RATE_LIMITED, str(exc))
        except AuthRequiredError as exc:
            return self._finish(response, ProviderStatus.AUTH_REQUIRED, str(exc))
        except SkippedError as exc:
            return self._finish(response, ProviderStatus.SKIPPED, str(exc))
        except UnsafeURLError as exc:
            return self._finish(response, ProviderStatus.FAILED, f"URL recusada pela política SSRF: {exc}")
        except asyncio.CancelledError:
            self._finish(response, ProviderStatus.CANCELLED, "Execução cancelada")
            raise
        except Exception as exc:  # noqa: BLE001 - falha isolada por provider
            breaker.record_failure()
            rt.count("provider_failures")
            return self._finish(response, ProviderStatus.FAILED, f"{type(exc).__name__}: {exc}")

        breaker.record_success()
        response.results = list(results)[: self.settings.search.max_results_per_provider]
        status = ProviderStatus.SUCCESS if response.results else ProviderStatus.NO_RESULTS
        self._finish(response, status)
        if self.cache_ttl > 0:
            rt.cache.set(key, response.model_copy(deep=True), self.cache_ttl)
        rt.count("results", len(response.results))
        return response

    async def enrich(self, entity: Entity) -> ProviderResponse:
        response = ProviderResponse(provider=self.name, query=entity.value, status=ProviderStatus.SKIPPED)
        return self._finish(response, ProviderStatus.SKIPPED, "Enriquecimento não implementado por este provider")

    async def healthcheck(self) -> HealthStatus:
        configured = self.is_configured()
        base = HealthStatus(provider=self.name, status="unknown", configured=configured,
                            credentials=self.masked_credentials(), circuit=self._breaker().state.value)
        if not configured:
            base.status = "not_configured"
            return base
        start = time.perf_counter()
        try:
            detail = await asyncio.wait_for(self._healthcheck(), timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            base.status = "unhealthy"
            base.detail = sanitize(f"{type(exc).__name__}: {exc}")
            return base
        base.latency_ms = round((time.perf_counter() - start) * 1000, 1)
        base.status = "healthy" if detail is not False else "degraded"
        if isinstance(detail, str):
            base.detail = detail
        return base

    # --- a implementar -------------------------------------------------------------

    @abstractmethod
    async def _search(self, identifier: NormalizedIdentifier, query: str | None) -> list[ProviderResult]:
        """Executa a coleta e retorna resultados padronizados.

        Deve levantar ``RateLimitedError``/``AuthRequiredError``/``ProviderError``
        (ou ``TransientError`` para falhas temporárias re-tentáveis) em vez de
        retornar lista vazia quando a coleta NÃO foi concluída.
        """

    async def _healthcheck(self) -> bool | str | None:
        """Checagem leve de disponibilidade. Padrão: nada a verificar."""
        return None

    # --- HTTP ----------------------------------------------------------------------

    def http_client(self) -> SafeHTTPClient:
        return SafeHTTPClient(
            timeout=self.timeout,
            user_agent=self.settings.network.user_agent,
            max_bytes=self.settings.resilience.max_response_bytes,
            trusted_hosts=self.trusted_hosts,
            resolver=self.runtime.resolver,
            transport=self.runtime.transport,
        )

    async def fetch(self, client: SafeHTTPClient, method: str, url: str, *,
                    auth_statuses: tuple[int, ...] = (401,), **kwargs: Any) -> HTTPResult:
        """Requisição com mapeamento padronizado de erros HTTP."""
        try:
            result = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:  # conexão, DNS, timeout de leitura
            raise TransientError(f"{type(exc).__name__}: {exc}") from exc
        self.raise_for_status(result, auth_statuses)
        return result

    @staticmethod
    def raise_for_status(result: HTTPResult, auth_statuses: tuple[int, ...] = (401,)) -> None:
        code = result.status_code
        if code == 429:
            raise RateLimitedError("HTTP 429 Too Many Requests",
                                   retry_after=parse_retry_after(result.headers.get("retry-after")))
        if code in auth_statuses:
            raise AuthRequiredError(f"HTTP {code}: credencial recusada pelo serviço")
        if code >= 500:
            raise TransientError(f"HTTP {code}")
        if code >= 400:
            raise ProviderError(f"HTTP {code}")

    # --- helpers -------------------------------------------------------------------

    async def _run_with_retry(self, identifier: NormalizedIdentifier, query: str | None,
                              response: ProviderResponse) -> list[ProviderResult]:
        res = self.settings.resilience

        async def attempt() -> list[ProviderResult]:
            waited = await self.runtime.rate_limiter.acquire(self.name)
            if waited:
                response.metadata["rate_limit_wait_s"] = round(
                    response.metadata.get("rate_limit_wait_s", 0) + waited, 3)
            return await self._search(identifier, query)

        def on_retry(n: int, exc: Exception, delay: float) -> None:
            self.runtime.count("retries")
            response.metadata.setdefault("retries", []).append(
                {"attempt": n, "error": sanitize(str(exc)), "delay_s": round(delay, 2)})

        return await retry_async(attempt, max_retries=res.max_retries, base=res.backoff_base_seconds,
                                 maximum=res.backoff_max_seconds, on_retry=on_retry, sleep=self.runtime.sleep)

    def _from_cache(self, response: ProviderResponse, cached: ProviderResponse) -> ProviderResponse:
        response.status = cached.status
        response.results = [r.model_copy(deep=True) for r in cached.results]
        response.metadata = {**cached.metadata, "cache_hit": True,
                             "cached_finished_at": cached.finished_at.isoformat() if cached.finished_at else None}
        response.finished_at = utcnow()
        return response

    @staticmethod
    def _finish(response: ProviderResponse, status: ProviderStatus, error: str | None = None) -> ProviderResponse:
        response.status = status
        response.finished_at = utcnow()
        if error:
            response.errors.append(sanitize(error))
        return response


# --- Especializações por tipo de acesso ---------------------------------------------


class LocalProvider(BaseProvider, ABC):
    """Processamento local, sem rede. Custo zero e sem cache (é determinístico e barato)."""

    provider_type = ProviderType.LOCAL
    tier = SourceTier.TIER_1
    source_tag = "LOCAL"
    classification = DataClassification.DERIVED
    default_cache_ttl_seconds = 0


class APIProvider(BaseProvider, ABC):
    provider_type = ProviderType.API
    tier = SourceTier.TIER_1


class HTTPProvider(BaseProvider, ABC):
    provider_type = ProviderType.HTTP
    tier = SourceTier.TIER_3


class BrowserProvider(BaseProvider, ABC):
    provider_type = ProviderType.BROWSER
    tier = SourceTier.TIER_4


class TorProvider(BaseProvider, ABC):
    """Executado somente por worker dedicado e somente se Tor estiver habilitado."""

    provider_type = ProviderType.TOR
    tier = SourceTier.TIER_5
    source_tag = "TOR"

    @property
    def enabled(self) -> bool:
        return self.config.enabled and self.settings.tor.mode != "TOR_DISABLED"


class PaidProvider(BaseProvider, ABC):
    provider_type = ProviderType.PAID
    tier = SourceTier.TIER_2
    requires_auth = True
    access = SourceAccess.PAID_SOURCE
    classification = DataClassification.PAID
