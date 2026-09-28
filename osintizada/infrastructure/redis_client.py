"""Conexão Redis a partir de ``REDIS_URL`` (ou ``redis.url`` na configuração).

Autenticação e TLS vão na própria URL (``rediss://:senha@host:6380/0``); nada é hardcoded.
"""

from __future__ import annotations

import os
from functools import lru_cache

import redis

from osintizada.config import Settings, get_settings


class RedisNotConfigured(RuntimeError):
    """Nenhuma REDIS_URL configurada."""


def redis_url(settings: Settings | None = None) -> str | None:
    return os.environ.get("REDIS_URL") or (settings or get_settings()).redis.url


def create_redis(url: str | None = None, settings: Settings | None = None, *, blocking: bool = False) -> redis.Redis:
    """``blocking=True``: conexão para escuta da fila (BLPOP longo) — sem timeout de leitura.

    As demais operações (locks, cache, heartbeats) usam timeout curto para falhar rápido.
    """
    settings = settings or get_settings()
    url = url or redis_url(settings)
    if not url:
        raise RedisNotConfigured("REDIS_URL não configurada")
    timeout = settings.redis.socket_timeout_seconds
    return redis.Redis.from_url(url, socket_timeout=None if blocking else timeout, socket_connect_timeout=timeout,
                                socket_keepalive=True, health_check_interval=30, decode_responses=False)


@lru_cache(maxsize=4)
def shared_redis(url: str) -> redis.Redis:
    return create_redis(url)


def redis_status(client: redis.Redis | None) -> dict:
    """Estado para /health (nunca inclui a URL, que pode conter senha)."""
    if client is None:
        return {"status": "NOT_CONFIGURED"}
    try:
        client.ping()
        return {"status": "ok"}
    except redis.RedisError as exc:
        return {"status": "UNAVAILABLE", "error": type(exc).__name__}
