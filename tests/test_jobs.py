from collections import Counter
from datetime import timedelta

import pytest

from osintizada.core.enums import JobStatus
from osintizada.db.tables import utcnow
from osintizada.investigation.service import InvestigationRequest
from osintizada.jobs.recovery import JobRecoveryService
from osintizada.jobs.service import JobService
from osintizada.repositories import (
    CaseRepository,
    EntityRepository,
    EvidenceRepository,
    JobRepository,
    SearchRepository,
)
from tests.fixtures.jobs_env import JobsEnv

REQ = InvestigationRequest(inputs=["example.com"], mode="deep", max_depth=2)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for var in ("BRAVE_SEARCH_API_KEY", "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION"):
        monkeypatch.delenv(var, raising=False)
    return JobsEnv(tmp_path)


def counts(env, case_id):
    with env.db.session() as s:
        return (EntityRepository(s).count(case_id), len(EvidenceRepository(s).list(case_id)))


def job_row(env, job_id):
    with env.db.session() as s:
        return JobRepository(s).get(job_id)


def events(env, job_id):
    with env.db.session() as s:
        return [e.event_type for e in JobRepository(s).events_after(job_id, 0, 10_000)]


def test_submit_persists_then_enqueues(env):
    case_id = env.service.create_case("jobs")
    job = env.jobs.submit_investigation(case_id, REQ)
    assert job["status"] == "QUEUED" and job["dispatch"] == "QUEUED" and job["job_type"] == "INVESTIGATION"
    assert env.queue.size() == 1 and env.queue.exists(job["id"])
    assert events(env, job["id"]) == ["JOB_CREATED", "JOB_QUEUED"]
    with env.db.session() as s:
        assert CaseRepository(s).get(case_id).status == "RUNNING"  # derivado: há job ativo


def test_worker_executes_job_outside_api(env):
    case_id = env.service.create_case("worker")
    job = env.jobs.submit_investigation(case_id, REQ)
    env.run_worker()
    row = job_row(env, job["id"])
    assert row.status == "COMPLETED" and row.attempt == 1 and row.result["summary"]["depth_reached"] == 2
    kinds = Counter(events(env, job["id"]))
    for kind in ("JOB_STARTED", "PROVIDER_STARTED", "PROVIDER_FINISHED", "ENTITY_CREATED", "PIVOT_CREATED",
                 "JOB_CHECKPOINT", "JOB_PROGRESS", "JOB_COMPLETED"):
        assert kinds[kind] > 0, kind
    described = env.jobs.describe(job["id"])
    assert described["progress_percent"] == 100 and described["entities_found"] >= 8
    assert described["attempts"][0]["status"] == "COMPLETED"
    assert "execution_token" not in described
    with env.db.session() as s:
        assert CaseRepository(s).get(case_id).status == "COMPLETED"
        assert row.checkpoint["name"] == "CORRELATION_COMPLETED"


def test_duplicate_delivery_is_idempotent(env):
    case_id = env.service.create_case("dup")
    job = env.jobs.submit_investigation(case_id, REQ)
    env.queue.enqueue(job["id"])  # mesma mensagem entregue duas vezes
    env.run_worker()
    first = counts(env, case_id)
    assert job_row(env, job["id"]).attempt == 1  # a 2ª entrega foi ignorada (terminal)
    assert env.runner().run(job["id"]) == "skip:COMPLETED"
    # Reentrega após "crash antes do ack": mesmo trabalho de novo → nenhum dado duplicado.
    with env.db.session() as s:
        JobRepository(s).set_status(job["id"], [JobStatus.COMPLETED], JobStatus.QUEUED)
        JobRepository(s).get(job["id"]).checkpoint = {}
    assert env.runner().run(job["id"]) == "COMPLETED"
    assert counts(env, case_id) == first
    with env.db.session() as s:
        reused = [e for e in SearchRepository(s).list(case_id) if e.error_code == "ALREADY_EXECUTED"]
    assert reused  # a memória investigativa do banco evitou repetir consultas


def test_atomic_claim_only_one_winner(env):
    case_id = env.service.create_case("claim")
    job = env.jobs.submit_investigation(case_id, REQ)
    with env.db.session() as s:
        repo = JobRepository(s)
        assert repo.claim(job["id"], "worker-a", "a" * 32)
        assert not repo.claim(job["id"], "worker-b", "b" * 32)
        assert not repo.heartbeat(job["id"], "b" * 32)
        assert repo.heartbeat(job["id"], "a" * 32)
    assert env.runner().run(job["id"]) == "skip:running"


def test_queue_unavailable_keeps_job_pending_then_dispatches(env):
    class DownQueue:
        name = "down"

        def enqueue(self, job_id):
            raise ConnectionError("redis fora")

        def exists(self, job_id):
            return False

        def size(self):
            return 0

    case_id = env.service.create_case("outbox")
    job = JobService(env.db, env.settings, DownQueue(), None).submit_investigation(case_id, REQ)
    assert job["status"] == "PENDING" and job["dispatch"] == "QUEUE_UNAVAILABLE"
    env.settings.jobs.pending_dispatch_after_seconds = 0
    report = JobRecoveryService(env.db, env.settings, env.jobs, env.queue).reconcile()
    assert report["dispatched"] == 1 and job_row(env, job["id"]).status == "QUEUED"
    env.run_worker()
    assert job_row(env, job["id"]).status == "COMPLETED"


def test_job_retry_then_dead_letter_and_manual_retry(env, monkeypatch):
    env.settings.jobs.max_attempts = 2
    env.settings.jobs.retry_backoff_seconds = 0
    env.jobs.settings.jobs.max_attempts = 2
    case_id = env.service.create_case("fail")
    job = env.jobs.submit_investigation(case_id, REQ)

    async def boom(*args, **kwargs):
        raise RuntimeError("banco de dados indisponível no meio do pipeline")

    runner = env.runner()
    monkeypatch.setattr(runner.ctx.investigation, "run", boom)
    assert runner.run(job["id"]) == "RETRYING"
    row = job_row(env, job["id"])
    assert row.error_code == "JOB_ERROR" and row.next_attempt_at is not None
    JobRecoveryService(env.db, env.settings, env.jobs, env.queue).reconcile(now=utcnow() + timedelta(seconds=5))
    assert job_row(env, job["id"]).status == "QUEUED"
    assert runner.run(job["id"]) == "FAILED"
    row = job_row(env, job["id"])
    assert row.error_code == "MAX_ATTEMPTS_EXCEEDED" and "indisponível" in row.error_message
    with env.db.session() as s:
        attempts = JobRepository(s).attempts(job["id"])
        assert [a.status for a in attempts] == ["FAILED", "FAILED"]  # histórico preservado
        assert CaseRepository(s).get(case_id).status == "FAILED"

    monkeypatch.undo()
    retried = env.jobs.retry(job["id"])
    assert retried["retry_of"] == job["id"] and retried["id"] != job["id"] and retried["status"] == "QUEUED"
    env.run_worker()
    assert job_row(env, retried["id"]).status == "COMPLETED"
    assert job_row(env, job["id"]).status == "FAILED"  # original intacto
    with pytest.raises(RuntimeError):
        env.jobs.retry(retried["id"])  # só FAILED/CANCELLED/INTERRUPTED


def test_cancel_before_start_is_immediate(env):
    case_id = env.service.create_case("cancel")
    job = env.jobs.submit_investigation(case_id, REQ)
    cancelled = env.jobs.cancel(job["id"])
    assert cancelled["status"] == "CANCELLED" and cancelled["cancel_requested"] is True
    env.run_worker()  # a mensagem na fila é consumida e ignorada
    assert job_row(env, job["id"]).status == "CANCELLED" and counts(env, case_id) == (0, 0)
    with pytest.raises(RuntimeError):
        env.jobs.cancel(job["id"])
    with env.db.session() as s:
        assert CaseRepository(s).get(case_id).status == "OPEN"


def test_cooperative_cancel_while_running_preserves_collected_data(env, monkeypatch):
    case_id = env.service.create_case("coop")
    job = env.jobs.submit_investigation(case_id, REQ)
    runner = env.runner()
    original = runner.ctx.investigation._on_provider_start
    fired = []

    def cancel_on_first_provider(state, provider, ident, query):
        if not fired:
            fired.append(provider)
            env.jobs.cancel(job["id"])  # investigador cancela com o job RUNNING
        return original(state, provider, ident, query)

    monkeypatch.setattr(runner.ctx.investigation, "_on_provider_start", cancel_on_first_provider)
    assert runner.run(job["id"]) == "CANCELLED"
    row = job_row(env, job["id"])
    assert row.status == "CANCELLED" and row.error_code == "CANCELLED"
    with env.db.session() as s:
        executions = SearchRepository(s).list(case_id)
    assert executions and max(e.depth for e in executions) == 0  # parou na 1ª rodada, sem pivôs
    assert "JOB_CANCELLED" in events(env, job["id"])


def test_worker_crash_recovery_resumes_from_checkpoint(env, monkeypatch):
    """Worker morre após o 1º checkpoint → heartbeat expira → INTERRUPTED → reagendado →
    outro worker retoma do checkpoint sem repetir a rodada inicial nem duplicar dados."""
    case_id = env.service.create_case("crash")
    job = env.jobs.submit_investigation(case_id, REQ)

    class WorkerKilled(BaseException):
        pass

    from osintizada.jobs.control import JobExecutionControl

    original_checkpoint = JobExecutionControl.checkpoint

    def crash_after_initial_round(self, name, state):
        original_checkpoint(self, name, state)
        if name == "INITIAL_PROVIDERS_COMPLETED":
            raise WorkerKilled()  # processo morre: nada de except/finish

    monkeypatch.setattr(JobExecutionControl, "checkpoint", crash_after_initial_round)
    with pytest.raises(WorkerKilled):
        env.runner().run(job["id"])
    monkeypatch.undo()

    row = job_row(env, job["id"])
    assert row.status == "RUNNING" and row.checkpoint["name"] == "INITIAL_PROVIDERS_COMPLETED"
    with env.db.session() as s:
        depth0_calls = len([e for e in SearchRepository(s).list(case_id) if e.depth == 0])

    # Ainda com heartbeat recente: o reconciliador NÃO mexe.
    recovery = JobRecoveryService(env.db, env.settings, env.jobs, env.queue)
    assert recovery.reconcile()["interrupted"] == 0
    # Heartbeat expira.
    report = recovery.reconcile(now=utcnow() + timedelta(seconds=env.settings.jobs.stale_after_seconds + 5))
    assert report["interrupted"] == 1 and report["rescheduled"] == 1 and report["dispatched"] == 1
    assert job_row(env, job["id"]).status == "QUEUED"

    env.run_worker()
    row = job_row(env, job["id"])
    assert row.status == "COMPLETED" and row.attempt == 2
    with env.db.session() as s:
        attempts = [a.status for a in JobRepository(s).attempts(job["id"])]
        assert attempts == ["INTERRUPTED", "COMPLETED"]
        executions = SearchRepository(s).list(case_id)
        assert len([e for e in executions if e.depth == 0]) == depth0_calls  # rodada inicial não repetida
        assert CaseRepository(s).get(case_id).status == "COMPLETED"
        fingerprints = [e.fingerprint for e in EntityRepository(s).list(case_id)]
        assert len(fingerprints) == len(set(fingerprints))
    kinds = events(env, job["id"])
    assert "JOB_INTERRUPTED" in kinds and kinds.count("JOB_STARTED") == 2

    # Resultado final igual ao de uma execução limpa.
    clean_case = env.service.create_case("limpo")
    env.jobs.submit_investigation(clean_case, REQ)
    env.run_worker()
    with env.db.session() as s:
        crashed = {(e.type, e.canonical_value) for e in EntityRepository(s).list(case_id)}
        clean = {(e.type, e.canonical_value) for e in EntityRepository(s).list(clean_case)}
    assert crashed == clean


def test_stale_job_without_attempts_left_goes_to_dead_letter(env):
    env.settings.jobs.max_attempts = 1
    case_id = env.service.create_case("dead")
    job = env.jobs.submit_investigation(case_id, REQ)
    with env.db.session() as s:
        JobRepository(s).claim(job["id"], "worker-x", "x" * 32)
    report = JobRecoveryService(env.db, env.settings, env.jobs, env.queue).reconcile(
        now=utcnow() + timedelta(minutes=10))
    assert report["failed"] == 1
    row = job_row(env, job["id"])
    assert row.status == "FAILED" and row.error_code == "MAX_ATTEMPTS_EXCEEDED"


def test_case_lock_contention_defers_without_consuming_attempt(env):
    from osintizada.infrastructure.locks import case_lock

    case_id = env.service.create_case("lock")
    job = env.jobs.submit_investigation(case_id, REQ)
    other = case_lock(env.redis, case_id, 60)
    assert other.acquire()  # outro Deep Sweep segurando o Case
    assert env.runner().run(job["id"]) == "deferred:case-locked"
    row = job_row(env, job["id"])
    assert row.status == "RETRYING" and row.attempt == 0 and row.next_attempt_at is not None
    other.release()
    JobRecoveryService(env.db, env.settings, env.jobs, env.queue).reconcile(now=utcnow() + timedelta(seconds=30))
    env.run_worker()
    assert job_row(env, job["id"]).status == "COMPLETED"


def test_result_of_worker_that_lost_ownership_is_discarded(env):
    case_id = env.service.create_case("owner")
    job = env.jobs.submit_investigation(case_id, REQ)
    with env.db.session() as s:
        repo = JobRepository(s)
        repo.claim(job["id"], "old", "o" * 32)
    with env.db.session() as s:  # reconciliador interrompe e outro worker assume
        repo = JobRepository(s)
        repo.interrupt_if_stale(job["id"], repo.get(job["id"]).heartbeat_at)
        repo.claim(job["id"], "new", "n" * 32)
    with env.db.session() as s:
        assert not JobRepository(s).finish(job["id"], "o" * 32, JobStatus.COMPLETED, result={"stale": True})
        assert JobRepository(s).get(job["id"]).worker_id == "new"


def test_redis_restart_does_not_lose_persistent_state(env):
    case_id = env.service.create_case("redis-restart")
    job = env.jobs.submit_investigation(case_id, REQ)
    env.redis.flushall()  # Redis reiniciou sem persistência: mensagem perdida
    assert not env.queue.exists(job["id"]) and job_row(env, job["id"]).status == "QUEUED"
    env.settings.jobs.queued_redispatch_after_seconds = 0
    report = JobRecoveryService(env.db, env.settings, env.jobs, env.queue).reconcile()
    assert report["redispatched"] == 1
    env.run_worker()
    assert job_row(env, job["id"]).status == "COMPLETED"
    first = counts(env, case_id)
    env.redis.flushall()  # perder o Redis de novo não afeta dados concluídos
    assert counts(env, case_id) == first


def test_job_heartbeat_updates_and_detects_lost_ownership(env):
    import threading

    from osintizada.jobs.heartbeat import JobHeartbeat

    case_id = env.service.create_case("hb")
    job = env.jobs.submit_investigation(case_id, REQ)
    with env.db.session() as s:
        JobRepository(s).claim(job["id"], "w", "t" * 32)
        before = JobRepository(s).get(job["id"]).heartbeat_at
    lost = threading.Event()
    hb = JobHeartbeat(env.db, job["id"], "t" * 32, 0.05, lost)
    hb.beat()
    assert job_row(env, job["id"]).heartbeat_at >= before and not lost.is_set()
    with env.db.session() as s:
        JobRepository(s).get(job["id"]).execution_token = "z" * 32  # outro assumiu
    hb.beat()
    assert lost.is_set()


def test_case_lock_ttl_follows_stale_after_by_default():
    from osintizada.config import JobSettings

    # Padrão: o lock de um worker morto expira junto com o limite de abandono do heartbeat,
    # então a retomada não fica bloqueada além do necessário.
    cfg = JobSettings(stale_after_seconds=40, heartbeat_interval_seconds=5)
    assert cfg.effective_case_lock_ttl == 40
    # Nunca menor que dois heartbeats (o lock é renovado a cada heartbeat).
    assert JobSettings(stale_after_seconds=3, heartbeat_interval_seconds=5).effective_case_lock_ttl == 11
    # Valor explícito é respeitado.
    assert JobSettings(case_lock_ttl_seconds=200).effective_case_lock_ttl == 200
