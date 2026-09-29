"""Claude (Anthropic) via SDK oficial ``anthropic`` — dependência opcional (``pip install -e ".[ai-cloud]"``).

* Sem SDK ou sem credencial (ANTHROPIC_API_KEY) → NOT_CONFIGURED (nunca chave fictícia).
* Healthcheck pelo Models API (``models.retrieve``): não consome tokens.
* Fallback de recusa no servidor habilitado por padrão (``fallbacks="default"``); ``stop_reason``
  ``refusal`` vira REFUSED — nunca é lido como resposta.
* Uso de tokens e custo estimado (tabela ``price_per_mtok`` configurável) vão para a proveniência.
* Repetição fica com o router (só erros transitórios): o SDK roda com ``max_retries=0``.
"""

from __future__ import annotations

import os

from osintizada.ai.base import AIModelNotFound, AIProvider, AIRefused, AITimeout, AITransientError, AIUnavailable
from osintizada.ai.models import AIHealth, AIProviderStatus, Completion, ProviderKind, Usage

FALLBACK_BETA = "server-side-fallback-2026-07-01"
CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def estimate_cost(prices: dict[str, list[float]], model: str | None, tokens_in: int | None,
                  tokens_out: int | None) -> float | None:
    price = prices.get(model or "")
    if not price or tokens_in is None or tokens_out is None:
        return None
    return round(tokens_in / 1e6 * price[0] + tokens_out / 1e6 * price[1], 6)


class ClaudeProvider(AIProvider):
    name = "ai.claude"
    kind = ProviderKind.CLOUD

    def __init__(self, cfg, http_client=None) -> None:
        super().__init__()
        self.cfg = cfg
        self._http_client = http_client   # testes: anthropic.DefaultAsyncHttpxClient(transport=MockTransport)
        self._client = None

    @property
    def model(self) -> str | None:
        return self.cfg.model

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    @property
    def max_input_chars(self) -> int:
        return self.cfg.max_input_chars

    @property
    def max_batch_items(self) -> int:
        return self.cfg.max_batch_items

    @staticmethod
    def sdk_available() -> bool:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return True

    def offline_health(self) -> AIHealth | None:
        base = {"provider": self.name, "kind": self.kind, "enabled": self.enabled, "runtime": "anthropic",
                "model": self.model}
        if not self.cfg.enabled:
            return AIHealth(**base, status=AIProviderStatus.NOT_CONFIGURED, message="Claude desativado")
        if not self.sdk_available():
            return AIHealth(**base, status=AIProviderStatus.NOT_CONFIGURED,
                            message='SDK "anthropic" não instalado (pip install -e ".[ai-cloud]")')
        if not any(os.environ.get(v) for v in CREDENTIAL_VARS):
            return AIHealth(**base, status=AIProviderStatus.NOT_CONFIGURED, missing=["ANTHROPIC_API_KEY"],
                            message="credencial da Anthropic não configurada")
        return None

    def _sdk(self):
        if self._client is None:
            import anthropic

            kwargs = {"timeout": self.cfg.timeout_seconds, "max_retries": 0}
            if self._http_client is not None:
                kwargs["http_client"] = self._http_client
            self._client = anthropic.AsyncAnthropic(**kwargs)
        return self._client

    async def _health_check(self) -> AIHealth:
        import anthropic

        base = {"provider": self.name, "kind": self.kind, "enabled": True, "runtime": "anthropic", "model": self.model}
        try:
            await self._sdk().models.retrieve(self.model)
        except anthropic.NotFoundError:
            return AIHealth(**base, reachable=True, status=AIProviderStatus.MODEL_NOT_FOUND,
                            message=f"modelo {self.model!r} inexistente")
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            return AIHealth(**base, reachable=True, status=AIProviderStatus.UNAVAILABLE,
                            message="credencial recusada pela API")
        except anthropic.APIConnectionError:
            return AIHealth(**base, reachable=False, status=AIProviderStatus.UNAVAILABLE,
                            message="sem conexão com a API da Anthropic")
        except anthropic.APIStatusError as exc:
            return AIHealth(**base, reachable=True, status=AIProviderStatus.UNAVAILABLE, message=f"HTTP {exc.status_code}")
        return AIHealth(**base, reachable=True, status=AIProviderStatus.READY)

    async def _complete(self, system: str, user: str) -> Completion:
        if self.offline_health() is not None:
            raise AIUnavailable("Claude não configurado")
        import anthropic

        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.cfg.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"effort": self.cfg.effort},
        }
        if self.cfg.fallbacks:
            kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
        try:
            response = await self._sdk().beta.messages.create(**kwargs)
        except anthropic.APITimeoutError as exc:
            raise AITimeout("Claude não respondeu no tempo limite") from exc
        except anthropic.NotFoundError as exc:
            raise AIModelNotFound(f"modelo {self.model!r} inexistente") from exc
        except anthropic.RateLimitError as exc:
            raise AITransientError("limite de requisições da Anthropic (429)") from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise AIUnavailable("credencial recusada pela API") from exc
        except anthropic.APIConnectionError as exc:
            raise AITransientError("sem conexão com a API da Anthropic") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise AITransientError(f"HTTP {exc.status_code} da API da Anthropic") from exc
            raise AIUnavailable(f"HTTP {exc.status_code} da API da Anthropic") from exc
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise AIRefused(f"pedido recusado ({getattr(details, 'category', None) or 'sem categoria'})")
        tokens_in = getattr(response.usage, "input_tokens", None)
        tokens_out = getattr(response.usage, "output_tokens", None)
        served_by = getattr(response, "model", None) or self.model
        return Completion(
            text="".join(block.text for block in response.content if block.type == "text"),
            usage=Usage(input_tokens=tokens_in, output_tokens=tokens_out,
                        cost_external_usd=estimate_cost(self.cfg.price_per_mtok, served_by, tokens_in, tokens_out),
                        note=None if served_by == self.model else f"atendido por {served_by} (fallback)"))

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
