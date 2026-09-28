"""Estado compartilhado de execução dos providers.

Uma instância é criada por orquestrador/processo e injetada em todos os
providers, para que rate limits, circuit breakers, cache e métricas sejam
compartilhados entre chamadas.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable

import httpx

from osintizada.resilience.cache import CacheBackend, InMemoryTTLCache
from osintizada.resilience.circuit_breaker import CircuitBreaker
from osintizada.resilience.rate_limiter import RateLimiter


class ProviderRuntime:
    def __init__(
        self,
        cache: CacheBackend | None = None,
        clock: Callable[[], float] = time.monotonic,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[[str], object] | None = None,
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        self.cache = cache if cache is not None else InMemoryTTLCache(clock=clock)
        self.rate_limiter = RateLimiter(clock=clock, sleep=sleep)
        self.clock = clock
        self.sleep = sleep
        # Injeções para testes/ambientes controlados.
        self.transport = transport
        self.resolver = resolver
        self.dns_resolver: object | None = None  # injeção de resolver DNS (testes)
        self.telegram_client_factory: Callable[[], object] | None = None  # injeção de cliente Telegram (testes)
        self.last_errors: dict[str, dict] = {}
        self.last_latency_ms: dict[str, float] = {}
        self._global_semaphore: asyncio.Semaphore | None = None
        self._breakers: dict[str, CircuitBreaker] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self.metrics: Counter[str] = Counter()

    def breaker(self, key: str, failure_threshold: int, recovery_seconds: float) -> CircuitBreaker:
        if key not in self._breakers:
            self._breakers[key] = CircuitBreaker(failure_threshold, recovery_seconds, clock=self.clock)
        return self._breakers[key]

    def semaphore(self, key: str, limit: int | None) -> asyncio.Semaphore | None:
        if not limit:
            return None
        if key not in self._semaphores:
            self._semaphores[key] = asyncio.Semaphore(limit)
        return self._semaphores[key]

    def global_semaphore(self, limit: int) -> asyncio.Semaphore:
        """Semáforo global compartilhado por todas as rodadas deste runtime."""
        if self._global_semaphore is None:
            self._global_semaphore = asyncio.Semaphore(max(1, limit))
        return self._global_semaphore

    def count(self, metric: str, n: int = 1) -> None:
        self.metrics[metric] += n
