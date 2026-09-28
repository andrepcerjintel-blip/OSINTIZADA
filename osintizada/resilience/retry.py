"""Retry com backoff exponencial e jitter — somente para falhas transitórias.

Rate limit (429) NÃO é re-tentado aqui: o provider entra em cooldown e o
status RATE_LIMITED é registrado (não insistir agressivamente).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class TransientError(Exception):
    """Falha possivelmente temporária (5xx, conexão, timeout de leitura)."""


def backoff_delay(attempt: int, base: float, maximum: float, jitter: float = 0.25,
                  rand: Callable[[], float] = random.random) -> float:
    """Atraso para a tentativa ``attempt`` (1 = primeira re-tentativa)."""
    delay = min(maximum, base * (2 ** (attempt - 1)))
    return max(0.0, delay * (1 + jitter * (2 * rand() - 1)))


async def retry_async(
    func: Callable[[], Awaitable[T]],
    max_retries: int,
    base: float,
    maximum: float,
    on_retry: Callable[[int, Exception, float], None] | None = None,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> T:
    attempt = 0
    while True:
        try:
            return await func()
        except TransientError as exc:
            attempt += 1
            if attempt > max_retries:
                raise
            delay = backoff_delay(attempt, base, maximum)
            if on_retry:
                on_retry(attempt, exc, delay)
            await sleep(delay)
