"""Contrato genérico de IA. O Core do RINO não conhece Ollama, Claude nem OpenAI.

Cada provider implementa só o transporte (``_health_check`` e ``_complete``). As operações do contrato
(classify_content, extract_entities, summarize, translate, generate_queries, assess_relevance), a
montagem do prompt, o parsing com reparo simples e a VALIDAÇÃO de esquema ficam aqui, iguais para todos.
"""

from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any

from pydantic import ValidationError

from osintizada.ai.models import OUTPUT_MODELS, AIHealth, AIProviderStatus, AITask, Completion, ProviderKind, Usage
from osintizada.ai.prompts import build_prompt

log = logging.getLogger("osintizada.ai")


class AIProviderError(Exception):
    code = "ERROR"
    transient = False       # só erros transitórios são repetidos pelo router


class AINotConfigured(AIProviderError):
    code = "NOT_CONFIGURED"


class AIUnavailable(AIProviderError):
    code = "UNAVAILABLE"


class AITransientError(AIProviderError):
    """Conexão recusada, HTTP 5xx, rate limit — pode dar certo numa nova tentativa."""

    code = "TRANSIENT_ERROR"
    transient = True


class AITimeout(AITransientError):
    code = "TIMEOUT"


class AIModelNotFound(AIProviderError):
    code = "MODEL_NOT_FOUND"


class AIParseError(AIProviderError):
    code = "AI_PARSE_ERROR"


class AIRefused(AIProviderError):
    code = "REFUSED"


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def parse_json_object(text: str) -> dict:
    """JSON da resposta com reparo SIMPLES: remove cercas ```, recorta o objeto e tira vírgulas finais.

    Nada além disso é "adivinhado": se não virar um objeto JSON, é ``AI_PARSE_ERROR``.
    """
    cleaned = _FENCE.sub("", (text or "").strip())
    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if 0 <= start < end:
        candidates.append(cleaned[start:end + 1])
    for candidate in list(candidates):
        candidates.append(_TRAILING_COMMA.sub(r"\1", candidate))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise AIParseError("resposta não é um objeto JSON válido (reparo simples falhou)")


class AIProvider(ABC):
    name: str = "ai.base"
    kind: ProviderKind = ProviderKind.LOCAL
    degraded_after_failures: int = 2

    def __init__(self) -> None:
        self.consecutive_failures = 0
        self._health: AIHealth | None = None
        self._health_at = 0.0

    # --- transporte (implementado por cada provider) ---------------------------------------------

    @property
    @abstractmethod
    def model(self) -> str | None: ...

    @property
    @abstractmethod
    def enabled(self) -> bool: ...

    @property
    @abstractmethod
    def max_input_chars(self) -> int: ...

    @property
    @abstractmethod
    def max_batch_items(self) -> int: ...

    @abstractmethod
    async def _health_check(self) -> AIHealth: ...

    @abstractmethod
    async def _complete(self, system: str, user: str) -> Completion:
        """Envia o prompt; devolve o TEXTO (esperado: objeto JSON) e o uso de tokens quando houver."""

    def offline_health(self) -> AIHealth | None:
        """Estado determinável SEM rede (configuração). None = é preciso consultar o serviço."""
        return None

    async def aclose(self) -> None:  # noqa: B027 - opcional
        pass

    # --- estado --------------------------------------------------------------------------------

    async def healthcheck(self, max_age: float = 0.0) -> AIHealth:
        """Estado atual (cacheado por ``max_age`` s). Nunca levanta exceção."""
        if self._health is not None and max_age and time.monotonic() - self._health_at < max_age:
            return self._apply_degraded(self._health)
        started = time.perf_counter()
        try:
            health = self.offline_health() or await self._health_check()
        except Exception as exc:  # noqa: BLE001 - health nunca derruba a aplicação
            health = AIHealth(provider=self.name, kind=self.kind, enabled=self.enabled, reachable=False,
                              status=AIProviderStatus.UNAVAILABLE, model=self.model, message=type(exc).__name__)
        health.latency_ms = round((time.perf_counter() - started) * 1000, 1)
        self._health, self._health_at = health, time.monotonic()
        return self._apply_degraded(health)

    def _apply_degraded(self, health: AIHealth) -> AIHealth:
        if health.status == AIProviderStatus.READY and self.consecutive_failures >= self.degraded_after_failures:
            return health.model_copy(update={
                "status": AIProviderStatus.DEGRADED,
                "message": f"{self.consecutive_failures} chamadas seguidas falharam (timeout ou saída inválida)"})
        return health

    # --- execução ------------------------------------------------------------------------------

    async def run(self, task: AITask, payload: dict[str, Any]) -> tuple[dict, Usage]:
        """Executa a tarefa. Devolve (saída VALIDADA pelo esquema, uso). Sem repetição por erro de esquema."""
        system, user = build_prompt(task, payload)
        try:
            completion = await self._complete(system, user)
            output = OUTPUT_MODELS[task].model_validate(parse_json_object(completion.text)).model_dump(mode="json")
        except ValidationError as exc:
            self.consecutive_failures += 1
            raise AIParseError(f"saída fora do esquema ({exc.error_count()} erro(s))") from None
        except AIProviderError:
            self.consecutive_failures += 1
            raise
        self.consecutive_failures = 0
        return output, completion.usage

    # --- contrato de operações (iguais para todos os providers) ------------------------------------

    async def classify_content(self, items: list[dict], labels: list[str]) -> dict:
        return (await self.run(AITask.CLASSIFY_CONTENT, {"data": items, "labels": labels}))[0]

    async def extract_entities(self, items: list[dict]) -> dict:
        return (await self.run(AITask.EXTRACT_ENTITIES, {"data": items}))[0]

    async def summarize(self, data: Any) -> dict:
        return (await self.run(AITask.SUMMARIZE, {"data": data}))[0]

    async def translate(self, items: list[dict], target_language: str = "pt") -> dict:
        return (await self.run(AITask.TRANSLATE, {"data": items, "target_language": target_language}))[0]

    async def generate_queries(self, entities: list[dict], max_queries: int = 10) -> dict:
        return (await self.run(AITask.GENERATE_QUERIES, {"data": entities, "max_queries": max_queries}))[0]

    async def assess_relevance(self, items: list[dict], targets: list[str], objective: str | None = None) -> dict:
        return (await self.run(AITask.ASSESS_RELEVANCE,
                               {"data": items, "targets": targets, "objective": objective}))[0]
