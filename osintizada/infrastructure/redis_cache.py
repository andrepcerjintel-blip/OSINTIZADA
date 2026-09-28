"""RedisCacheBackend — cache compartilhado entre processos (API, workers).

* valores em JSON (nunca pickle); payload inválido é descartado com warning;
* TTL nativo do Redis (SET … EX);
* Redis indisponível NÃO derruba a coleta: leitura vira miss, escrita é ignorada, e o
  evento é registrado (log + métrica). O provider simplesmente executa de novo.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis

from osintizada.observability.metrics import metrics
from osintizada.resilience.cache import CacheBackend

log = logging.getLogger("osintizada.cache")


class RedisCacheBackend(CacheBackend):
    def __init__(self, client: redis.Redis, prefix: str = "osintizada") -> None:
        self.client = client
        self.prefix = prefix
        self.hits = 0
        self.misses = 0
        self.errors = 0

    def get(self, key: str) -> Any | None:
        try:
            raw = self.client.get(key)
        except redis.RedisError as exc:
            self._error("get", exc)
            return None
        if raw is None:
            self.misses += 1
            metrics.inc("redis_cache_miss")
            return None
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            log.warning("payload de cache corrompido descartado", extra={"cache_key": key})
            metrics.inc("redis_cache_corrupted")
            self.delete(key)
            self.misses += 1
            return None
        self.hits += 1
        metrics.inc("redis_cache_hit")
        return value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        try:
            self.client.set(key, json.dumps(value, ensure_ascii=False, default=str), ex=max(1, int(ttl_seconds)))
        except redis.RedisError as exc:
            self._error("set", exc)

    def delete(self, key: str) -> None:
        try:
            self.client.delete(key)
        except redis.RedisError as exc:
            self._error("delete", exc)

    def clear(self) -> None:
        """Remove apenas as chaves de cache de providers deste prefixo."""
        try:
            for key in self.client.scan_iter(match=f"{self.prefix}:provider:*", count=500):
                self.client.delete(key)
        except redis.RedisError as exc:
            self._error("clear", exc)

    def ttl(self, key: str) -> int:
        return int(self.client.ttl(key))

    def _error(self, op: str, exc: Exception) -> None:
        self.errors += 1
        metrics.inc("redis_cache_error")
        log.warning("Redis indisponível no cache; seguindo sem cache", extra={"operation": op,
                                                                             "error": type(exc).__name__})


def build_cache(settings, client: redis.Redis | None = None) -> CacheBackend:
    """Seleciona o backend pela configuração (``cache.backend``)."""
    from osintizada.resilience.cache import MemoryCacheBackend

    if settings.cache.backend == "redis":
        if client is None:
            from osintizada.infrastructure.redis_client import create_redis

            client = create_redis(settings=settings)
        return RedisCacheBackend(client, prefix=settings.cache.prefix)
    return MemoryCacheBackend(max_entries=settings.cache.max_memory_entries)
