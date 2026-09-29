"""AIRouter: decide QUEM executa cada tarefa de IA — e o que NUNCA sai da máquina.

Ordem das decisões:
  1. MODO efetivo = o mais restritivo entre o global (``ai.mode``) e o do Case (``ai_mode``);
  2. ROTA da tarefa (``ai.routing``: local | cloud) e política:
       LOCAL_ONLY → só local; nenhuma chamada de rede a IA externa (nem healthcheck);
       HYBRID     → rota "local": local, e nuvem só se ``allow_cloud_fallback``;
                    rota "cloud": nuvem primeiro, local como alternativa;
       CLOUD      → provider de nuvem preferencial primeiro, local como alternativa;
  3. PRIVACYGATE → material restrito/privado/com credenciais não vai à nuvem; segredos são redigidos
     antes de QUALQUER modelo, inclusive o local;
  4. PREFERÊNCIA do usuário (dentro da política), TAMANHO (sem truncar), CIRCUIT BREAKER e
     DISPONIBILIDADE (healthcheck cacheado);
  5. CACHE (provider+modelo+tarefa+versão do prompt+hash da entrada) antes de chamar o modelo;
  6. RETRY só para erros transitórios (timeout, conexão, 5xx, 429). Erro de esquema não é repetido.
Todas as tentativas ficam no resultado (auditável).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

from osintizada.ai.base import AIProvider, AIProviderError
from osintizada.ai.models import (
    ROUTE_KEYS,
    AIAttempt,
    AIHealth,
    AIMode,
    AIProviderStatus,
    AIResult,
    AIResultStatus,
    AITask,
    ProviderKind,
    Usage,
    most_restrictive,
)
from osintizada.ai.privacy import PrivacyGate
from osintizada.ai.prompts import prompt_id, prompt_size
from osintizada.observability.metrics import metrics
from osintizada.resilience.circuit_breaker import CircuitBreaker

log = logging.getLogger("osintizada.ai.router")

_USABLE = (AIProviderStatus.READY, AIProviderStatus.DEGRADED)
_ERROR_STATUS = {"AI_PARSE_ERROR": AIResultStatus.AI_PARSE_ERROR, "REFUSED": AIResultStatus.REFUSED}


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


class AIRouter:
    def __init__(self, cfg, providers: list[AIProvider], cache=None, cache_prefix: str = "osintizada",
                 sleep=asyncio.sleep) -> None:
        self.cfg = cfg
        self.mode = AIMode(str(cfg.mode).upper())
        self.providers = providers
        self.by_name = {p.name: p for p in providers}
        self.gate = PrivacyGate(cfg.privacy)
        self.cache = cache
        self.cache_prefix = cache_prefix
        self.sleep = sleep
        self.breakers = {p.name: CircuitBreaker(cfg.breaker_failure_threshold, cfg.breaker_recovery_seconds)
                         for p in providers}

    # --- providers por papel ----------------------------------------------------------------------

    @property
    def local(self) -> list[AIProvider]:
        preferred = f"ai.{self.cfg.local_provider}"
        return sorted([p for p in self.providers if p.kind == ProviderKind.LOCAL], key=lambda p: p.name != preferred)

    @property
    def cloud(self) -> list[AIProvider]:
        preferred = f"ai.{self.cfg.cloud_provider}"
        return sorted([p for p in self.providers if p.kind == ProviderKind.CLOUD], key=lambda p: p.name != preferred)

    # --- estado --------------------------------------------------------------------------------

    async def provider_health(self, provider: AIProvider, mode: AIMode | None = None) -> AIHealth:
        mode = mode or self.mode
        if provider.kind == ProviderKind.CLOUD and mode == AIMode.LOCAL_ONLY:
            # LOCAL_ONLY: nenhuma chamada de rede a IA externa — nem para healthcheck.
            return provider.offline_health() or AIHealth(
                provider=provider.name, kind=provider.kind, enabled=provider.enabled, model=provider.model,
                status=AIProviderStatus.UNAVAILABLE, message="não consultado: modo LOCAL_ONLY (sem rede externa)")
        breaker = self.breakers[provider.name]
        if not breaker.allow():
            return AIHealth(provider=provider.name, kind=provider.kind, enabled=provider.enabled, model=provider.model,
                            status=AIProviderStatus.UNAVAILABLE,
                            message=f"circuito aberto após falhas; nova tentativa em {breaker.seconds_until_retry():.0f}s")
        return await provider.healthcheck(max_age=self.cfg.health_cache_seconds)

    async def status(self) -> dict:
        health = [(await self.provider_health(p)).model_dump(mode="json") for p in self.providers]
        usable = [h for h in health if h["status"] in _USABLE and
                  (self.mode != AIMode.LOCAL_ONLY or h["kind"] == ProviderKind.LOCAL)]
        active = usable[0]["provider"] if usable else None
        return {"mode": self.mode.value, "available": bool(usable), "status": "READY" if usable else "AI_UNAVAILABLE",
                "active_provider": active, "local_provider": f"ai.{self.cfg.local_provider}",
                "cloud_provider": f"ai.{self.cfg.cloud_provider}", "allow_cloud_fallback": self.cfg.allow_cloud_fallback,
                "routing": dict(self.cfg.routing), "providers": health}

    # --- planejamento --------------------------------------------------------------------------

    def plan(self, task: AITask, mode: AIMode, route_key: str | None = None) -> tuple[list[AIProvider], str, list[AIAttempt]]:
        route_key = route_key or ROUTE_KEYS[task]
        route = self.cfg.routing.get(route_key, "local")
        blocked: list[AIAttempt] = []
        if mode == AIMode.LOCAL_ONLY:
            order = self.local
            blocked = [AIAttempt(provider=p.name, kind=p.kind, status="SKIPPED_POLICY",
                                 detail="modo LOCAL_ONLY: nenhum conteúdo vai a API externa") for p in self.cloud]
        elif mode == AIMode.HYBRID:
            if route == "cloud":
                order = self.cloud + self.local
            elif self.cfg.allow_cloud_fallback:
                order = self.local + self.cloud
            else:
                order = self.local
                blocked = [AIAttempt(provider=p.name, kind=p.kind, status="SKIPPED_POLICY",
                                     detail=f"HYBRID: rota '{route_key}' é local e allow_cloud_fallback=false")
                           for p in self.cloud]
        else:
            order = self.cloud + self.local
        return order, route, blocked

    # --- execução ------------------------------------------------------------------------------

    async def run(self, task: AITask | str, payload: dict[str, Any], *, preferred: str | None = None,
                  case_mode: AIMode | str | None = None, route_key: str | None = None,
                  meta: dict[str, Any] | None = None) -> AIResult:
        """``meta`` (não enviado ao modelo): fontes/privacidade do material, para o PrivacyGate."""
        task = AITask(task)
        mode = most_restrictive(self.mode, case_mode)
        order, route, attempts = self.plan(task, mode, route_key)
        redaction = self.gate.redact(payload)              # segredos fora — para QUALQUER modelo
        data = redaction.payload
        if any(p.kind == ProviderKind.CLOUD for p in order):
            decision = self.gate.cloud_decision(meta or {}, redaction)
            if not decision.allowed:
                attempts += [AIAttempt(provider=p.name, kind=p.kind, status="SKIPPED_PRIVACY",
                                       detail="; ".join(decision.reasons)) for p in order if p.kind == ProviderKind.CLOUD]
                order = [p for p in order if p.kind != ProviderKind.CLOUD]
        if preferred:
            if preferred in self.by_name and preferred not in [p.name for p in order]:
                attempts.append(AIAttempt(provider=preferred, kind=self.by_name[preferred].kind,
                                          status="SKIPPED_POLICY", detail="preferência não permitida pela política"))
            order = [p for p in order if p.name == preferred] + [p for p in order if p.name != preferred]
        size = prompt_size(task, data)
        version = prompt_id(task)
        too_large = 0
        last_error: AIProviderError | None = None
        first = order[0].name if order else None

        for provider in order:
            if size > provider.max_input_chars:
                too_large += 1
                attempts.append(AIAttempt(provider=provider.name, kind=provider.kind, status="SKIPPED_TOO_LARGE",
                                          detail=f"{size} caracteres > limite {provider.max_input_chars}"))
                continue
            health = await self.provider_health(provider, mode)
            if health.status not in _USABLE:
                attempts.append(AIAttempt(provider=provider.name, kind=provider.kind,
                                          status=f"SKIPPED_{health.status.value}", detail=health.message))
                continue
            key = self._cache_key(provider, task, version, data)
            if (hit := self._cache_get(key)) is not None:
                attempts.append(AIAttempt(provider=provider.name, kind=provider.kind, status="CACHE_HIT"))
                metrics.inc("ai_cache_hits", task=task.value)
                return self._result(task, mode, route, provider, hit["output"], Usage(**hit["usage"]), 0.0,
                                    version, attempts, cached=True, fallback=provider.name != first)
            outcome = await self._call(provider, task, data, attempts)
            if isinstance(outcome, AIProviderError):
                last_error = outcome
                continue
            output, usage, ms = outcome
            self._cache_set(key, {"output": output, "usage": usage.model_dump()})
            fallback = provider.name != first
            if fallback:
                metrics.inc("ai_fallbacks", task=task.value)
            return self._result(task, mode, route, provider, output, usage, ms, version, attempts,
                                fallback=fallback)
        status, reason = self._failure(task, mode, order, too_large, last_error)
        metrics.inc("ai_failures", task=task.value, status=status.value)
        return AIResult(task=task, status=status, mode=mode, route=route, prompt_version=version,
                        attempts=attempts, reason=reason)

    async def _call(self, provider: AIProvider, task: AITask, data: dict, attempts: list[AIAttempt]):
        breaker = self.breakers[provider.name]
        for attempt in range(self.cfg.retries + 1):
            started = time.perf_counter()
            metrics.inc("ai_requests", provider=provider.name, task=task.value)
            metrics.inc("ai_local_requests" if provider.kind == ProviderKind.LOCAL else "ai_cloud_requests")
            try:
                output, usage = await provider.run(task, data)
            except AIProviderError as exc:
                ms = round((time.perf_counter() - started) * 1000, 1)
                attempts.append(AIAttempt(provider=provider.name, kind=provider.kind, status=exc.code,
                                          detail=str(exc)[:300], latency_ms=ms))
                metrics.inc("ai_failures", provider=provider.name, task=task.value, code=exc.code)
                log.warning("tarefa de IA falhou", extra={"provider": provider.name, "task": task.value,
                                                          "error": exc.code})
                if exc.transient:
                    breaker.record_failure()
                    if attempt < self.cfg.retries and breaker.allow():
                        await self.sleep(0.5 * (2 ** attempt))
                        continue
                return exc
            ms = round((time.perf_counter() - started) * 1000, 1)
            breaker.record_success()
            attempts.append(AIAttempt(provider=provider.name, kind=provider.kind, status="OK", latency_ms=ms))
            metrics.observe("ai_latency_seconds", ms / 1000, provider=provider.name)
            log.info("tarefa de IA executada", extra={"provider": provider.name, "task": task.value})
            return output, usage, ms
        return None  # pragma: no cover

    def _result(self, task, mode, route, provider, output, usage, ms, version, attempts, *, cached=False,
                fallback=False) -> AIResult:
        return AIResult(task=task, status=AIResultStatus.OK, mode=mode, route=route, provider=provider.name,
                        provider_kind=provider.kind, model=provider.model, output=output,
                        output_hash=canonical_hash(output), usage=usage, latency_ms=ms, prompt_version=version,
                        cached=cached, fallback_used=fallback, attempts=attempts,
                        reason=(f"{task.value} executada localmente ({mode.value}): nada enviado a API externa"
                                if provider.kind == ProviderKind.LOCAL else
                                f"{task.value} enviada a {provider.name} (modo {mode.value}); segredos redigidos"))

    def _failure(self, task, mode, order, too_large, last_error) -> tuple[AIResultStatus, str]:
        if not order:
            return AIResultStatus.POLICY_BLOCKED, f"nenhum provider permitido para {task.value} no modo {mode.value}"
        if too_large == len(order):
            return AIResultStatus.CONTENT_TOO_LARGE, "item maior que o limite de todos os providers permitidos"
        if last_error is not None and last_error.code in _ERROR_STATUS:
            return _ERROR_STATUS[last_error.code], str(last_error)
        suffix = (" No modo LOCAL_ONLY nada é enviado a APIs externas: configure/ligue o Llama local."
                  if mode == AIMode.LOCAL_ONLY else "")
        return AIResultStatus.AI_UNAVAILABLE, f"nenhum provider de IA disponível para {task.value}.{suffix}"

    # --- cache ---------------------------------------------------------------------------------

    def _cache_key(self, provider: AIProvider, task: AITask, version: str, data: dict) -> str:
        return (f"{self.cache_prefix}:ai:{provider.name}:{provider.model}:{task.value}:{version}:"
                f"{canonical_hash(data)}")

    def _cache_get(self, key: str) -> dict | None:
        if self.cache is None or self.cfg.cache_ttl_seconds <= 0:
            return None
        try:
            value = self.cache.get(key)
        except Exception:  # noqa: BLE001 - cache nunca derruba a análise
            return None
        return value if isinstance(value, dict) and "output" in value else None

    def _cache_set(self, key: str, value: dict) -> None:
        if self.cache is None or self.cfg.cache_ttl_seconds <= 0:
            return
        try:
            self.cache.set(key, value, self.cfg.cache_ttl_seconds)
        except Exception:  # noqa: BLE001
            pass

    async def aclose(self) -> None:
        for p in self.providers:
            await p.aclose()


# --- montagem a partir da configuração -------------------------------------------------------------


def _truthy(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.strip().lower() in ("1", "true", "yes", "sim", "on")


def effective_ai_settings(settings):
    """``settings.ai`` com as variáveis RINO_AI_* aplicadas (env tem precedência) e o perfil resolvido."""
    from osintizada.branding import env

    cfg = settings.ai.model_copy(deep=True)
    if (mode := env("AI_MODE")) is not None:
        cfg.mode = mode.strip().upper()
    if (enabled := _truthy(env("AI_LLAMA_ENABLED"))) is not None:
        cfg.llama.enabled = enabled
    for attr, var in (("base_url", "AI_LLAMA_BASE_URL"), ("model", "AI_LLAMA_MODEL"), ("runtime", "AI_LLAMA_RUNTIME")):
        if value := env(var):
            setattr(cfg.llama, attr, value.strip())
    if value := env("AI_PROFILE"):
        cfg.profile = value.strip().lower()
    if cfg.profile:
        chosen = cfg.profiles.get(cfg.profile)
        if chosen and not env("AI_LLAMA_MODEL"):
            cfg.llama.model = chosen                      # perfil → modelo escolhido PELO USUÁRIO
    if value := env("AI_LOCAL_TIMEOUT"):
        cfg.llama.timeout_seconds = float(value)
    if value := env("AI_CLOUD_TIMEOUT"):
        cfg.claude.timeout_seconds = cfg.openai.timeout_seconds = float(value)
    if value := env("AI_CLOUD_PROVIDER"):
        cfg.cloud_provider = value.strip().lower()
    if (fallback := _truthy(env("AI_ALLOW_CLOUD_FALLBACK"))) is not None:
        cfg.allow_cloud_fallback = fallback
    if value := env("AI_CLAUDE_MODEL"):
        cfg.claude.model = value.strip()
    if (enabled := _truthy(env("AI_CLAUDE_ENABLED"))) is not None:
        cfg.claude.enabled = enabled
    if value := env("AI_OPENAI_MODEL"):
        cfg.openai.model = value.strip()
    if value := env("AI_OPENAI_BASE_URL"):
        cfg.openai.base_url = value.strip()
    if (enabled := _truthy(env("AI_OPENAI_ENABLED"))) is not None:
        cfg.openai.enabled = enabled
    AIMode(cfg.mode)  # valida (ValueError com modo inválido)
    if cfg.cloud_provider not in ("claude", "openai"):
        raise ValueError(f"ai.cloud_provider inválido: {cfg.cloud_provider!r} (use claude ou openai)")
    return cfg


def build_ai_router(settings, *, cache=None, llama_transport=None, openai_transport=None,
                    claude_http_client=None) -> AIRouter:
    from osintizada.ai.providers.claude import ClaudeProvider
    from osintizada.ai.providers.llama import LlamaProvider
    from osintizada.ai.providers.openai import OpenAIProvider

    cfg = effective_ai_settings(settings)
    providers: list[AIProvider] = [
        LlamaProvider(cfg.llama, transport=llama_transport),
        ClaudeProvider(cfg.claude, http_client=claude_http_client),
        OpenAIProvider(cfg.openai, transport=openai_transport),
    ]
    return AIRouter(cfg, providers, cache=cache, cache_prefix=settings.cache.prefix)
