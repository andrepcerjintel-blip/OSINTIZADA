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
import logging
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
from osintizada.core.models import (
    Entity,
    NormalizedIdentifier,
    ProviderResponse,
    ProviderResult,
    utcnow,
)
from osintizada.core.secrets import get_secret, mask_secret, sanitize
from osintizada.net.http_client import HTTPResult, SafeHTTPClient, parse_retry_after
from osintizada.net.ssrf import UnsafeURLError
from osintizada.resilience import (
    ProviderRuntime,
    TransientError,
    backoff_delay,
    cache_key,
    retry_async,
)

# --- Erros tipados -----------------------------------------------------------


class ProviderError(Exception):
    """Falha do provider (→ FAILED). ``code`` é um identificador estável (ex.: HTTP_404)."""

    default_code = "PROVIDER_ERROR"

    def __init__(self, message: str = "", code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.default_code


class RateLimitedError(ProviderError):
    """Serviço limitou as requisições (→ RATE_LIMITED)."""

    default_code = "RATE_LIMITED"

    def __init__(self, message: str = "Rate limited", retry_after: float | None = None,
                 code: str | None = None) -> None:
        super().__init__(message, code)
        self.retry_after = retry_after


class AuthRequiredError(ProviderError):
    """Credencial ausente, inválida ou expirada (→ AUTH_REQUIRED)."""

    default_code = "AUTH_REQUIRED"


class SkippedError(ProviderError):
    """O provider decidiu não executar esta consulta (→ SKIPPED), ex.: operador não suportado."""

    default_code = "SKIPPED"


class ProviderNotConfigured(ProviderError):
    """Dependência ou credencial ausente detectada durante a execução (→ NOT_CONFIGURED)."""

    default_code = "NOT_CONFIGURED"


class ProviderTimeout(ProviderError, TransientError):
    """Tempo esgotado no serviço (re-tentável; esgotado → TIMEOUT)."""

    default_code = "TIMEOUT"


class ProviderUnavailable(ProviderError, TransientError):
    """Serviço temporariamente indisponível: 502/503/504, conexão (re-tentável; esgotado → FAILED)."""

    default_code = "UNAVAILABLE"


# Nomes do contrato padronizado.
ProviderRateLimited = RateLimitedError
ProviderAuthenticationError = AuthRequiredError

RETRYABLE_HTTP = frozenset({502, 503, 504})
_log = logging.getLogger("osintizada.providers")
_FAILURE_STATUSES = frozenset({ProviderStatus.FAILED, ProviderStatus.TIMEOUT, ProviderStatus.RATE_LIMITED,
                               ProviderStatus.AUTH_REQUIRED})


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
    default_requests_per_second: ClassVar[float | None] = None
    default_concurrency: ClassVar[int | None] = None
    default_max_results: ClassVar[int | None] = None
    default_cache_ttl_seconds: ClassVar[int | None] = None
    # Limite de tamanho de resposta específico (ex.: crt.sh devolve JSON grande).
    max_response_bytes: ClassVar[int | None] = None
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
        """Limite efetivo aplicado ANTES da requisição; ``0`` na configuração desativa o padrão."""
        cfg = self.config
        if cfg.rate_limit_per_minute is not None:
            return cfg.rate_limit_per_minute or None
        if cfg.requests_per_second is not None:
            return cfg.requests_per_second * 60 or None
        if self.default_rate_limit_per_minute is not None:
            return self.default_rate_limit_per_minute
        return self.default_requests_per_second * 60 if self.default_requests_per_second else None

    @property
    def max_concurrency(self) -> int | None:
        return self.config.max_concurrency or self.default_concurrency

    @property
    def max_results(self) -> int:
        return self.config.max_results or self.default_max_results or self.settings.search.max_results_per_provider

    def not_configured_reason(self) -> str:
        return "Credenciais ausentes: " + ", ".join(v for v in self.required_secrets if not get_secret(v))

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
        return {"max_results": self.max_results}

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
            return self._finish(response, ProviderStatus.SKIPPED, f"Tipo {identifier.type.value} não suportado",
                                "UNSUPPORTED_TYPE")
        if not self.enabled:
            return self._finish(response, ProviderStatus.SKIPPED, "Provider desabilitado na configuração", "DISABLED")
        if not self.is_configured():
            return self._finish(response, ProviderStatus.NOT_CONFIGURED, self.not_configured_reason(),
                                "NOT_CONFIGURED")

        rt = self.runtime
        key = cache_key(self.name, self.parser_version, identifier.type.value, identifier.value, query,
                        self.cache_params())
        if self.cache_ttl > 0 and (cached := rt.cache.get(key)) is not None:
            rt.count("cache_hits")
            return self._from_cache(response, cached)

        if (cooldown := rt.rate_limiter.cooldown_remaining(self.name)) > 0:
            response.metadata["retry_after"] = round(cooldown, 1)
            return self._finish(response, ProviderStatus.RATE_LIMITED,
                                f"Em pausa por limite do serviço; nova tentativa em {cooldown:.0f}s",
                                "RATE_LIMIT_COOLDOWN")

        breaker = self._breaker()
        if not breaker.allow():
            return self._finish(
                response, ProviderStatus.SKIPPED,
                f"Circuit breaker aberto após {breaker.consecutive_failures} falhas consecutivas; "
                f"nova tentativa em {breaker.seconds_until_retry():.0f}s", "CIRCUIT_OPEN",
            )

        semaphore = rt.semaphore(self.name, self.max_concurrency)
        rt.count("provider_calls")
        started = time.perf_counter()
        try:
            async with semaphore or contextlib.nullcontext():
                results = await asyncio.wait_for(self._run_with_retry(identifier, query, response),
                                                 timeout=self.timeout)
        except (TimeoutError, ProviderTimeout) as exc:
            breaker.record_failure()
            msg = str(exc) if isinstance(exc, ProviderTimeout) else f"Tempo limite de {self.timeout}s excedido"
            return self._finish(response, ProviderStatus.TIMEOUT, msg, "TIMEOUT")
        except RateLimitedError as exc:
            wait = exc.retry_after if exc.retry_after is not None else self.settings.resilience.default_cooldown_seconds
            rt.rate_limiter.set_cooldown(self.name, wait)
            rt.count("rate_limited")
            response.metadata["retry_after"] = wait
            return self._finish(response, ProviderStatus.RATE_LIMITED, str(exc), exc.code)
        except AuthRequiredError as exc:
            return self._finish(response, ProviderStatus.AUTH_REQUIRED, str(exc), exc.code)
        except ProviderNotConfigured as exc:
            return self._finish(response, ProviderStatus.NOT_CONFIGURED, str(exc), exc.code)
        except SkippedError as exc:
            return self._finish(response, ProviderStatus.SKIPPED, str(exc), exc.code)
        except UnsafeURLError as exc:
            return self._finish(response, ProviderStatus.FAILED, f"URL recusada pela política SSRF: {exc}",
                                "SSRF_BLOCKED")
        except asyncio.CancelledError:
            self._finish(response, ProviderStatus.CANCELLED, "Execução cancelada", "CANCELLED")
            raise
        except Exception as exc:  # noqa: BLE001 - falha isolada por provider
            breaker.record_failure()
            rt.count("provider_failures")
            code = exc.code if isinstance(exc, ProviderError) else type(exc).__name__.upper()
            return self._finish(response, ProviderStatus.FAILED, f"{type(exc).__name__}: {exc}", code)

        rt.last_latency_ms[self.name] = round((time.perf_counter() - started) * 1000, 1)
        breaker.record_success()
        response.results = list(results)[: self.max_results]
        status = ProviderStatus.SUCCESS if response.results else ProviderStatus.NO_RESULTS
        self._finish(response, status)
        if self.cache_ttl > 0:
            rt.cache.set(key, response.model_copy(deep=True), self.cache_ttl)
        rt.count("results", len(response.results))
        return response

    async def enrich(self, entity: Entity) -> ProviderResponse:
        response = ProviderResponse(provider=self.name, query=entity.value, status=ProviderStatus.SKIPPED)
        return self._finish(response, ProviderStatus.SKIPPED, "Enriquecimento não implementado por este provider",
                            "NOT_IMPLEMENTED")

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
            max_bytes=self.max_response_bytes or self.settings.resilience.max_response_bytes,
            trusted_hosts=self.trusted_hosts,
            resolver=self.runtime.resolver,
            transport=self.runtime.transport,
        )

    async def fetch(self, client: SafeHTTPClient, method: str, url: str, *,
                    auth_statuses: tuple[int, ...] = (401,), not_found_ok: bool = False,
                    **kwargs: Any) -> HTTPResult | None:
        """Requisição com mapeamento padronizado de erros HTTP.

        ``not_found_ok``: 404 significa "não existe" (resposta válida) e retorna ``None``.
        """
        try:
            result = await client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"{type(exc).__name__}: {exc}") from exc
        except httpx.TransportError as exc:  # conexão, DNS, proxy
            raise ProviderUnavailable(f"{type(exc).__name__}: {exc}", code="CONNECTION_ERROR") from exc
        if not_found_ok and result.status_code == 404:
            return None
        self.raise_for_status(result, auth_statuses)
        return result

    @staticmethod
    def raise_for_status(result: HTTPResult, auth_statuses: tuple[int, ...] = (401,)) -> None:
        """429 → RATE_LIMITED; 408 → timeout; 502/503/504 → re-tentável; demais 4xx/5xx → sem retry."""
        code = result.status_code
        if code < 400:
            return
        if code == 429:
            raise RateLimitedError("HTTP 429 Too Many Requests",
                                   retry_after=parse_retry_after(result.headers.get("retry-after")),
                                   code="HTTP_429")
        if code in auth_statuses:
            raise AuthRequiredError(f"HTTP {code}: credencial recusada pelo serviço", code=f"HTTP_{code}")
        if code == 408:
            raise ProviderTimeout("HTTP 408 Request Timeout", code="HTTP_408")
        if code in RETRYABLE_HTTP:
            raise ProviderUnavailable(f"HTTP {code}", code=f"HTTP_{code}")
        raise ProviderError(f"HTTP {code}", code=f"HTTP_{code}")

    # --- helpers -------------------------------------------------------------------

    async def _run_with_retry(self, identifier: NormalizedIdentifier, query: str | None,
                              response: ProviderResponse) -> list[ProviderResult]:
        """Rate limit antes de cada tentativa; retry com backoff apenas para falhas recuperáveis.

        * transitórias (timeout, 502/503/504, rede): até ``max_retries``;
        * 429: até ``rate_limit_retries``, respeitando Retry-After (ou backoff) desde que a
          espera não ultrapasse ``max_rate_limit_wait_seconds``; caso contrário → RATE_LIMITED.
        * 400/401/403/404 e demais: nunca re-tentados.
        """
        res = self.settings.resilience
        rate_limit_attempts = 0

        def note_retry(n: int, exc: Exception, delay: float) -> None:
            self.runtime.count("retries")
            response.metadata.setdefault("retries", []).append(
                {"attempt": n, "error": sanitize(str(exc)), "delay_s": round(delay, 2)})

        async def attempt() -> list[ProviderResult]:
            nonlocal rate_limit_attempts
            while True:
                waited = await self.runtime.rate_limiter.acquire(self.name)
                if waited:
                    response.metadata["rate_limit_wait_s"] = round(
                        response.metadata.get("rate_limit_wait_s", 0) + waited, 3)
                try:
                    return await self._search(identifier, query)
                except RateLimitedError as exc:
                    delay = exc.retry_after if exc.retry_after is not None else backoff_delay(
                        rate_limit_attempts + 1, res.backoff_base_seconds, res.backoff_max_seconds)
                    if rate_limit_attempts >= res.rate_limit_retries or delay > res.max_rate_limit_wait_seconds:
                        raise
                    rate_limit_attempts += 1
                    note_retry(rate_limit_attempts, exc, delay)
                    await self.runtime.sleep(delay)

        return await retry_async(attempt, max_retries=res.max_retries, base=res.backoff_base_seconds,
                                 maximum=res.backoff_max_seconds, on_retry=note_retry, sleep=self.runtime.sleep)

    def _from_cache(self, response: ProviderResponse, cached: ProviderResponse) -> ProviderResponse:
        response.status = cached.status
        response.results = [r.model_copy(deep=True) for r in cached.results]
        response.metadata = {**cached.metadata, "cache_hit": True,
                             "cached_finished_at": cached.finished_at.isoformat() if cached.finished_at else None}
        response.finished_at = utcnow()
        return response

    def _finish(self, response: ProviderResponse, status: ProviderStatus, error: str | None = None,
                code: str | None = None) -> ProviderResponse:
        response.status = status
        response.finished_at = utcnow()
        _log.info("provider search finished", extra={
            "provider": self.name, "operation": "search", "status": status.value, "error_code": code,
            "duration_ms": round(response.duration_ms or 0, 1), "results": len(response.results),
            "cache_hit": bool(response.metadata.get("cache_hit"))})
        if code:
            response.error_code = code
        if error:
            response.errors.append(sanitize(error))
        if status in _FAILURE_STATUSES:
            self.runtime.last_errors[self.name] = {
                "status": status.value, "code": code, "message": sanitize(error or ""),
                "at": response.finished_at.isoformat()}
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
