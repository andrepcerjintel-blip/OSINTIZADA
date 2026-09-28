"""Implementação do ExecutionControl para jobs (banco + Redis).

* cancelamento: flag no banco (persistente) + chave Redis (sinal rápido);
* progresso: espelho transitório no Redis a cada atualização; banco em intervalos e a cada
  mudança de etapa (o banco é a fonte da verdade);
* eventos: tabela ``job_events`` (base do SSE);
* checkpoints: gravados no banco SOMENTE se o token de execução ainda for deste worker —
  perder a posse aciona o cancelamento cooperativo desta execução.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from osintizada.core.enums import JobEventType
from osintizada.db import Database
from osintizada.db.tables import utcnow
from osintizada.investigation.control import ExecutionControl
from osintizada.repositories import JobRepository

log = logging.getLogger("osintizada.jobs")


def cancel_key(prefix: str, job_id: str) -> str:
    return f"{prefix}:job:{job_id}:cancel"


def progress_key(prefix: str, job_id: str) -> str:
    return f"{prefix}:job:{job_id}:progress"


class JobExecutionControl(ExecutionControl):
    def __init__(self, db: Database, job_id: str, case_id: str, token: str, redis=None, prefix: str = "osintizada",
                 db_check_interval: float = 2.0) -> None:
        self.db = db
        self.job_id = job_id
        self.case_id = case_id
        self.token = token
        self.redis = redis
        self.prefix = prefix
        self.lost_ownership = threading.Event()
        self._cancel_seen = False
        self._db_check_interval = db_check_interval
        self._last_db_check = 0.0
        self._last_progress_save = 0.0
        self._stage: str | None = None

    # --- cancelamento -------------------------------------------------------------------------

    def is_cancelled(self) -> bool:
        if self._cancel_seen or self.lost_ownership.is_set():
            return True
        if self.redis is not None:
            try:
                if self.redis.exists(cancel_key(self.prefix, self.job_id)):
                    self._cancel_seen = True
                    return True
            except Exception:  # noqa: BLE001 - Redis fora: o banco continua valendo
                pass
        now = time.monotonic()
        if now - self._last_db_check >= self._db_check_interval:
            self._last_db_check = now
            with self.db.session() as s:
                job = JobRepository(s).get(self.job_id)
                if job is None or job.cancel_requested or job.execution_token != self.token:
                    self._cancel_seen = True
        return self._cancel_seen

    # --- eventos / progresso ------------------------------------------------------------------

    def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        with self.db.session() as s:
            JobRepository(s).add_event(self.job_id, self.case_id, event_type, data or {})

    def progress(self, stage: str, **fields: Any) -> None:
        payload = {"stage": stage, **fields, "updated_at": utcnow().isoformat()}
        if self.redis is not None:
            try:
                self.redis.set(progress_key(self.prefix, self.job_id), json.dumps(payload), ex=3600)
            except Exception:  # noqa: BLE001
                pass
        now = time.monotonic()
        stage_changed = stage != self._stage
        if stage_changed or now - self._last_progress_save >= 2.0:
            self._last_progress_save = now
            with self.db.session() as s:
                JobRepository(s).save_progress(self.job_id, self.token, stage, payload)
            if stage_changed:
                self._stage = stage
                self.emit(JobEventType.JOB_PROGRESS, payload)

    # --- checkpoints ----------------------------------------------------------------------------

    def checkpoint(self, name: str, state: dict[str, Any]) -> None:
        data = {"name": name, "saved_at": utcnow().isoformat(), **state}
        with self.db.session() as s:
            saved = JobRepository(s).save_checkpoint(self.job_id, self.token, data)
        if not saved:
            log.warning("posse do job perdida ao gravar checkpoint", extra={"job_id": self.job_id})
            self.lost_ownership.set()
            return
        self.emit(JobEventType.JOB_CHECKPOINT, {"name": name, "depth": state.get("depth"),
                                                "pending_identifiers": len(state.get("frontier") or [])})

    def load_checkpoint(self) -> dict[str, Any] | None:
        with self.db.session() as s:
            job = JobRepository(s).get(self.job_id)
            return dict(job.checkpoint) if job is not None and job.checkpoint else None
