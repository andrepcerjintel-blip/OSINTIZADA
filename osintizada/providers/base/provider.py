"""Interface base de providers.

Um provider concreto implementa apenas ``_search`` (e opcionalmente
``_enrich``/``_healthcheck``). O método público ``search`` aplica, de forma
uniforme para todas as fontes:

  * verificação de suporte ao tipo de identificador (→ SKIPPED);
  * verificação de configuração/credenciais (→ NOT_CONFIGURED);
  * timeout (→ TIMEOUT);
  * mapeamento de erros tipados (→ RATE_LIMITED / AUTH_REQUIRED / FAILED);
  * distinção entre SUCCESS e NO_RESULTS;
  * sanitização de mensagens de erro (sem secrets).

Assim, nenhuma lógica específica de site vaza para o Core e uma falha nunca
interrompe a investigação inteira.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import ClassVar

from pydantic import BaseModel

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

# --- Erros tipados -----------------------------------------------------------


class ProviderError(Exception):
    """Falha genérica do provider (→ FAILED)."""


class RateLimitedError(ProviderError):
    def __init__(self, message: str = "Rate limited", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AuthRequiredError(ProviderError):
    """Credencial ausente, inválida ou expirada (→ AUTH_REQUIRED)."""


# --- Healthcheck --------------------------------------------------------------


class HealthStatus(BaseModel):
    provider: str
    status: str  # healthy | degraded | unhealthy | not_configured | unknown
    configured: bool
    latency_ms: float | None = None
    quota_remaining: int | None = None
    detail: str | None = None
    credentials: dict[str, str] = {}


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

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

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
        return self.settings.query_budget.costs.get(self.provider_type.value, 1)

    @property
    def timeout(self) -> float:
        return self.config.timeout_seconds or self.default_timeout_seconds

    def secret(self, env_var: str) -> str | None:
        return get_secret(env_var)

    def is_configured(self) -> bool:
        return all(get_secret(var) for var in self.required_secrets)

    def supports(self, identifier_type: IdentifierType) -> bool:
        return identifier_type in self.supported_identifiers

    def masked_credentials(self) -> dict[str, str]:
        return {var: mask_secret(get_secret(var)) for var in self.required_secrets}

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
        try:
            results = await asyncio.wait_for(self._search(identifier, query), timeout=self.timeout)
        except asyncio.TimeoutError:
            return self._finish(response, ProviderStatus.TIMEOUT, f"Tempo limite de {self.timeout}s excedido")
        except RateLimitedError as exc:
            if exc.retry_after is not None:
                response.metadata["retry_after"] = exc.retry_after
            return self._finish(response, ProviderStatus.RATE_LIMITED, str(exc))
        except AuthRequiredError as exc:
            return self._finish(response, ProviderStatus.AUTH_REQUIRED, str(exc))
        except asyncio.CancelledError:
            self._finish(response, ProviderStatus.CANCELLED, "Execução cancelada")
            raise
        except Exception as exc:  # noqa: BLE001 - falha isolada por provider
            return self._finish(response, ProviderStatus.FAILED, f"{type(exc).__name__}: {exc}")

        max_results = self.settings.search.max_results_per_provider
        response.results = list(results)[:max_results]
        status = ProviderStatus.SUCCESS if response.results else ProviderStatus.NO_RESULTS
        return self._finish(response, status)

    async def enrich(self, entity: Entity) -> ProviderResponse:
        response = ProviderResponse(provider=self.name, query=entity.value, status=ProviderStatus.SKIPPED)
        return self._finish(response, ProviderStatus.SKIPPED, "Enriquecimento não implementado por este provider")

    async def healthcheck(self) -> HealthStatus:
        configured = self.is_configured()
        base = HealthStatus(provider=self.name, status="unknown", configured=configured,
                            credentials=self.masked_credentials())
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
        em vez de retornar lista vazia quando a coleta NÃO foi concluída.
        """

    async def _healthcheck(self) -> bool | str | None:
        """Checagem leve de disponibilidade. Padrão: nada a verificar."""
        return None

    # --- helpers -------------------------------------------------------------------

    @staticmethod
    def _finish(response: ProviderResponse, status: ProviderStatus, error: str | None = None) -> ProviderResponse:
        response.status = status
        response.finished_at = utcnow()
        if error:
            response.errors.append(sanitize(error))
        return response


# --- Especializações por tipo de acesso ---------------------------------------------


class LocalProvider(BaseProvider, ABC):
    """Processamento local, sem rede. Custo zero."""

    provider_type = ProviderType.LOCAL
    tier = SourceTier.TIER_1
    source_tag = "LOCAL"
    classification = DataClassification.DERIVED


class APIProvider(BaseProvider, ABC):
    provider_type = ProviderType.API
    tier = SourceTier.TIER_1


class HTTPProvider(BaseProvider, ABC):
    provider_type = ProviderType.HTTP
    tier = SourceTier.TIER_3
    user_agent: ClassVar[str] = "OSINTIZADA/0.1 (+investigation research tool)"


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
