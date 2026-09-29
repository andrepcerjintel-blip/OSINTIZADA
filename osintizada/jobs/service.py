"""JobService — camada de aplicação entre API e fila.

Garantia DB-commit + publicação (outbox simplificado):
  1. o Job é gravado como PENDING e COMMITADO no banco;
  2. só então é publicado na fila; confirmada a publicação → QUEUED;
  3. se a publicação falhar (Redis fora), o Job continua PENDING — o reconciliador
     (``JobRecoveryService``) republica PENDING antigos. Nenhum Job é perdido e nenhum é
     marcado como enfileirado sem estar.
A própria linha do Job funciona como registro de outbox: não há tabela extra.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from osintizada.config import Settings, get_settings
from osintizada.core.enums import (
    ACTIVE_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    AuditEvent,
    CaseStatus,
    JobEventType,
    JobStatus,
    JobType,
)
from osintizada.db import Database
from osintizada.db.tables import utcnow
from osintizada.infrastructure.queue import JobQueue
from osintizada.investigation.service import InvestigationRequest
from osintizada.jobs.control import cancel_key, progress_key
from osintizada.observability.metrics import metrics
from osintizada.repositories import AuditRepository, CaseRepository, JobRepository, row_to_dict

log = logging.getLogger("osintizada.jobs")
EXPORT_FORMATS = ("json", "csv", "html")


def derive_case_status(session, case_id: str) -> CaseStatus | None:
    """Status do Case derivado dos Jobs de investigação (nunca de uma flag solta).

    RUNNING   → há job de investigação ativo (PENDING/QUEUED/RUNNING/RETRYING);
    COMPLETED → o último job de investigação terminou com sucesso;
    FAILED    → o último falhou de forma não recuperável;
    OPEN      → existe, mas não executa (último cancelado/interrompido).
    ARCHIVED é preservado. Sem jobs, o status atual é mantido (execução direta via CLI).
    """
    cases, jobs = CaseRepository(session), JobRepository(session)
    case = cases.get(case_id)
    if case is None or case.status == CaseStatus.ARCHIVED.value:
        return None
    investigation_jobs = [j for j in jobs.list(case_id) if j.job_type == JobType.INVESTIGATION.value]
    if not investigation_jobs:
        return None
    if any(JobStatus(j.status) in ACTIVE_JOB_STATUSES for j in investigation_jobs):
        target = CaseStatus.RUNNING
    else:
        last = max(investigation_jobs, key=lambda j: (j.finished_at or j.created_at))
        target = {JobStatus.COMPLETED.value: CaseStatus.COMPLETED,
                  JobStatus.FAILED.value: CaseStatus.FAILED}.get(last.status, CaseStatus.OPEN)
    if case.status != target.value:
        cases.set_status(case, target)
    return target


class JobService:
    def __init__(self, db: Database, settings: Settings | None = None, queue: JobQueue | None = None,
                 redis=None) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.queue = queue
        self.redis = redis

    @property
    def prefix(self) -> str:
        return self.settings.cache.prefix

    # --- submissão ------------------------------------------------------------------------------

    def submit_investigation(self, case_id: str, request: InvestigationRequest) -> dict:
        return self._submit(case_id, JobType.INVESTIGATION, request.model_dump(mode="json"))

    def submit_export(self, case_id: str, fmt: str) -> dict:
        fmt = fmt.lower()
        if fmt not in EXPORT_FORMATS:
            raise ValueError(f"Formato não suportado: {fmt} (use {', '.join(EXPORT_FORMATS)})")
        return self._submit(case_id, JobType.EXPORT, {"format": fmt})

    def submit_ai(self, case_id: str, operation: str, params: dict | None = None) -> dict:
        """Análise de IA sobre o Case, como Job (não bloqueia a API). A IA interpreta; o Core decide."""
        from osintizada.ai.service import OPERATIONS

        if operation not in OPERATIONS:
            raise ValueError(f"Operação de IA desconhecida: {operation} (use {', '.join(OPERATIONS)})")
        allowed = {"labels", "target_language", "evidence_ids", "max_queries"}
        params = {k: v for k, v in (params or {}).items() if k in allowed}
        return self._submit(case_id, JobType.AI_ANALYSIS, {"operation": operation, "params": params})

    def _submit(self, case_id: str, job_type: JobType, request: dict) -> dict:
        with self.db.session() as s:
            case = CaseRepository(s).get(case_id)
            if case is None:
                raise LookupError(f"Case {case_id} não encontrado")
            if case.status == CaseStatus.ARCHIVED.value and job_type == JobType.INVESTIGATION:
                raise RuntimeError("Case arquivado não aceita novas investigações")
            job = JobRepository(s).create(case_id, job_type, request, self.settings.jobs.max_attempts)
            JobRepository(s).add_event(job.id, case_id, JobEventType.JOB_CREATED, {"job_type": job_type.value})
            AuditRepository(s).log(case_id, AuditEvent.JOB_CREATED, "JobService",
                                   f"Job {job_type.value} criado (PENDING)", {"job_id": job.id})
            job_id = job.id
        dispatched = self.dispatch(job_id)
        self.refresh_case_status(case_id)
        data = self.describe(job_id)
        data["dispatch"] = "QUEUED" if dispatched else "QUEUE_UNAVAILABLE"
        return data

    def dispatch(self, job_id: str) -> bool:
        """Publica na fila. Falhou → o Job permanece PENDING/RETRYING e será republicado."""
        if self.queue is None:
            return False
        try:
            self.queue.enqueue(job_id)
        except Exception as exc:  # noqa: BLE001 - Redis indisponível: nada é perdido (o banco guarda o Job)
            log.warning("falha ao publicar job na fila", extra={"job_id": job_id, "error": type(exc).__name__})
            metrics.inc("jobs_dispatch_failed")
            return False
        with self.db.session() as s:
            repo = JobRepository(s)
            if repo.mark_queued(job_id):
                job = repo.get(job_id)
                repo.add_event(job_id, job.case_id, JobEventType.JOB_QUEUED, {})
                AuditRepository(s).log(job.case_id, AuditEvent.JOB_QUEUED, "JobService", "Job publicado na fila",
                                       {"job_id": job_id, "queue": getattr(self.queue, "name", None)})
                metrics.inc("jobs_queued")
        return True

    # --- controle -----------------------------------------------------------------------------

    def cancel(self, job_id: str) -> dict:
        with self.db.session() as s:
            repo = JobRepository(s)
            job = repo.get(job_id)
            if job is None:
                raise LookupError(f"Job {job_id} não encontrado")
            if JobStatus(job.status) in TERMINAL_JOB_STATUSES:
                raise RuntimeError(f"Job já finalizado ({job.status})")
            repo.request_cancel(job_id)
            case_id = job.case_id
            immediate = repo.set_status(job_id, [JobStatus.PENDING, JobStatus.QUEUED, JobStatus.RETRYING,
                                                 JobStatus.INTERRUPTED], JobStatus.CANCELLED, finished_at=utcnow())
            if immediate:
                repo.add_event(job_id, case_id, JobEventType.JOB_CANCELLED, {"before_start": True})
            AuditRepository(s).log(case_id, AuditEvent.JOB_CANCELLED, "JobService",
                                   "Cancelamento " + ("aplicado" if immediate else "solicitado (cooperativo)"),
                                   {"job_id": job_id})
        if self.redis is not None:
            try:
                self.redis.set(cancel_key(self.prefix, job_id), "1", ex=86400)
            except Exception:  # noqa: BLE001 - o banco já registra o pedido
                pass
        self.refresh_case_status(case_id)
        return self.describe(job_id)

    def retry(self, job_id: str) -> dict:
        """Retry manual: NOVO Job ligado ao anterior (histórico preservado), retomando do checkpoint."""
        with self.db.session() as s:
            repo = JobRepository(s)
            job = repo.get(job_id)
            if job is None:
                raise LookupError(f"Job {job_id} não encontrado")
            if job.status not in (JobStatus.FAILED.value, JobStatus.CANCELLED.value, JobStatus.INTERRUPTED.value):
                raise RuntimeError(f"Só é possível reexecutar jobs FAILED/CANCELLED/INTERRUPTED (atual: {job.status})")
            new = repo.create(job.case_id, JobType(job.job_type), job.request, self.settings.jobs.max_attempts,
                              retry_of=job.id, checkpoint=dict(job.checkpoint or {}),
                              investigation_id=job.investigation_id)
            repo.add_event(new.id, job.case_id, JobEventType.JOB_CREATED, {"retry_of": job.id})
            AuditRepository(s).log(job.case_id, AuditEvent.JOB_RETRY_SCHEDULED, "JobService",
                                   f"Retry manual do job {job.id}", {"job_id": new.id, "retry_of": job.id})
            new_id, case_id = new.id, job.case_id
        self.dispatch(new_id)
        self.refresh_case_status(case_id)
        return self.describe(new_id)

    def refresh_case_status(self, case_id: str) -> CaseStatus | None:
        with self.db.session() as s:
            return derive_case_status(s, case_id)

    # --- leitura --------------------------------------------------------------------------------

    def describe(self, job_id: str) -> dict:
        with self.db.session() as s:
            repo = JobRepository(s)
            job = repo.get(job_id)
            if job is None:
                raise LookupError(f"Job {job_id} não encontrado")
            data = row_to_dict(job)
            attempts = [row_to_dict(a) for a in repo.attempts(job_id)]
        progress = dict(data.get("progress") or {})
        live = self._live_progress(job_id)
        if live and live.get("updated_at", "") >= progress.get("updated_at", ""):
            progress = live
        data.pop("execution_token", None)
        data.pop("checkpoint", None)  # detalhe interno (pode ser grande)
        data["attempts"] = attempts
        data["progress"] = progress
        data["stage"] = progress.get("stage") or data.get("stage")
        total, done = progress.get("providers_total"), progress.get("providers_completed")
        data.update({
            "progress_percent": progress.get("progress"),
            "providers_completed": done,
            "providers_pending": (total - done) if isinstance(total, int) and isinstance(done, int) else None,
            "entities_found": progress.get("entities_found"),
            "evidence_found": progress.get("evidence_found"),
            "pivots_processed": progress.get("pivots_processed"),
            "error": ({"code": data["error_code"], "message": data["error_message"]}
                      if data.get("error_code") else None),
        })
        return data

    def _live_progress(self, job_id: str) -> dict[str, Any] | None:
        if self.redis is None:
            return None
        try:
            raw = self.redis.get(progress_key(self.prefix, job_id))
            return json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001
            return None
