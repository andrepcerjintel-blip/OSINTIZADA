"""Lock distribuído com TTL, token de posse, renovação e liberação segura.

Baseado no ``redis.lock.Lock`` (scripts Lua atômicos): só quem detém o token consegue
renovar (``extend``) ou liberar (``release``) — nunca libera o lock de outro worker.
"""

from __future__ import annotations

import logging

import redis
from redis.exceptions import LockError, LockNotOwnedError

log = logging.getLogger("osintizada.locks")


class DistributedLock:
    def __init__(self, client: redis.Redis, name: str, ttl_seconds: float, prefix: str = "osintizada") -> None:
        self.name = f"{prefix}:lock:{name}"
        self.ttl = ttl_seconds
        self._lock = client.lock(self.name, timeout=ttl_seconds, thread_local=False)

    @property
    def token(self) -> str | None:
        token = self._lock.local.token
        return token.decode() if isinstance(token, bytes) else token

    def acquire(self, wait_seconds: float = 0) -> bool:
        return bool(self._lock.acquire(blocking=wait_seconds > 0, blocking_timeout=wait_seconds or None))

    def renew(self) -> bool:
        """Estende o TTL; False se o lock expirou ou pertence a outro dono."""
        try:
            return bool(self._lock.extend(self.ttl, replace_ttl=True))
        except (LockNotOwnedError, LockError):
            return False

    def release(self) -> bool:
        try:
            self._lock.release()
            return True
        except (LockNotOwnedError, LockError):
            log.warning("lock não pertencia mais a este processo; nada liberado", extra={"lock": self.name})
            return False

    def owned(self) -> bool:
        try:
            return bool(self._lock.owned())
        except redis.RedisError:
            return False


def case_lock(client: redis.Redis, case_id: str, ttl_seconds: float, prefix: str = "osintizada") -> DistributedLock:
    return DistributedLock(client, f"case:{case_id}", ttl_seconds, prefix)
