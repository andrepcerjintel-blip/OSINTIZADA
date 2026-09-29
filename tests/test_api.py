import json

import fakeredis
import pytest
from fastapi.testclient import TestClient

from osintizada.api import create_app
from osintizada.jobs.heartbeat import WorkerHeartbeat
from osintizada.jobs.service import JobService
from tests.fixtures.jobs_env import JobsEnv


@pytest.fixture
def env(tmp_path, monkeypatch):
    for var in ("BRAVE_SEARCH_API_KEY", "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION",
                "RINO_API_TOKEN", "OSINTIZADA_API_TOKEN", "GOOGLE_CSE_API_KEY", "GOOGLE_CSE_CX"):
        monkeypatch.delenv(var, raising=False)
    e = JobsEnv(tmp_path)
    e.settings.jobs.export_dir = str(tmp_path / "exports")
    return e


def api(env):
    return TestClient(create_app(env.service, env.jobs, env.redis, reconcile_on_startup=False))


def test_health_reports_database_redis_worker(env):
    client = api(env)
    body = client.get("/health").json()
    assert body["api"]["status"] == "ok" and body["redis"]["status"] == "ok"
    assert body["worker"]["status"] == "OFFLINE" and body["status"] == "degraded"  # sem worker: não finge
    assert body["queue"]["status"] == "ok" and "database" in body
    hb = WorkerHeartbeat(env.redis, "w-1", interval=10)
    hb.beat()
    body = client.get("/health").json()
    assert body["worker"]["status"] == "ONLINE" and body["worker"]["workers"][0]["worker_id"] == "w-1"
    assert "redis://" not in json.dumps(body)  # nunca expõe URL/segredo


def test_health_without_redis_is_honest(env):
    down = fakeredis.FakeServer()
    down.connected = False
    client = TestClient(create_app(env.service, JobService(env.db, env.settings, None, None),
                                   fakeredis.FakeRedis(server=down), reconcile_on_startup=False))
    body = client.get("/health").json()
    assert body["redis"]["status"] == "UNAVAILABLE" and body["queue"]["status"] == "QUEUE_UNAVAILABLE"
    assert body["worker"]["status"] in ("UNKNOWN", "OFFLINE")


def test_investigate_enqueues_and_returns_immediately(env):
    client = api(env)
    case_id = client.post("/cases", json={"name": "api"}).json()["id"]
    resp = client.post(f"/cases/{case_id}/investigate", json={"inputs": ["example.com"], "mode": "deep"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "QUEUED" and body["case_id"] == case_id and body["job_id"]
    assert "WORKER_UNAVAILABLE" in body["warnings"]  # nenhum worker ONLINE: avisado, não fingido
    assert client.get(f"/cases/{case_id}").json()["counts"]["entities"] == 0  # a API não executou nada
    assert client.get(f"/jobs/{body['job_id']}").json()["status"] == "QUEUED"


def test_queue_unavailable_job_stays_pending(env):
    client = TestClient(create_app(env.service, JobService(env.db, env.settings, None, None), None,
                                   reconcile_on_startup=False))
    case_id = client.post("/cases", json={"name": "sem fila"}).json()["id"]
    body = client.post(f"/cases/{case_id}/investigate", json={"inputs": ["example.com"]}).json()
    assert body["status"] == "PENDING" and "QUEUE_UNAVAILABLE" in body["warnings"]


def test_api_restart_does_not_affect_investigation(env):
    """API envia Job → API "morre" → worker processa → nova API lê o resultado do banco."""
    first = api(env)
    case_id = first.post("/cases", json={"name": "restart"}).json()["id"]
    job_id = first.post(f"/cases/{case_id}/investigate",
                        json={"inputs": ["example.com"], "mode": "deep", "max_depth": 2}).json()["job_id"]
    del first  # processo da API encerrado; o job está no Redis e no banco
    env.run_worker()
    second = api(env)  # API volta
    job = second.get(f"/jobs/{job_id}").json()
    assert job["status"] == "COMPLETED" and job["progress_percent"] == 100
    case = second.get(f"/cases/{case_id}").json()
    assert case["status"] == "COMPLETED" and case["counts"]["entities"] >= 8
    assert case["jobs"][0]["id"] == job_id and "checkpoint" not in case["jobs"][0]
    values = {(e["type"], e["canonical_value"]) for e in second.get(f"/cases/{case_id}/entities").json()}
    assert ("IP", "93.184.216.34") in values and ("ASN", "AS15133") in values
    asn = next(e for e in second.get(f"/cases/{case_id}/entities").json() if e["type"] == "ASN")
    path = second.get(f"/cases/{case_id}/entities/{asn['id']}").json()["investigative_path"]
    assert [p["value"] for p in path] == ["example.com", "93.184.216.34", "AS15133"]
    for route in ("evidence", "relationships", "searches", "audit", "pivots", "conflicts", "correlations"):
        assert second.get(f"/cases/{case_id}/{route}").status_code == 200


def test_job_status_fields_and_sse_events(env):
    client = api(env)
    case_id = client.post("/cases", json={"name": "sse"}).json()["id"]
    job_id = client.post(f"/cases/{case_id}/investigate", json={"inputs": ["example.com"], "mode": "quick"}
                         ).json()["job_id"]
    env.run_worker()
    job = client.get(f"/jobs/{job_id}").json()
    for field in ("status", "progress", "stage", "created_at", "started_at", "finished_at", "providers_completed",
                  "providers_pending", "entities_found", "evidence_found", "pivots_processed", "error", "attempts"):
        assert field in job, field
    assert job["stage"] == "COMPLETED" and job["error"] is None
    with client.stream("GET", f"/jobs/{job_id}/events", params={"poll": 0.05}) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        text = "".join(resp.iter_text())
    names = [line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("event: ")]
    for kind in ("JOB_CREATED", "JOB_QUEUED", "JOB_STARTED", "PROVIDER_STARTED", "PROVIDER_FINISHED",
                 "ENTITY_CREATED", "JOB_PROGRESS", "JOB_COMPLETED"):
        assert kind in names, kind
    assert names[-1] == "end"
    last_id = max(int(line[4:]) for line in text.splitlines() if line.startswith("id: "))
    with client.stream("GET", f"/jobs/{job_id}/events", headers={"Last-Event-ID": str(last_id)}) as resp:
        resumed = "".join(resp.iter_text())
    assert "event: JOB_STARTED" not in resumed and "event: end" in resumed  # retomada sem repetir eventos


def test_cancel_and_retry_endpoints(env):
    client = api(env)
    case_id = client.post("/cases", json={"name": "cancel"}).json()["id"]
    job_id = client.post(f"/cases/{case_id}/investigate", json={"inputs": ["example.com"]}).json()["job_id"]
    cancelled = client.post(f"/jobs/{job_id}/cancel").json()
    assert cancelled["status"] == "CANCELLED"
    assert client.post(f"/jobs/{job_id}/cancel").status_code == 409
    retried = client.post(f"/jobs/{job_id}/retry")
    assert retried.status_code == 202 and retried.json()["retry_of"] == job_id
    assert client.post("/jobs/nao-existe/retry").status_code == 404
    env.run_worker()
    assert client.get(f"/jobs/{retried.json()['id']}").json()["status"] == "COMPLETED"
    assert len(client.get(f"/cases/{case_id}/jobs").json()) == 2


def test_export_job_and_download(env):
    client = api(env)
    case_id = client.post("/cases", json={"name": "export"}).json()["id"]
    client.post(f"/cases/{case_id}/investigate", json={"inputs": ["example.com"], "mode": "quick"})
    env.run_worker()
    assert client.post(f"/cases/{case_id}/exports", json={"format": "pdf"}).status_code == 422
    job = client.post(f"/cases/{case_id}/exports", json={"format": "json"}).json()
    assert job["status"] == "QUEUED" and job["job_type"] == "EXPORT"
    assert client.get(f"/jobs/{job['job_id']}/download").status_code == 409  # ainda não concluído
    env.run_worker()
    download = client.get(f"/jobs/{job['job_id']}/download")
    assert download.status_code == 200 and download.json()["case"]["id"] == case_id
    assert client.get(f"/cases/{case_id}").json()["status"] == "COMPLETED"  # export não altera status do Case


def test_timeline_endpoint_and_flags(env):
    client = api(env)
    case_id = client.post("/cases", json={"name": "tl"}).json()["id"]
    client.post(f"/cases/{case_id}/investigate", json={"inputs": ["example.com"], "mode": "deep", "max_depth": 1})
    env.run_worker()
    events = client.get(f"/cases/{case_id}/timeline").json()
    assert events and [e["event_time"] for e in events] == sorted(e["event_time"] for e in events)
    assert all(e["provider"] == "infra.crtsh" for e in client.get(
        f"/cases/{case_id}/timeline", params={"provider": "infra.crtsh"}).json())
    assert client.get(f"/cases/{case_id}/timeline", params={"date_from": "não-é-data"}).status_code == 422
    entity = client.get(f"/cases/{case_id}/entities").json()[0]
    flagged = client.post(f"/cases/{case_id}/entities/{entity['id']}/flags",
                          json={"low_identity_value": True, "reason": "logo"}).json()
    assert flagged["metadata"]["flags"]["low_identity_value"]["reason"] == "logo"


def test_metrics_endpoint(env):
    client = api(env)
    case_id = client.post("/cases", json={"name": "m"}).json()["id"]
    client.post(f"/cases/{case_id}/investigate", json={"inputs": ["example.com"], "mode": "quick"})
    env.run_worker()
    text = client.get("/metrics").text
    for name in ('jobs{status="COMPLETED"} 1', 'jobs{status="RUNNING"} 0', "jobs_completed_total",
                 "jobs_queued_total", "workers_online", "job_duration_seconds_count",
                 "provider_latency_seconds", "provider_calls_total"):
        assert f"osintizada_{name}" in text, name
    # Exposição válida: cada família declarada uma única vez, sem nome repetido entre tipos.
    types = [line.split()[2] for line in text.splitlines() if line.startswith("# TYPE")]
    assert len(types) == len(set(types))


def test_providers_not_configured_show_missing_names_only(env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    client = api(env)
    providers = {p["name"]: p for p in client.get("/providers").json()}
    tg = providers["social.telegram"]
    assert tg["status"] == "NOT_CONFIGURED" and tg["missing"] == ["TELEGRAM_API_HASH", "TELEGRAM_SESSION"]
    assert "12345" not in json.dumps(tg)
    assert providers["search.brave"]["missing"] == ["BRAVE_SEARCH_API_KEY"]
    result = client.post("/providers/social.telegram/validate-credentials").json()
    assert result["status"] == "NOT_CONFIGURED" and result["valid"] is None
    assert client.post("/providers/infra.dns/validate-credentials").json()["status"] == "NOT_REQUIRED"
    assert client.post("/providers/nope/validate-credentials").status_code == 404


def test_api_token(env, monkeypatch):
    monkeypatch.setenv("RINO_API_TOKEN", "segredo-de-teste-123")
    client = api(env)
    assert client.get("/health").status_code == 200
    assert client.get("/cases").status_code == 401
    assert client.get("/metrics").status_code == 401
    assert client.get("/cases", headers={"Authorization": "Bearer segredo-de-teste-123"}).status_code == 200


def test_api_never_leaks_secrets(env, monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "BSA-super-secret-value-9876")
    text = api(env).get("/providers").text
    assert "BSA-super-secret-value-9876" not in text and "9876" in text


def test_validation_errors(env):
    client = api(env)
    case_id = client.post("/cases", json={"name": "v"}).json()["id"]
    assert client.post(f"/cases/{case_id}/investigate", json={"inputs": []}).status_code == 422
    assert client.post(f"/cases/{case_id}/investigate", json={"inputs": ["x"], "mode": "raw"}).status_code == 422
    assert client.post("/cases/nope/investigate", json={"inputs": ["example.com"]}).status_code == 404
    assert client.get("/jobs/nope").status_code == 404
