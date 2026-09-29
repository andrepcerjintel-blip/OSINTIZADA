"""Llama local: Ollama (padrão) ou qualquer servidor OpenAI-compatível (llama.cpp, LM Studio, vLLM).

* Nenhum modelo é fixado no código: sem ``RINO_AI_LLAMA_MODEL`` (ou perfil) → NOT_CONFIGURED.
* Nunca baixa modelos. Modelo ausente → MODEL_NOT_FOUND com a instrução (``ollama pull <modelo>``)
  para o operador executar se quiser.
* Ollama desligado → UNAVAILABLE; o RINO segue funcionando sem IA.
* O endereço vem da configuração do operador (não de conteúdo investigado); redirects não são seguidos.
* CPU offload é problema do runtime: o RINO só aplica timeout (nunca espera indefinidamente).
"""

from __future__ import annotations

import httpx

from osintizada.ai.base import AIModelNotFound, AIProvider, AITimeout, AITransientError, AIUnavailable
from osintizada.ai.models import AIHealth, AIProviderStatus, Completion, ProviderKind, Usage

RUNTIMES = ("ollama", "openai")
LOCAL_COST_NOTE = "custo externo zero; custo computacional local não é medido"


def model_matches(configured: str, available: list[str]) -> bool:
    """Ollama lista ``nome:tag``; ``llama3.1`` corresponde a ``llama3.1:latest``."""
    wanted = configured if ":" in configured else f"{configured}:latest"
    return configured in available or wanted in available


def _raise_for_status(response: httpx.Response, model: str | None) -> None:
    if response.status_code == 404:
        raise AIModelNotFound(f"modelo {model!r} não encontrado no servidor")
    if response.status_code == 429 or response.status_code >= 500:
        raise AITransientError(f"HTTP {response.status_code}")
    if response.status_code >= 400:
        raise AIUnavailable(f"HTTP {response.status_code}")


async def openai_compatible_chat(client: httpx.AsyncClient, model: str, system: str, user: str, *,
                                 temperature: float | None = 0.0, json_mode: bool = True) -> tuple[str, dict]:
    """POST /v1/chat/completions (formato OpenAI; servido também por llama.cpp/LM Studio/vLLM)."""
    body: dict = {"model": model, "messages": [{"role": "system", "content": system},
                                               {"role": "user", "content": user}]}
    if temperature is not None:
        body["temperature"] = temperature
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    response = await client.post("/v1/chat/completions", json=body)
    _raise_for_status(response, model)
    data = response.json()
    try:
        return data["choices"][0]["message"]["content"] or "", data.get("usage") or {}
    except (KeyError, IndexError, TypeError) as exc:
        raise AIUnavailable("resposta sem choices[0].message.content") from exc


class LlamaProvider(AIProvider):
    name = "ai.llama"
    kind = ProviderKind.LOCAL

    def __init__(self, cfg, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.runtime = (cfg.runtime or "ollama").lower()
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

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.cfg.base_url.rstrip("/"), transport=self._transport,
                                 timeout=timeout or self.cfg.timeout_seconds, follow_redirects=False)

    def offline_health(self) -> AIHealth | None:
        base = {"provider": self.name, "kind": self.kind, "enabled": self.enabled, "runtime": self.runtime,
                "model": self.model}
        if self.runtime not in RUNTIMES:
            return AIHealth(**base, status=AIProviderStatus.NOT_CONFIGURED,
                            message=f"runtime inválido {self.runtime!r} (use ollama ou openai)")
        missing = ([] if self.cfg.enabled else ["RINO_AI_LLAMA_ENABLED"]) + ([] if self.cfg.model else
                                                                             ["RINO_AI_LLAMA_MODEL"])
        if missing:
            return AIHealth(**base, status=AIProviderStatus.NOT_CONFIGURED, missing=missing,
                            message="Llama local desativado ou sem modelo configurado")
        return None

    async def _health_check(self) -> AIHealth:
        base = {"provider": self.name, "kind": self.kind, "enabled": True, "runtime": self.runtime, "model": self.model}
        path = "/api/tags" if self.runtime == "ollama" else "/v1/models"
        try:
            async with self._client(timeout=5.0) as client:
                response = await client.get(path)
        except httpx.HTTPError as exc:
            return AIHealth(**base, reachable=False, status=AIProviderStatus.UNAVAILABLE,
                            message=f"servidor {self.runtime} não responde em {self.cfg.base_url} ({type(exc).__name__})")
        if response.status_code >= 400:
            return AIHealth(**base, reachable=True, status=AIProviderStatus.UNAVAILABLE,
                            message=f"HTTP {response.status_code} em {path}")
        data = response.json()
        items = data.get("models", []) if self.runtime == "ollama" else data.get("data", [])
        available = [m.get("name") or m.get("model") or m.get("id") for m in items]
        if not model_matches(self.model, [a for a in available if a]):
            hint = (f"execute manualmente: ollama pull {self.model}" if self.runtime == "ollama"
                    else "carregue o modelo no servidor OpenAI-compatível")
            return AIHealth(**base, reachable=True, status=AIProviderStatus.MODEL_NOT_FOUND,
                            message=f"modelo {self.model!r} não está instalado ({hint}). "
                                    "O RINO não baixa modelos automaticamente.")
        return AIHealth(**base, reachable=True, status=AIProviderStatus.READY)

    async def _complete(self, system: str, user: str) -> Completion:
        if self.offline_health() is not None:
            raise AIUnavailable("Llama local não configurado")
        try:
            async with self._client() as client:
                if self.runtime == "openai":
                    text, usage = await openai_compatible_chat(client, self.model, system, user,
                                                               temperature=self.cfg.temperature)
                    return Completion(text=text, usage=Usage(
                        input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
                        cost_external_usd=0.0, note=LOCAL_COST_NOTE))
                response = await client.post("/api/chat", json={
                    "model": self.model, "stream": False, "format": "json",
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "options": {"temperature": self.cfg.temperature}})
        except httpx.TimeoutException as exc:
            raise AITimeout(f"Llama não respondeu em {self.cfg.timeout_seconds:.0f}s") from exc
        except httpx.ConnectError as exc:
            raise AITransientError(f"Llama recusou a conexão em {self.cfg.base_url}") from exc
        except httpx.HTTPError as exc:
            raise AIUnavailable(f"Llama indisponível ({type(exc).__name__})") from exc
        _raise_for_status(response, self.model)
        data = response.json()
        return Completion(text=(data.get("message") or {}).get("content") or "", usage=Usage(
            input_tokens=data.get("prompt_eval_count"), output_tokens=data.get("eval_count"),
            cost_external_usd=0.0, note=LOCAL_COST_NOTE))
