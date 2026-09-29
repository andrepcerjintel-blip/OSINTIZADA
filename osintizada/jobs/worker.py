"""Worker: executa Jobs fora do processo da API.

Fluxo de um Job (``JobRunner.run``):
  1. carrega o Job; terminal → ignora (entrega duplicada é inofensiva);
  2. CLAIM atômico no banco (status + novo token de execução); outro worker já tem → ignora;
  3. lock distribuído do Case (TTL + token + renovação); ocupado → devolve sem gastar tentativa;
  4. registra a tentativa (``job_attempts``) e inicia o heartbeat (banco + Redis + lock);
  5. executa o handler (investigação/export) com ``JobExecutionControl``;
  6. grava o resultado APENAS se ainda detém o token; falhas → RETRYING (backoff) ou
     FAILED (dead letter) quando excede ``max_attempts``;
  7. libera o lock e recalcula o status do Case.
Idempotência: fingerprints + unique constraints no banco + memória de execuções + checkpoints.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, ClassVar

from osintizada.config import Settings, get_settings
from osintizada.core.enums import AuditEvent, JobEventType, JobStatus, JobType
from osintizada.core.secrets import sanitize
from osintizada.db import Database
from osintizada.db.tables import utcnow
from osintizada.infrastructure.locks import case_lock
from osintizada.infrastructure.queue import JobQueue, RQJobQueue
from osintizada.investigation.control import InvestigationCancelled
from osintizada.investigation.service import InvestigationRequest, InvestigationService
from osintizada.jobs.control import JobExecutionControl
from osintizada.jobs.heartbeat import JobHeartbeat, WorkerHeartbeat
from osintizada.jobs.service import JobService
from osintizada.observability import job_context
from osintizada.observability.metrics import metrics
from osintizada.orchestration.source_orchestrator import SourceOrchestrator
from osintizada.repositories import AuditRepository, JobRepository

log = logging.getLogger("osintizada.worker")


@dataclass
class WorkerContext:
    db: Database
    settings: Settings
    redis: Any
    queue: JobQueue | None
    investigation: InvestigationService
    jobs: JobService
    worker_id: str = field(default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}")
    loop: asyncio.AbstractEventLoop = field(default_factory=asyncio.new_event_loop)
    worker_heartbeat: WorkerHeartbeat | None = None
    # Conexão exclusiva para escutar a fila (BLPOP bloqueante, sem timeout de leitura).
    queue_redis: Any = None
    # Roteador de IA (criado sob demanda na 1ª análise de IA; injetável em testes).
    ai_router: Any = None

    _current: ClassVar[WorkerContext | None] = None

    @classmethod
    def set(cls, ctx: WorkerContext) -> None:
        cls._current = ctx

    @classmethod
    def current(cls) -> WorkerContext:
        if cls._current is None:
            cls._current = build_worker_context()
        return cls._current


def build_worker_context(settings: Settings | None = None, redis=None, db: Database | None = None,
                         orchestrator: SourceOrchestrator | None = None) -> WorkerContext:
    from osintizada.infrastructure.redis_cache import build_runtime
    from osintizada.infrastructure.redis_client import create_redis

    settings = settings or get_settings()
    queue_redis = None
    if redis is None:
        redis = create_redis(settings=settings)
        queue_redis = create_redis(settings=settings, blocking=True)
    db = db or Database()
    metrics.bind_redis(redis, settings.cache.prefix)
    if orchestrator is None:
        runtime = build_runtime(settings, redis)
        orchestrator = SourceOrchestrator(settings=settings, runtime=runtime)
    queue = RQJobQueue(redis, settings.jobs.queue_name)
    investigation = InvestigationService(db, orchestrator, settings)
    return WorkerContext(db=db, settings=settings, redis=redis, queue=queue, investigation=investigation,
                         jobs=JobService(db, settings, queue, redis), queue_redis=queue_redis)


def execute_job(job_id: str) -> str:
    """Ponto de entrada chamado pelo RQ (argumento serializado em JSON: apenas o id)."""
    return JobRunner(WorkerContext.current()).run(job_id)


class JobRunner:
    def __init__(self, ctx: WorkerContext) -> None:
        self.ctx = ctx
        self.cfg = ctx.settings.jobs

    def run(self, job_id: str) -> str:
        ctx = self.ctx
        with ctx.db.session() as s:
            job = JobRepository(s).get(job_id)
            if job is None:
                return "missing"
            if JobStatus(job.status) in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                return f"skip:{job.status}"  # entrega duplicada após término
            if job.status == JobStatus.RUNNING.value:
                return "skip:running"  # entrega duplicada durante execução (outro worker tem a posse)
            case_id, job_type = job.case_id, JobType(job.job_type)

        token = uuid.uuid4().hex
        with ctx.db.session() as s:
            if not JobRepository(s).claim(job_id, ctx.worker_id, token):
                return "skip:not-claimable"

        lock = case_lock(ctx.redis, case_id, self.cfg.effective_case_lock_ttl, ctx.settings.cache.prefix) \
            if ctx.redis is not None and job_type == JobType.INVESTIGATION else None
        if lock is not None and not lock.acquire(wait_seconds=self.cfg.case_lock_wait_seconds):
            with ctx.db.session() as s:
                JobRepository(s).release_claim(job_id, token, utcnow() + timedelta(seconds=10))
            log.info("case ocupado por outro job; devolvido para nova tentativa", extra={"job_id": job_id})
            return "deferred:case-locked"

        with ctx.db.session() as s:
            repo = JobRepository(s)
            job = repo.get(job_id)
            attempt = job.attempt
            repo.add_attempt(job_id, attempt, ctx.worker_id, token)
            repo.add_event(job_id, case_id, JobEventType.JOB_STARTED, {"attempt": attempt, "worker_id": ctx.worker_id})
            AuditRepository(s).log(case_id, AuditEvent.JOB_STARTED, "Worker",
                                   f"Job {job_type.value} iniciado (tentativa {attempt}/{job.max_attempts})",
                                   {"job_id": job_id, "worker_id": ctx.worker_id})
            max_attempts = job.max_attempts
        ctx.jobs.refresh_case_status(case_id)

        control = JobExecutionControl(ctx.db, job_id, case_id, token, ctx.redis, ctx.settings.cache.prefix)
        heartbeat = JobHeartbeat(ctx.db, job_id, token, self.cfg.heartbeat_interval_seconds,
                                 control.lost_ownership, lock, ctx.redis, ctx.settings.cache.prefix)
        heartbeat.start()
        if ctx.worker_heartbeat is not None:
            ctx.worker_heartbeat.current_job = job_id
        started = utcnow()
        outcome = "unknown"
        try:
            with job_context(job_id, case_id):
                result = self._handle(job_type, job_id, case_id, control)
            outcome = self._finish(job_id, case_id, token, JobStatus.COMPLETED, result=result)
        except InvestigationCancelled:
            if control.lost_ownership.is_set() and not self._cancel_requested(job_id):
                # Outro worker/reconciliador assumiu: esta execução apenas encerra, sem gravar nada.
                self._close_attempt(job_id, token, JobStatus.INTERRUPTED, "OWNERSHIP_LOST",
                                    "Posse perdida durante a execução")
                outcome = "ownership-lost"
            else:
                outcome = self._finish(job_id, case_id, token, JobStatus.CANCELLED, error_code="CANCELLED",
                                       error_message="Cancelado pelo investigador")
        except Exception as exc:  # noqa: BLE001 - falha sistêmica do pipeline (≠ falha de provider)
            log.exception("job falhou", extra={"job_id": job_id})
            message = f"{type(exc).__name__}: {exc}"
            if attempt < max_attempts:
                delay = self.cfg.retry_backoff_seconds * (2 ** (attempt - 1))
                outcome = self._finish(job_id, case_id, token, JobStatus.RETRYING, error_code="JOB_ERROR",
                                       error_message=message, next_attempt_at=utcnow() + timedelta(seconds=delay))
            else:
                outcome = self._finish(job_id, case_id, token, JobStatus.FAILED, error_code="MAX_ATTEMPTS_EXCEEDED",
                                       error_message=message)
        finally:
            heartbeat.stop()
            if lock is not None:
                lock.release()
            if ctx.worker_heartbeat is not None:
                ctx.worker_heartbeat.current_job = None
            metrics.observe("job_duration_seconds", (utcnow() - started).total_seconds(), job_type=job_type.value)
            ctx.jobs.refresh_case_status(case_id)
        return outcome

    # --- handlers --------------------------------------------------------------------------------

    def _handle(self, job_type: JobType, job_id: str, case_id: str, control: JobExecutionControl) -> dict:
        if job_type == JobType.INVESTIGATION:
            return self._investigation(job_id, case_id, control)
        if job_type == JobType.EXPORT:
            from osintizada.exports.service import ExportService

            with self.ctx.db.session() as s:
                fmt = JobRepository(s).get(job_id).request.get("format", "json")
            control.progress("EXPORTING", progress=10)
            result = ExportService(self.ctx.db, self.ctx.settings).export_to_file(case_id, fmt, job_id)
            control.progress("EXPORTED", progress=100)
            return result
        if job_type == JobType.AI_ANALYSIS:
            return self._ai_analysis(job_id, case_id, control)
        raise ValueError(f"Tipo de job desconhecido: {job_type}")

    def _ai_analysis(self, job_id: str, case_id: str, control: JobExecutionControl) -> dict:
        from osintizada.ai.router import build_ai_router
        from osintizada.ai.service import AIService

        with self.ctx.db.session() as s:
            request = dict(JobRepository(s).get(job_id).request or {})
        if self.ctx.ai_router is None:
            # Cache de IA = o mesmo CacheBackend do runtime de providers (memória ou Redis compartilhado).
            cache = getattr(getattr(self.ctx.investigation.orchestrator, "runtime", None), "cache", None)
            self.ctx.ai_router = build_ai_router(self.ctx.settings, cache=cache)
        control.progress("AI_ANALYSIS", progress=10)
        service = AIService(self.ctx.db, self.ctx.ai_router, self.ctx.settings.ai.max_items)
        annotation = self.ctx.loop.run_until_complete(
            service.run_operation(case_id, request.get("operation"), job_id, **(request.get("params") or {})))
        control.progress("AI_COMPLETED", progress=100)
        # IA indisponível NÃO é falha do job (não gera retry): o resultado registra o motivo.
        return {"annotation_id": annotation["id"], "ai_status": annotation["status"],
                "provider": annotation["provider"], "model": annotation["model"]}

    def _investigation(self, job_id: str, case_id: str, control: JobExecutionControl) -> dict:
        service = self.ctx.investigation
        with self.ctx.db.session() as s:
            job = JobRepository(s).get(job_id)
            request = InvestigationRequest.model_validate(job.request)
            investigation_id = job.investigation_id
        if investigation_id is None:
            investigation_id = service.start(case_id, request, enforce_idle=False)
            with self.ctx.db.session() as s:
                JobRepository(s).get(job_id).investigation_id = investigation_id
        summary = self.ctx.loop.run_until_complete(service.run(case_id, investigation_id, request, control))
        return {"summary": summary, "investigation_id": investigation_id}

    # --- término ---------------------------------------------------------------------------------

    def _finish(self, job_id: str, case_id: str, token: str, status: JobStatus, **kwargs) -> str:
        with self.ctx.db.session() as s:
            repo = JobRepository(s)
            if not repo.finish(job_id, token, status, **kwargs):
                log.warning("resultado descartado: posse do job perdida", extra={"job_id": job_id})
                return "ownership-lost"
            attempt_status = JobStatus.FAILED if status == JobStatus.RETRYING else status
            repo.close_attempt(job_id, token, attempt_status, kwargs.get("error_code"), kwargs.get("error_message"))
            event = {JobStatus.COMPLETED: JobEventType.JOB_COMPLETED, JobStatus.CANCELLED: JobEventType.JOB_CANCELLED,
                     JobStatus.RETRYING: JobEventType.JOB_RETRY_SCHEDULED,
                     JobStatus.FAILED: JobEventType.JOB_FAILED}[status]
            repo.add_event(job_id, case_id, event, {"error_code": kwargs.get("error_code"),
                                                    "error": sanitize(kwargs.get("error_message") or "") or None})
            audit = {JobStatus.COMPLETED: AuditEvent.JOB_COMPLETED, JobStatus.CANCELLED: AuditEvent.JOB_CANCELLED,
                     JobStatus.RETRYING: AuditEvent.JOB_RETRY_SCHEDULED, JobStatus.FAILED: AuditEvent.JOB_FAILED}[status]
            message = {JobStatus.FAILED: "Job FAILED (dead letter: tentativas esgotadas)",
                       JobStatus.RETRYING: "Falha sistêmica; nova tentativa agendada"}.get(status, f"Job {status.value}")
            AuditRepository(s).log(case_id, audit, "Worker", message,
                                   {"job_id": job_id, "error_code": kwargs.get("error_code"),
                                    "dead_letter": status == JobStatus.FAILED or None})
        metrics.inc({JobStatus.COMPLETED: "jobs_completed", JobStatus.FAILED: "jobs_failed",
                     JobStatus.RETRYING: "jobs_retried", JobStatus.CANCELLED: "jobs_cancelled"}[status])
        return status.value

    def _close_attempt(self, job_id: str, token: str, status: JobStatus, code: str, message: str) -> None:
        with self.ctx.db.session() as s:
            JobRepository(s).close_attempt(job_id, token, status, code, message)

    def _cancel_requested(self, job_id: str) -> bool:
        with self.ctx.db.session() as s:
            job = JobRepository(s).get(job_id)
            return bool(job and job.cancel_requested)


# --- processo do worker ---------------------------------------------------------------------------


def run_worker(settings: Settings | None = None, burst: bool = False, ctx: WorkerContext | None = None) -> None:
    """Loop do worker: RQ SimpleWorker (sem fork: um event loop persistente por processo) +
    heartbeat do worker + reconciliador periódico (um por vez, via lock)."""
    from rq import SimpleWorker
    from rq.serializers import JSONSerializer

    from osintizada.jobs.recovery import JobRecoveryService

    ctx = ctx or build_worker_context(settings)
    WorkerContext.set(ctx)
    cfg = ctx.settings.jobs
    ctx.worker_heartbeat = WorkerHeartbeat(ctx.redis, ctx.worker_id, cfg.worker_heartbeat_interval_seconds,
                                           ctx.settings.cache.prefix)
    ctx.worker_heartbeat.start()
    recovery = JobRecoveryService(ctx.db, ctx.settings, ctx.jobs, ctx.queue, ctx.redis)
    stop = threading.Event()

    def maintenance() -> None:
        while not stop.is_set():
            try:
                recovery.reconcile_with_lock()
            except Exception:  # noqa: BLE001
                log.exception("falha no reconciliador")
            stop.wait(cfg.reconcile_interval_seconds)

    recovery.reconcile_with_lock()  # ao subir: republica PENDING, recupera RUNNING abandonados
    maintainer = threading.Thread(target=maintenance, name="reconciler", daemon=True)
    if not burst:
        maintainer.start()
    log.info("worker iniciado", extra={"worker_id": ctx.worker_id, "queue": cfg.queue_name})
    try:
        listen = ctx.queue_redis or ctx.redis
        from rq import Queue

        queue = Queue(ctx.queue.name, connection=listen, serializer=JSONSerializer)
        worker = SimpleWorker([queue], connection=listen, serializer=JSONSerializer, name=ctx.worker_id)
        worker.work(burst=burst, with_scheduler=False, logging_level="WARNING")
    finally:
        stop.set()
        ctx.worker_heartbeat.stop()
