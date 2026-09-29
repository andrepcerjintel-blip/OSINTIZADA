"""Executor embutido: o próprio servidor (``rino serve``) executa os Jobs, sem Redis.

Para uso em uma máquina só (ex.: Windows sem Redis/WSL). O BANCO é a fila:

  * a API grava o Job e o marca ``QUEUED`` (a linha do Job é a mensagem — persistente);
  * uma thread do servidor pega o próximo Job ``QUEUED`` e o executa com o mesmo ``JobRunner``
    dos workers (claim atômico, tentativa, heartbeat no banco, checkpoints, retry, cancelamento);
  * o reconciliador roda periodicamente: Job ``RUNNING`` sem heartbeat (ex.: servidor encerrado no
    meio da execução) vira ``INTERRUPTED`` → ``RETRYING`` e é retomado do último checkpoint.

Limites (documentados): um Job por vez neste processo (os demais aguardam na fila do banco) e
execução no mesmo processo da API. Para vários workers/máquinas, use Redis + ``rino worker``.
"""

from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import select

from osintizada.core.enums import JobStatus
from osintizada.db import Database
from osintizada.db.tables import JobRow
from osintizada.infrastructure.queue import JobQueue

log = logging.getLogger("osintizada.embedded")

EXECUTOR_REDIS = "redis"
EXECUTOR_EMBEDDED = "embedded"


def resolve_executor(settings, redis_configured: bool) -> str:
    from osintizada.branding import env

    mode = (env("EXECUTOR") or settings.jobs.executor or "auto").strip().lower()
    if mode not in ("auto", EXECUTOR_REDIS, EXECUTOR_EMBEDDED):
        raise ValueError(f"executor inválido: {mode!r} (use auto, redis ou embedded)")
    if mode == "auto":
        return EXECUTOR_REDIS if redis_configured else EXECUTOR_EMBEDDED
    return mode


class DatabaseJobQueue(JobQueue):
    """Fila no próprio banco: publicar = acordar o executor (o Job já está persistido)."""

    name = "database"

    def __init__(self, db: Database) -> None:
        self.db = db
        self.wake = threading.Event()

    def enqueue(self, job_id: str) -> None:
        self.wake.set()

    def exists(self, job_id: str) -> bool:
        return True  # a linha QUEUED no banco É a mensagem: nunca "some" como numa fila externa

    def size(self) -> int:
        with self.db.session() as s:
            rows = s.scalars(select(JobRow.id).where(JobRow.status == JobStatus.QUEUED.value)).all()
        return len(rows)

    def next_job(self) -> str | None:
        """Próximo Job QUEUED (FIFO por publicação)."""
        with self.db.session() as s:
            return s.scalars(select(JobRow.id)
                             .where(JobRow.status == JobStatus.QUEUED.value, JobRow.cancel_requested.is_(False))
                             .order_by(JobRow.queued_at, JobRow.created_at).limit(1)).first()


class EmbeddedExecutor:
    def __init__(self, ctx, poll_seconds: float = 1.0, reconcile_seconds: float = 5.0) -> None:
        from osintizada.jobs.recovery import JobRecoveryService
        from osintizada.jobs.worker import JobRunner

        self.ctx = ctx
        self.queue: DatabaseJobQueue = ctx.queue
        self.runner = JobRunner(ctx)
        self.recovery = JobRecoveryService(ctx.db, ctx.settings, ctx.jobs, ctx.queue, None)
        self.poll_seconds = poll_seconds
        self.reconcile_seconds = reconcile_seconds
        self.current_job: str | None = None
        self.last_beat: float = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- ciclo de vida ---------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="rino-embedded-executor", daemon=True)
        self._thread.start()
        log.info("executor embutido iniciado (fila no banco)", extra={"worker_id": self.ctx.worker_id})

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self.queue.wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive() and self.current_job:
                # Job em andamento: o heartbeat para junto com o processo e o reconciliador o retoma
                # do último checkpoint na próxima subida. Nunca é marcado COMPLETED sem ter terminado.
                log.warning("servidor encerrando com job em andamento; será retomado na próxima execução",
                            extra={"job_id": self.current_job})

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        state = "ONLINE" if self.alive else "OFFLINE"
        return {"status": state, "mode": "EMBEDDED",
                "workers": [{"worker_id": self.ctx.worker_id, "status": state,
                             "age_seconds": round(time.monotonic() - self.last_beat, 1) if self.last_beat else None,
                             "current_job": self.current_job}]}

    # --- execução --------------------------------------------------------------------------------

    def run_once(self) -> str | None:
        """Executa no máximo um Job pronto. Retorna o id executado (ou None)."""
        job_id = self.queue.next_job()
        if job_id is None:
            return None
        self.current_job = job_id
        try:
            outcome = self.runner.run(job_id)
            log.info("job executado pelo executor embutido", extra={"job_id": job_id, "outcome": outcome})
        finally:
            self.current_job = None
        return job_id

    def reconcile(self) -> dict | None:
        try:
            return self.recovery.reconcile()
        except Exception:  # noqa: BLE001
            log.exception("falha no reconciliador embutido")
            return None

    def _loop(self) -> None:
        last_reconcile = 0.0
        while not self._stop.is_set():
            self.last_beat = time.monotonic()
            if time.monotonic() - last_reconcile >= self.reconcile_seconds:
                self.reconcile()  # recupera abandonados, publica PENDING/RETRYING vencidos
                last_reconcile = time.monotonic()
            try:
                ran = self.run_once()
            except Exception:  # noqa: BLE001 - o executor nunca morre por causa de um Job
                log.exception("falha inesperada no executor embutido")
                ran = None
            if ran is None:
                self.queue.wake.wait(self.poll_seconds)
                self.queue.wake.clear()


def build_embedded_executor(service, jobs, poll_seconds: float = 1.0) -> EmbeddedExecutor:
    """Monta o executor sobre os serviços da API (mesmo banco e orquestrador, sem Redis)."""
    from osintizada.jobs.worker import WorkerContext

    ctx = WorkerContext(db=service.db, settings=service.settings, redis=None, queue=jobs.queue,
                        investigation=service, jobs=jobs)
    return EmbeddedExecutor(ctx, poll_seconds=poll_seconds,
                            reconcile_seconds=min(5.0, service.settings.jobs.reconcile_interval_seconds))
