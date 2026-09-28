"""JobRecoveryService — reconciliador de jobs.

Executado ao subir API/worker e periodicamente no worker (um reconciliador por vez, via lock):

  1. RUNNING com heartbeat expirado → INTERRUPTED (transição otimista: só se ninguém renovou);
     depois RETRYING (se ainda há tentativas) ou FAILED (dead letter). Nunca COMPLETED.
  2. RETRYING vencidos e PENDING não publicados → publica na fila (outbox).
  3. QUEUED cuja mensagem sumiu da fila (ex.: Redis reiniciado sem persistência) → republica.
     Entrega duplicada é segura: o claim é atômico e os dados são idempotentes.
  4. recalcula o status dos Cases afetados.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from osintizada.config import Settings
from osintizada.core.enums import AuditEvent, JobEventType, JobStatus
from osintizada.db import Database
from osintizada.db.tables import utcnow
from osintizada.infrastructure.queue import JobQueue
from osintizada.jobs.service import JobService
from osintizada.observability.metrics import metrics
from osintizada.repositories import AuditRepository, JobRepository

log = logging.getLogger("osintizada.recovery")


class JobRecoveryService:
    def __init__(self, db: Database, settings: Settings, jobs: JobService, queue: JobQueue | None = None,
                 redis=None) -> None:
        self.db, self.settings, self.jobs, self.queue, self.redis = db, settings, jobs, queue, redis
        self.cfg = settings.jobs

    def reconcile_with_lock(self) -> dict | None:
        if self.redis is None:
            return self.reconcile()
        from osintizada.infrastructure.locks import DistributedLock

        lock = DistributedLock(self.redis, "reconciler", ttl_seconds=max(30.0, self.cfg.reconcile_interval_seconds),
                               prefix=self.settings.cache.prefix)
        if not lock.acquire():
            return None  # outro processo está reconciliando
        try:
            return self.reconcile()
        finally:
            lock.release()

    def reconcile(self, now=None) -> dict:
        now = now or utcnow()
        report = {"interrupted": 0, "rescheduled": 0, "failed": 0, "dispatched": 0, "redispatched": 0}
        touched: set[str] = set()

        # 1. RUNNING abandonados
        stale_cut = now - timedelta(seconds=self.cfg.stale_after_seconds)
        with self.db.session() as s:
            stale = [(j.id, j.case_id, j.heartbeat_at, j.attempt, j.max_attempts, j.execution_token)
                     for j in JobRepository(s).stale_running(stale_cut)]
        for job_id, case_id, heartbeat, attempt, max_attempts, token in stale:
            with self.db.session() as s:
                repo = JobRepository(s)
                if not repo.interrupt_if_stale(job_id, heartbeat):
                    continue  # renovado nesse meio tempo: não interfere
                if token:
                    repo.close_attempt(job_id, token, JobStatus.INTERRUPTED, "HEARTBEAT_EXPIRED",
                                       "Heartbeat expirado (worker encerrado?)")
                repo.add_event(job_id, case_id, JobEventType.JOB_INTERRUPTED,
                               {"last_heartbeat": heartbeat.isoformat() if heartbeat else None})
                audit = AuditRepository(s)
                audit.log(case_id, AuditEvent.JOB_INTERRUPTED, "JobRecoveryService",
                          "Job RUNNING sem heartbeat marcado como INTERRUPTED", {"job_id": job_id})
                if attempt < max_attempts:
                    repo.set_status(job_id, [JobStatus.INTERRUPTED], JobStatus.RETRYING, next_attempt_at=now)
                    audit.log(case_id, AuditEvent.JOB_RECOVERED, "JobRecoveryService",
                              f"Job reagendado (tentativa {attempt + 1}/{max_attempts}); retoma do último checkpoint",
                              {"job_id": job_id})
                    report["rescheduled"] += 1
                else:
                    repo.set_status(job_id, [JobStatus.INTERRUPTED], JobStatus.FAILED, finished_at=now,
                                    error_code="MAX_ATTEMPTS_EXCEEDED",
                                    error_message="Interrompido e sem tentativas restantes (dead letter)")
                    repo.add_event(job_id, case_id, JobEventType.JOB_FAILED, {"error_code": "MAX_ATTEMPTS_EXCEEDED"})
                    audit.log(case_id, AuditEvent.JOB_FAILED, "JobRecoveryService",
                              "Job interrompido sem tentativas restantes (dead letter)", {"job_id": job_id})
                    report["failed"] += 1
            report["interrupted"] += 1
            metrics.inc("recovered_jobs")
            touched.add(case_id)

        # 2. PENDING não publicados e RETRYING vencidos
        with self.db.session() as s:
            due = [(j.id, j.case_id) for j in JobRepository(s).due_for_dispatch(now, self.cfg.pending_dispatch_after_seconds)]
        for job_id, case_id in due:
            if self.jobs.dispatch(job_id):
                report["dispatched"] += 1
                touched.add(case_id)

        # 3. QUEUED sem mensagem na fila
        if self.queue is not None:
            cut = now - timedelta(seconds=self.cfg.queued_redispatch_after_seconds)
            with self.db.session() as s:
                queued = [(j.id, j.case_id) for j in JobRepository(s).queued_older_than(cut)]
            for job_id, case_id in queued:
                try:
                    present = self.queue.exists(job_id)
                except Exception:  # noqa: BLE001 - Redis fora: tenta no próximo ciclo
                    continue
                if not present and self.jobs.dispatch(job_id):
                    report["redispatched"] += 1
                    touched.add(case_id)

        for case_id in touched:
            self.jobs.refresh_case_status(case_id)
        if any(report.values()):
            log.info("reconciliação", extra=report)
        return report
