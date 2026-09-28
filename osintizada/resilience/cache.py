"""Cache de respostas de providers.

Chave = provider + versão do parser + tipo + valor + consulta + parâmetros.
Só respostas concluídas (SUCCESS / NO_RESULTS) são cacheadas: falhas nunca.
A interface ``CacheBackend`` permite trocar por Redis sem alterar providers.
"""

from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Callable
from typing import Any


def cache_key(provider: str, parser_version: str, identifier_type: str, value: str, query: str | None,
              params: dict[str, Any] | None = None) -> str:
    payload = json.dumps(
        {"p": provider, "v": parser_version, "t": identifier_type, "id": value, "q": query, "params": params or {}},
        sort_keys=True, ensure_ascii=False,
    )
    return f"osintizada:{provider}:{hashlib.sha256(payload.encode()).hexdigest()}"


class CacheBackend(ABC):
    @abstractmethod
    def get(self, key: str) -> Any | None: ...

    @abstractmethod
    def set(self, key: str, value: Any, ttl_seconds: float) -> None: ...

    @abstractmethod
    def clear(self) -> None: ...


class InMemoryTTLCache(CacheBackend):
    def __init__(self, max_entries: int = 10_000, clock: Callable[[], float] = time.monotonic) -> None:
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self.max_entries = max_entries
        self._clock = clock
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if item is None:
            self.misses += 1
            return None
        expires, value = item
        if expires <= self._clock():
            del self._data[key]
            self.misses += 1
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        self._data[key] = (self._clock() + ttl_seconds, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
