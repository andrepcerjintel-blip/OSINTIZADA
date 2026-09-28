"""Cache de respostas de providers.

Chave: ``<prefix>:provider:<nome>:<versão do parser>:<sha256(tipo, valor, consulta, parâmetros)>``.
Só respostas concluídas (SUCCESS / NO_RESULTS) são cacheadas: falhas nunca.

Os valores são SEMPRE JSON (nunca pickle): dados externos não podem virar código
ao serem lidos. Quem lê valida o payload e descarta entradas corrompidas.
A interface ``CacheBackend`` é do Core; ``RedisCacheBackend`` fica em
``osintizada.infrastructure`` (o Core não depende de Redis).
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
              params: dict[str, Any] | None = None, prefix: str = "osintizada") -> str:
    payload = json.dumps(
        {"t": identifier_type, "id": " ".join(str(value).split()), "q": " ".join(query.split()) if query else None,
         "params": params or {}},
        sort_keys=True, ensure_ascii=False,
    )
    return f"{prefix}:provider:{provider}:{parser_version}:{hashlib.sha256(payload.encode()).hexdigest()}"


class CacheBackend(ABC):
    """Armazena valores JSON-serializáveis com TTL."""

    @abstractmethod
    def get(self, key: str) -> Any | None: ...

    @abstractmethod
    def set(self, key: str, value: Any, ttl_seconds: float) -> None: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def clear(self) -> None: ...

    @property
    def name(self) -> str:
        return type(self).__name__


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
        return json.loads(value)

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        # Serializa já na escrita: mesma semântica do Redis (JSON), sem referências compartilhadas.
        self._data[key] = (self._clock() + ttl_seconds, json.dumps(value, ensure_ascii=False, default=str))
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


MemoryCacheBackend = InMemoryTTLCache
