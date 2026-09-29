"""OpenAI (ou endpoint remoto compatível) via HTTP ``/v1/chat/completions``.

* Sem ``OPENAI_API_KEY`` ou sem ``RINO_AI_OPENAI_MODEL`` → NOT_CONFIGURED. O modelo não tem padrão no
  código (é escolha do operador).
* Healthcheck por ``GET /v1/models/{modelo}`` (não consome tokens).
"""

from __future__ import annotations

import os

import httpx

from osintizada.ai.base import AIProvider, AITimeout, AITransientError, AIUnavailable
from osintizada.ai.models import AIHealth, AIProviderStatus, Completion, ProviderKind, Usage
from osintizada.ai.providers.claude import estimate_cost
from osintizada.ai.providers.llama import openai_compatible_chat


class OpenAIProvider(AIProvider):
    name = "ai.openai"
    kind = ProviderKind.CLOUD

    def __init__(self, cfg, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.cfg = cfg
        self._transport = transport

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

    def offline_health(self) -> AIHealth | None:
        base = {"provider": self.name, "kind": self.kind, "enabled": self.enabled, "runtime": "openai",
                "model": self.model}
        if not self.cfg.enabled:
            return AIHealth(**base, status=AIProviderStatus.NOT_CONFIGURED, message="OpenAI desativado")
        missing = ([] if os.environ.get("OPENAI_API_KEY") else ["OPENAI_API_KEY"]) + \
                  ([] if self.cfg.model else ["RINO_AI_OPENAI_MODEL"])
        if missing:
            return AIHealth(**base, status=AIProviderStatus.NOT_CONFIGURED, missing=missing)
        return None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.cfg.base_url.rstrip("/"), transport=self._transport,
                                 timeout=self.cfg.timeout_seconds, follow_redirects=False,
                                 headers={"authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"})

    async def _health_check(self) -> AIHealth:
        base = {"provider": self.name, "kind": self.kind, "enabled": True, "runtime": "openai", "model": self.model}
        try:
            async with self._client() as client:
                response = await client.get(f"/v1/models/{self.model}")
        except httpx.HTTPError as exc:
            return AIHealth(**base, reachable=False, status=AIProviderStatus.UNAVAILABLE, message=type(exc).__name__)
        if response.status_code == 404:
            return AIHealth(**base, reachable=True, status=AIProviderStatus.MODEL_NOT_FOUND,
                            message=f"modelo {self.model!r} inexistente")
        if response.status_code >= 400:
            return AIHealth(**base, reachable=True, status=AIProviderStatus.UNAVAILABLE,
                            message="credencial recusada" if response.status_code in (401, 403)
                            else f"HTTP {response.status_code}")
        return AIHealth(**base, reachable=True, status=AIProviderStatus.READY)

    async def _complete(self, system: str, user: str) -> Completion:
        if self.offline_health() is not None:
            raise AIUnavailable("OpenAI não configurado")
        try:
            async with self._client() as client:
                text, usage = await openai_compatible_chat(client, self.model, system, user, temperature=None)
        except httpx.TimeoutException as exc:
            raise AITimeout("OpenAI não respondeu no tempo limite") from exc
        except httpx.ConnectError as exc:
            raise AITransientError("sem conexão com a OpenAI") from exc
        except httpx.HTTPError as exc:
            raise AIUnavailable(f"OpenAI indisponível ({type(exc).__name__})") from exc
        tokens_in, tokens_out = usage.get("prompt_tokens"), usage.get("completion_tokens")
        return Completion(text=text, usage=Usage(
            input_tokens=tokens_in, output_tokens=tokens_out,
            cost_external_usd=estimate_cost(self.cfg.price_per_mtok, self.model, tokens_in, tokens_out)))
