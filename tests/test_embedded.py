"""Executor embutido: `rino serve` executa os Jobs sem Redis (fila no banco)."""

import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from osintizada.api import create_app
from osintizada.config import Settings
from osintizada.db.tables import utcnow
from osintizada.investigation.service import InvestigationRequest
from osintizada.jobs.embedded import DatabaseJobQueue, build_embedded_executor, resolve_executor
from osintizada.jobs.service import JobService
from osintizada.repositories import EntityRepository, JobRepository, SearchRepository
from tests.fixtures.jobs_env import JobsEnv

REQ = InvestigationRequest(inputs=["example.com"], mode="deep", max_depth=2)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for var in ("BRAVE_SEARCH_API_KEY", "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION",
                "RINO_API_TOKEN", "OSINTIZADA_API_TOKEN", "GOOGLE_CSE_API_KEY", "GOOGLE_CSE_CX", "RINO_EXECUTOR"):
        monkeypatch.delenv(var, raising=False)
    e = JobsEnv(tmp_path)
    e.settings.jobs.export_dir = str(tmp_path / "exports")
    # Sem Redis: fila no banco.
    e.jobs = JobService(e.db, e.settings, DatabaseJobQueue(e.db), None)
    return e


def wait_status(client, job_id, statuses=("COMPLETED", "FAILED", "CANCELLED"), timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/jobs/{job_id}").json()
        if body["status"] in statuses:
            return body
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} não terminou: {body['status']}")


def test_server_runs_investigation_without_redis(env):
    app = create_app(env.service, env.jobs, None)
    with TestClient(app) as client:  # `with`: dispara o startup (inicia o executor)
        health = client.get("/health").json()
        assert health["worker"]["status"] == "ONLINE" and health["worker"]["mode"] == "EMBEDDED"
        assert health["queue"]["backend"] == "database" and health["redis"]["status"] == "NOT_CONFIGURED"
        # O banco de teste é criado sem Alembic (MIGRATIONS_PENDING); com banco migrado o status seria "ok".
        assert health["status"] == ("ok" if health["database"]["status"] == "ok" else "degraded")

        case_id = client.post("/cases", json={"name": "servidor"}).json()["id"]
        sub = client.post(f"/cases/{case_id}/investigate", json=REQ.model_dump(mode="json")).json()
        assert sub["status"] == "QUEUED" and sub["warnings"] == []  # nada de QUEUE/WORKER_UNAVAILABLE

        job = wait_status(client, sub["job_id"])
        assert job["status"] == "COMPLETED" and job["attempt"] == 1
        entities = client.get(f"/cases/{case_id}/entities").json()
        assert any(e["canonical_value"] == "example.com" for e in entities)
        assert client.get(f"/cases/{case_id}").json()["status"] == "COMPLETED"

        # Export também roda no servidor.
        exp = client.post(f"/cases/{case_id}/exports", json={"format": "html"}).json()
        assert wait_status(client, exp["job_id"])["status"] == "COMPLETED"
        report = client.get(f"/jobs/{exp['job_id']}/download")
        assert report.status_code == 200 and "RINO" in report.text


def test_jobs_queue_in_order_one_at_a_time(env):
    executor = build_embedded_executor(env.service, env.jobs)
    a = env.jobs.submit_investigation(env.service.create_case("a"), REQ)
    b = env.jobs.submit_investigation(env.service.create_case("b"), REQ)
    assert a["status"] == b["status"] == "QUEUED"
    assert executor.run_once() == a["id"]  # FIFO
    assert executor.run_once() == b["id"]
    assert executor.run_once() is None
    with env.db.session() as s:
        assert {JobRepository(s).get(j["id"]).status for j in (a, b)} == {"COMPLETED"}


def test_server_restart_resumes_interrupted_job_from_checkpoint(env, monkeypatch):
    """Servidor encerrado no meio do Job → na próxima subida o reconciliador o retoma do checkpoint."""
    from osintizada.jobs.control import JobExecutionControl

    case_id = env.service.create_case("queda")
    job = env.jobs.submit_investigation(case_id, REQ)

    class ServerKilled(BaseException):
        pass

    original = JobExecutionControl.checkpoint

    def die_after_initial(self, name, state):
        original(self, name, state)
        if name == "INITIAL_PROVIDERS_COMPLETED":
            raise ServerKilled()

    monkeypatch.setattr(JobExecutionControl, "checkpoint", die_after_initial)
    with pytest.raises(ServerKilled):
        build_embedded_executor(env.service, env.jobs).run_once()
    monkeypatch.undo()
    with env.db.session() as s:
        assert JobRepository(s).get(job["id"]).status == "RUNNING"
        depth0 = len([e for e in SearchRepository(s).list(case_id) if e.depth == 0])

    # "Nova subida": outro executor, heartbeat do anterior já expirado.
    executor = build_embedded_executor(env.service, env.jobs)
    report = executor.recovery.reconcile(now=utcnow() + timedelta(seconds=env.settings.jobs.stale_after_seconds + 5))
    assert report["interrupted"] == 1 and report["rescheduled"] == 1
    assert executor.run_once() == job["id"]
    with env.db.session() as s:
        row = JobRepository(s).get(job["id"])
        assert row.status == "COMPLETED" and row.attempt == 2
        assert [a.status for a in JobRepository(s).attempts(job["id"])] == ["INTERRUPTED", "COMPLETED"]
        assert len([e for e in SearchRepository(s).list(case_id) if e.depth == 0]) == depth0  # não repetiu
        fps = [e.fingerprint for e in EntityRepository(s).list(case_id)]
        assert len(fps) == len(set(fps))


def test_cancel_queued_job_is_never_executed(env):
    executor = build_embedded_executor(env.service, env.jobs)
    job = env.jobs.submit_investigation(env.service.create_case("c"), REQ)
    env.jobs.cancel(job["id"])
    assert executor.run_once() is None
    with env.db.session() as s:
        assert JobRepository(s).get(job["id"]).status == "CANCELLED"


def test_resolve_executor(monkeypatch):
    monkeypatch.delenv("RINO_EXECUTOR", raising=False)
    s = Settings()
    assert resolve_executor(s, redis_configured=False) == "embedded"   # Windows sem Redis
    assert resolve_executor(s, redis_configured=True) == "redis"       # produção: workers separados
    monkeypatch.setenv("RINO_EXECUTOR", "embedded")
    assert resolve_executor(s, redis_configured=True) == "embedded"
    monkeypatch.setenv("RINO_EXECUTOR", "qualquer")
    with pytest.raises(ValueError):
        resolve_executor(s, redis_configured=False)


def test_executor_off_when_disabled(env):
    app = create_app(env.service, env.jobs, None, embedded_executor=False)
    with TestClient(app) as client:
        assert client.get("/health").json()["worker"]["status"] == "WORKER_UNAVAILABLE"
