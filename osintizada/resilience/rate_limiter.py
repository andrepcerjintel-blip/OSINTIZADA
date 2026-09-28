"""Rate limiting por provider (token bucket assíncrono) com cooldown após HTTP 429."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class TokenBucket:
    """Token bucket: ``rate_per_minute`` requisições, com rajada de até ``burst``."""

    def __init__(
        self,
        rate_per_minute: float,
        burst: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute deve ser positivo")
        self.rate_per_second = rate_per_minute / 60.0
        self.capacity = float(burst or max(1, int(rate_per_minute // 60) or 1))
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate_per_second)
        self._updated = now

    def wait_time(self) -> float:
        self._refill()
        if self._tokens >= 1:
            return 0.0
        return (1 - self._tokens) / self.rate_per_second

    async def acquire(self) -> float:
        """Aguarda um token; retorna o tempo esperado (s)."""
        waited = 0.0
        async with self._lock:
            while True:
                delay = self.wait_time()
                if delay <= 0:
                    self._tokens -= 1
                    return waited
                await self._sleep(delay)
                waited += delay


class RateLimiter:
    """Conjunto de buckets por chave (provider) + cooldowns impostos pelo serviço."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], object] = asyncio.sleep) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._cooldown_until: dict[str, float] = {}
        self._clock = clock
        self._sleep = sleep

    def configure(self, key: str, rate_per_minute: float | None) -> None:
        if rate_per_minute and key not in self._buckets:
            self._buckets[key] = TokenBucket(rate_per_minute, clock=self._clock, sleep=self._sleep)

    async def acquire(self, key: str) -> float:
        bucket = self._buckets.get(key)
        return await bucket.acquire() if bucket else 0.0

    def set_cooldown(self, key: str, seconds: float) -> None:
        """Registra que o serviço pediu para aguardar (429 / Retry-After)."""
        until = self._clock() + max(0.0, seconds)
        self._cooldown_until[key] = max(until, self._cooldown_until.get(key, 0.0))

    def cooldown_remaining(self, key: str) -> float:
        remaining = self._cooldown_until.get(key, 0.0) - self._clock()
        return remaining if remaining > 0 else 0.0
