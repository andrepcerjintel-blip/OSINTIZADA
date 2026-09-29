"""Ambiente de jobs: banco SQLite + Redis simulado (fakeredis) + RQ real + providers reais com rede simulada.

RINO_TEST_DATABASE_URL=postgresql+psycopg://… roda os mesmos testes contra PostgreSQL real
(o schema é recriado a cada teste — use um banco descartável).
"""

from __future__ import annotations

import fakeredis

from osintizada.branding import env as branding_env
from osintizada.config import Settings
from osintizada.infrastructure.queue import RQJobQueue
from osintizada.jobs.service import JobService
from osintizada.jobs.worker import (
    JobRunner,
    WorkerContext,
    build_worker_context,
    run_worker,
)
from tests.fixtures.environment import build_service


class JobsEnv:
    def __init__(self, tmp_path, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.settings.jobs.heartbeat_interval_seconds = 0.2
        self.settings.jobs.worker_heartbeat_interval_seconds = 0.2
        self.settings.jobs.case_lock_wait_seconds = 0.2
        self.server = fakeredis.FakeServer()
        self.redis = fakeredis.FakeRedis(server=self.server)
        url = branding_env("TEST_DATABASE_URL")
        if url:
            from osintizada.db import Database
            from osintizada.db.tables import Base

            Base.metadata.drop_all(Database(url).engine)
        self.service, self.db, self.runtime = build_service(self.settings, url or f"sqlite:///{tmp_path}/jobs.db")
        self.queue = RQJobQueue(self.redis, self.settings.jobs.queue_name)
        self.jobs = JobService(self.db, self.settings, self.queue, self.redis)

    def worker_context(self) -> WorkerContext:
        ctx = build_worker_context(self.settings, redis=fakeredis.FakeRedis(server=self.server), db=self.db,
                                   orchestrator=self.service.orchestrator)
        WorkerContext.set(ctx)
        return ctx

    def run_worker(self) -> WorkerContext:
        """Processa a fila até esvaziar (worker em modo burst)."""
        ctx = self.worker_context()
        run_worker(ctx=ctx, burst=True)
        return ctx

    def runner(self) -> JobRunner:
        return JobRunner(self.worker_context())
