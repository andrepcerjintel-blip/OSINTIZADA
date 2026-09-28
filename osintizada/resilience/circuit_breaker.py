"""Circuit breaker por provider.

CLOSED → (falhas consecutivas ≥ limite) → OPEN → (após recovery) → HALF_OPEN
HALF_OPEN → sucesso → CLOSED | falha → OPEN
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_seconds = recovery_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open_in_flight = False

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._clock() - self._opened_at >= self.recovery_seconds:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    def allow(self) -> bool:
        state = self.state
        if state == BreakerState.CLOSED:
            return True
        if state == BreakerState.HALF_OPEN and not self._half_open_in_flight:
            self._half_open_in_flight = True  # uma única chamada de teste
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._half_open_in_flight = False

    def record_failure(self) -> None:
        self._failures += 1
        self._half_open_in_flight = False
        if self._opened_at is not None or self._failures >= self.failure_threshold:
            self._opened_at = self._clock()

    def seconds_until_retry(self) -> float:
        if self._opened_at is None:
            return 0.0
        return max(0.0, self.recovery_seconds - (self._clock() - self._opened_at))
