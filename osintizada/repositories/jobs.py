"""JobRepository — estado persistente dos Jobs (fonte da verdade; a fila só transporta o id).

Toda transição sensível é CONDICIONAL (UPDATE … WHERE status/token = …) e usa o número de
linhas afetadas para decidir: dois workers nunca "ganham" o mesmo Job, e um worker que
perdeu a posse (token trocado pela recuperação) não consegue mais gravar resultado.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, update

from osintizada.core.enums import (
    ACTIVE_JOB_STATUSES,
    CLAIMABLE_JOB_STATUSES,
    JobEventType,
    JobStatus,
    JobType,
)
from osintizada.core.secrets import sanitize, sanitize_payload
from osintizada.db.tables import JobAttemptRow, JobEventRow, JobRow, utcnow
from osintizada.repositories.base import Repository


class JobRepository(Repository):
    # --- criação / leitura ----------------------------------------------------------------

    def create(self, case_id: str, job_type: JobType, request: dict, max_attempts: int,
               retry_of: str | None = None, checkpoint: dict | None = None,
               investigation_id: str | None = None) -> JobRow:
        row = JobRow(case_id=case_id, job_type=JobType(job_type).value, status=JobStatus.PENDING.value,
                     request=sanitize_payload(request), max_attempts=max_attempts, retry_of=retry_of,
                     checkpoint=checkpoint or {}, investigation_id=investigation_id, progress={})
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, job_id: str) -> JobRow | None:
        return self.session.get(JobRow, job_id)

    def list(self, case_id: str | None = None, status: JobStatus | None = None, limit: int = 200) -> list[JobRow]:
        stmt = select(JobRow)
        if case_id:
            stmt = stmt.where(JobRow.case_id == case_id)
        if status:
            stmt = stmt.where(JobRow.status == JobStatus(status).value)
        return list(self.session.scalars(stmt.order_by(JobRow.created_at.desc()).limit(limit)))

    def active_for_case(self, case_id: str, job_type: JobType | None = None) -> list[JobRow]:
        stmt = select(JobRow).where(JobRow.case_id == case_id,
                                    JobRow.status.in_([s.value for s in ACTIVE_JOB_STATUSES]))
        if job_type:
            stmt = stmt.where(JobRow.job_type == JobType(job_type).value)
        return list(self.session.scalars(stmt))

    def count_by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for status in self.session.scalars(select(JobRow.status)):
            counts[status] = counts.get(status, 0) + 1
        return counts

    # --- transições condicionais ------------------------------------------------------------

    def mark_queued(self, job_id: str) -> bool:
        res = self.session.execute(
            update(JobRow).where(JobRow.id == job_id,
                                 JobRow.status.in_([JobStatus.PENDING.value, JobStatus.RETRYING.value,
                                                    JobStatus.QUEUED.value]))
            .values(status=JobStatus.QUEUED.value, queued_at=utcnow(), next_attempt_at=None))
        return res.rowcount == 1

    def claim(self, job_id: str, worker_id: str, token: str) -> bool:
        """Posse exclusiva: só um worker passa daqui por tentativa (entrega duplicada é ignorada)."""
        now = utcnow()
        res = self.session.execute(
            update(JobRow)
            .where(JobRow.id == job_id, JobRow.status.in_([s.value for s in CLAIMABLE_JOB_STATUSES]),
                   JobRow.cancel_requested.is_(False))
            .values(status=JobStatus.RUNNING.value, worker_id=worker_id, execution_token=token,
                    attempt=JobRow.attempt + 1, started_at=now, heartbeat_at=now, finished_at=None,
                    next_attempt_at=None, error_code=None, error_message=None))
        return res.rowcount == 1

    def heartbeat(self, job_id: str, token: str) -> bool:
        """Renova o heartbeat; False = posse perdida (outro worker/recuperação assumiu)."""
        res = self.session.execute(
            update(JobRow).where(JobRow.id == job_id, JobRow.execution_token == token,
                                 JobRow.status == JobStatus.RUNNING.value)
            .values(heartbeat_at=utcnow()))
        return res.rowcount == 1

    def finish(self, job_id: str, token: str, status: JobStatus, *, result: dict | None = None,
               error_code: str | None = None, error_message: str | None = None,
               next_attempt_at: datetime | None = None) -> bool:
        values = {"status": JobStatus(status).value, "error_code": error_code,
                  "error_message": sanitize(error_message)[:4000] if error_message else None}
        if status == JobStatus.RETRYING:
            values["next_attempt_at"] = next_attempt_at
        else:
            values["finished_at"] = utcnow()
        if result is not None:
            values["result"] = sanitize_payload(result)
        res = self.session.execute(
            update(JobRow).where(JobRow.id == job_id, JobRow.execution_token == token,
                                 JobRow.status == JobStatus.RUNNING.value).values(**values))
        return res.rowcount == 1

    def release_claim(self, job_id: str, token: str, retry_at: datetime) -> bool:
        """Devolve o Job sem consumir tentativa (ex.: lock do Case ocupado)."""
        res = self.session.execute(
            update(JobRow).where(JobRow.id == job_id, JobRow.execution_token == token,
                                 JobRow.status == JobStatus.RUNNING.value)
            .values(status=JobStatus.RETRYING.value, attempt=JobRow.attempt - 1, next_attempt_at=retry_at,
                    execution_token=None, worker_id=None))
        return res.rowcount == 1

    def interrupt_if_stale(self, job_id: str, observed_heartbeat: datetime | None) -> bool:
        """RUNNING com heartbeat expirado → INTERRUPTED (otimista: só se ninguém renovou)."""
        res = self.session.execute(
            update(JobRow).where(JobRow.id == job_id, JobRow.status == JobStatus.RUNNING.value,
                                 JobRow.heartbeat_at == observed_heartbeat)
            .values(status=JobStatus.INTERRUPTED.value, execution_token=None, error_code="HEARTBEAT_EXPIRED",
                    error_message="Heartbeat expirado: worker provavelmente encerrado durante a execução"))
        return res.rowcount == 1

    def set_status(self, job_id: str, from_statuses: list[JobStatus], to: JobStatus, **values) -> bool:
        res = self.session.execute(
            update(JobRow).where(JobRow.id == job_id, JobRow.status.in_([s.value for s in from_statuses]))
            .values(status=JobStatus(to).value, **values))
        return res.rowcount == 1

    def request_cancel(self, job_id: str) -> None:
        self.session.execute(update(JobRow).where(JobRow.id == job_id).values(cancel_requested=True))

    def save_progress(self, job_id: str, token: str | None, stage: str | None, progress: dict) -> None:
        stmt = update(JobRow).where(JobRow.id == job_id)
        if token:
            stmt = stmt.where(JobRow.execution_token == token)
        self.session.execute(stmt.values(stage=stage, progress=progress))

    def save_checkpoint(self, job_id: str, token: str | None, checkpoint: dict) -> bool:
        stmt = update(JobRow).where(JobRow.id == job_id)
        if token:
            stmt = stmt.where(JobRow.execution_token == token)
        return self.session.execute(stmt.values(checkpoint=checkpoint)).rowcount == 1

    # --- consultas do reconciliador -----------------------------------------------------------

    def stale_running(self, older_than: datetime) -> list[JobRow]:
        return list(self.session.scalars(select(JobRow).where(
            JobRow.status == JobStatus.RUNNING.value, JobRow.heartbeat_at < older_than)))

    def interrupted(self) -> list[JobRow]:
        return list(self.session.scalars(select(JobRow).where(JobRow.status == JobStatus.INTERRUPTED.value)))

    def due_for_dispatch(self, now: datetime, pending_after: float) -> list[JobRow]:
        pending_cut = now - timedelta(seconds=pending_after)
        stmt = select(JobRow).where(
            (JobRow.status == JobStatus.PENDING.value) & (JobRow.created_at <= pending_cut)
            | (JobRow.status == JobStatus.RETRYING.value) & (JobRow.next_attempt_at <= now))
        return list(self.session.scalars(stmt))

    def queued_older_than(self, cut: datetime) -> list[JobRow]:
        return list(self.session.scalars(select(JobRow).where(
            JobRow.status == JobStatus.QUEUED.value, JobRow.queued_at <= cut)))

    # --- tentativas e eventos -------------------------------------------------------------------

    def add_attempt(self, job_id: str, attempt: int, worker_id: str, token: str) -> JobAttemptRow:
        row = JobAttemptRow(job_id=job_id, attempt=attempt, worker_id=worker_id, execution_token=token,
                            status=JobStatus.RUNNING.value)
        self.session.add(row)
        self.session.flush()
        return row

    def close_attempt(self, job_id: str, token: str, status: JobStatus, error_code: str | None = None,
                      error_message: str | None = None) -> None:
        self.session.execute(
            update(JobAttemptRow).where(JobAttemptRow.job_id == job_id, JobAttemptRow.execution_token == token,
                                        JobAttemptRow.status == JobStatus.RUNNING.value)
            .values(status=JobStatus(status).value, finished_at=utcnow(), error_code=error_code,
                    error_message=sanitize(error_message)[:4000] if error_message else None))

    def attempts(self, job_id: str) -> list[JobAttemptRow]:
        return list(self.session.scalars(select(JobAttemptRow).where(JobAttemptRow.job_id == job_id)
                                         .order_by(JobAttemptRow.attempt)))

    def add_event(self, job_id: str, case_id: str, event_type: JobEventType | str, data: dict | None = None) -> None:
        self.session.add(JobEventRow(job_id=job_id, case_id=case_id, event_type=str(event_type),
                                     data=sanitize_payload(data or {})))

    def events_after(self, job_id: str, after_id: int = 0, limit: int = 500) -> list[JobEventRow]:
        return list(self.session.scalars(select(JobEventRow).where(JobEventRow.job_id == job_id,
                                                                   JobEventRow.id > after_id)
                                         .order_by(JobEventRow.id).limit(limit)))
