"""Heartbeats: do Job (banco + Redis + renovação do lock do Case) e do Worker (Redis)."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time

from osintizada.db import Database
from osintizada.repositories import JobRepository

log = logging.getLogger("osintizada.jobs")


def worker_key(prefix: str, worker_id: str) -> str:
    return f"{prefix}:worker:{worker_id}"


class JobHeartbeat(threading.Thread):
    """Enquanto o Job roda: renova ``heartbeat_at`` (condicionado ao token) e o lock do Case.

    Falha em qualquer renovação = posse perdida → sinaliza cancelamento cooperativo.
    """

    def __init__(self, db: Database, job_id: str, token: str, interval: float, lost_ownership: threading.Event,
                 lock=None, redis=None, prefix: str = "osintizada") -> None:
        super().__init__(name=f"heartbeat-{job_id}", daemon=True)
        self.db, self.job_id, self.token, self.interval = db, job_id, token, interval
        self.lost_ownership, self.lock, self.redis, self.prefix = lost_ownership, lock, redis, prefix
        self._stop = threading.Event()
        self.beats = 0

    def beat(self) -> None:
        with self.db.session() as s:
            alive = JobRepository(s).heartbeat(self.job_id, self.token)
        if not alive:
            log.warning("heartbeat recusado: posse do job perdida", extra={"job_id": self.job_id})
            self.lost_ownership.set()
        if self.lock is not None and not self.lock.renew():
            log.warning("lock do case perdido", extra={"job_id": self.job_id})
            self.lost_ownership.set()
        if self.redis is not None:
            try:
                self.redis.set(f"{self.prefix}:job:{self.job_id}:heartbeat", str(time.time()),
                               ex=int(self.interval * 3) + 1)
            except Exception:  # noqa: BLE001
                pass
        self.beats += 1

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.beat()
            except Exception:  # noqa: BLE001 - heartbeat nunca derruba o job; o reconciliador decide
                log.exception("falha no heartbeat", extra={"job_id": self.job_id})

    def stop(self) -> None:
        self._stop.set()


class WorkerHeartbeat(threading.Thread):
    """Chave ``<prefix>:worker:<id>`` com TTL: base do status ONLINE/STALE/OFFLINE."""

    def __init__(self, redis, worker_id: str, interval: float, prefix: str = "osintizada") -> None:
        super().__init__(name=f"worker-heartbeat-{worker_id}", daemon=True)
        self.redis, self.worker_id, self.interval, self.prefix = redis, worker_id, interval, prefix
        self.current_job: str | None = None
        self._stop = threading.Event()

    def beat(self) -> None:
        payload = {"worker_id": self.worker_id, "hostname": socket.gethostname(), "pid": os.getpid(),
                   "ts": time.time(), "current_job": self.current_job, "interval": self.interval}
        self.redis.set(worker_key(self.prefix, self.worker_id), json.dumps(payload), ex=int(self.interval * 3) + 1)

    def run(self) -> None:
        while True:
            try:
                self.beat()
            except Exception:  # noqa: BLE001
                log.warning("falha ao publicar heartbeat do worker")
            if self._stop.wait(self.interval):
                break

    def stop(self) -> None:
        self._stop.set()
        try:
            self.redis.delete(worker_key(self.prefix, self.worker_id))
        except Exception:  # noqa: BLE001
            pass


def workers_status(redis, prefix: str = "osintizada") -> dict:
    """ONLINE (heartbeat recente), STALE (chave viva mas atrasada), OFFLINE/WORKER_UNAVAILABLE."""
    if redis is None:
        return {"status": "WORKER_UNAVAILABLE", "reason": "Redis não configurado", "workers": []}
    workers = []
    try:
        for key in redis.scan_iter(match=f"{prefix}:worker:*", count=200):
            raw = redis.get(key)
            if not raw:
                continue
            data = json.loads(raw)
            age = time.time() - float(data.get("ts", 0))
            state = "ONLINE" if age <= 2 * float(data.get("interval", 10)) else "STALE"
            workers.append({"worker_id": data.get("worker_id"), "status": state, "age_seconds": round(age, 1),
                            "current_job": data.get("current_job")})
    except Exception as exc:  # noqa: BLE001
        return {"status": "UNKNOWN", "reason": f"Redis indisponível ({type(exc).__name__})", "workers": []}
    if any(w["status"] == "ONLINE" for w in workers):
        overall = "ONLINE"
    elif workers:
        overall = "STALE"
    else:
        overall = "OFFLINE"
    return {"status": overall, "workers": workers}
