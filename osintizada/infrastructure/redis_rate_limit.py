"""Rate limit compartilhado entre processos (API + N workers) via Redis.

Sem isso, cada worker teria seu próprio token bucket e N workers consumiriam N× a cota de um
provider (ex.: Brave free = 1 req/s). O bucket vive no Redis e é atualizado por um script Lua
atômico que usa o relógio do PRÓPRIO Redis (``TIME``) — workers em máquinas diferentes, com
relógios divergentes, concordam sobre a mesma janela.

O cooldown pedido por um HTTP 429 (``Retry-After``) também é compartilhado: um worker que recebeu
429 faz todos pausarem aquele provider.

Redis indisponível → degrada para o limitador local (por processo), com aviso e métrica; a
investigação não falha por causa da coordenação.
"""

from __future__ import annotations

import logging
import time

from osintizada.observability.metrics import metrics
from osintizada.resilience.rate_limiter import RateLimiter

log = logging.getLogger("osintizada.ratelimit")

# KEYS[1] = bucket; ARGV = taxa (tokens/s), capacidade, ttl (ms).
# Retorna 0 se consumiu um token; senão, milissegundos até haver um token.
_TOKEN_BUCKET = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil or ts == nil then
  tokens = capacity
  ts = now
end
tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)
local wait = 0
if tokens >= 1 then
  tokens = tokens - 1
else
  wait = math.ceil((1 - tokens) / rate * 1000)
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[3]))
return wait
"""


class RedisRateLimiter(RateLimiter):
    """Mesmo contrato do ``RateLimiter`` (configure/acquire/set_cooldown/cooldown_remaining)."""

    def __init__(self, client, prefix: str = "osintizada", **kwargs) -> None:
        super().__init__(**kwargs)
        self.client = client
        self.prefix = prefix
        self._rates: dict[str, float] = {}
        self._script = client.register_script(_TOKEN_BUCKET)
        self._degraded = False

    def bucket_key(self, key: str) -> str:
        return f"{self.prefix}:ratelimit:{key}"

    def cooldown_key(self, key: str) -> str:
        return f"{self.prefix}:cooldown:{key}"

    def configure(self, key: str, rate_per_minute: float | None) -> None:
        super().configure(key, rate_per_minute)  # bucket local = fallback
        if rate_per_minute:
            self._rates[key] = float(rate_per_minute)

    def try_acquire(self, key: str) -> float:
        """Tenta consumir um token compartilhado. Retorna 0 (consumiu) ou segundos até o próximo."""
        rate_per_minute = self._rates[key]
        rate = rate_per_minute / 60.0
        capacity = float(max(1, int(rate_per_minute // 60) or 1))
        ttl_ms = int(max(60.0, capacity / rate * 2) * 1000)
        return int(self._script(keys=[self.bucket_key(key)], args=[rate, capacity, ttl_ms])) / 1000.0

    async def acquire(self, key: str) -> float:
        if key not in self._rates:
            return 0.0
        waited = 0.0
        while True:
            try:
                delay = self.try_acquire(key)
            except Exception as exc:  # noqa: BLE001 - Redis fora: limite local (por processo)
                self._warn_degraded(exc)
                return waited + await super().acquire(key)
            self._degraded = False
            if delay <= 0:
                return waited
            metrics.inc("rate_limit_waits", provider=key)
            await self._sleep(delay)
            waited += delay

    def set_cooldown(self, key: str, seconds: float) -> None:
        super().set_cooldown(key, seconds)
        ms = int(max(0.0, seconds) * 1000)
        if ms <= 0:
            return
        try:
            current = self.client.pttl(self.cooldown_key(key))
            if current is None or current < ms:  # nunca encurta um cooldown mais longo
                self.client.set(self.cooldown_key(key), str(time.time() + seconds), px=ms)
        except Exception as exc:  # noqa: BLE001
            self._warn_degraded(exc)

    def cooldown_remaining(self, key: str) -> float:
        local = super().cooldown_remaining(key)
        try:
            pttl = self.client.pttl(self.cooldown_key(key))
        except Exception as exc:  # noqa: BLE001
            self._warn_degraded(exc)
            return local
        shared = pttl / 1000.0 if pttl and pttl > 0 else 0.0
        return max(local, shared)

    def _warn_degraded(self, exc: Exception) -> None:
        metrics.inc("rate_limit_redis_error")
        if not self._degraded:
            log.warning("rate limit compartilhado indisponível; usando limite local por processo",
                        extra={"error": type(exc).__name__})
        self._degraded = True
