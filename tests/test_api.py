import pytest
from fastapi.testclient import TestClient

from osintizada.api import create_app
from osintizada.config import Settings
from tests.fixtures.environment import build_service


@pytest.fixture
def client(monkeypatch):
    for var in ("BRAVE_SEARCH_API_KEY", "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION",
                "OSINTIZADA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    service, _, _ = build_service(Settings())
    return TestClient(create_app(service))


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_case_crud_and_404(client):
    created = client.post("/cases", json={"name": "Caso API", "description": "d", "metadata": {"ref": "X-1"}})
    assert created.status_code == 201
    case = created.json()
    assert case["status"] == "OPEN" and case["metadata"] == {"ref": "X-1"}
    assert client.get(f"/cases/{case['id']}").json()["counts"]["entities"] == 0
    assert any(c["id"] == case["id"] for c in client.get("/cases").json())
    assert client.get("/cases/nao-existe").status_code == 404
    assert client.post("/cases", json={"name": ""}).status_code == 422


def test_investigate_end_to_end(client):
    case_id = client.post("/cases", json={"name": "example"}).json()["id"]
    resp = client.post(f"/cases/{case_id}/investigate", json={"inputs": ["example.com"], "mode": "deep",
                                                               "max_depth": 2})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "RUNNING" and body["case_id"] == case_id

    # TestClient executa o background antes de retornar: o case já terminou.
    case = client.get(f"/cases/{case_id}").json()
    assert case["status"] == "COMPLETED"
    assert case["investigations"][0]["summary"]["depth_reached"] == 2
    assert case["inputs"][0]["source_type"] == "USER_INPUT"

    entities = client.get(f"/cases/{case_id}/entities").json()
    values = {(e["type"], e["canonical_value"]) for e in entities}
    assert ("IP", "93.184.216.34") in values and ("ASN", "AS15133") in values
    assert all(e["evidence_count"] >= 1 for e in entities)
    ips = client.get(f"/cases/{case_id}/entities", params={"type": "IP"}).json()
    assert [e["canonical_value"] for e in ips] == ["93.184.216.34"]

    rels = client.get(f"/cases/{case_id}/relationships").json()
    assert rels and all(r["evidence_ids"] for r in rels)
    evidence = client.get(f"/cases/{case_id}/evidence").json()
    assert {e["provider"] for e in evidence} >= {"infra.dns", "infra.rdap", "infra.crtsh"}
    searches = client.get(f"/cases/{case_id}/searches").json()
    assert {s["status"] for s in searches} >= {"SUCCESS", "NOT_CONFIGURED"}
    audit = [a["event_type"] for a in client.get(f"/cases/{case_id}/audit").json()]
    assert audit[0] == "CASE_CREATED" and audit[-1] == "CASE_FINISHED"
    assert client.get(f"/cases/{case_id}/pivots").json()
    assert client.get(f"/cases/{case_id}/conflicts").json()[0]["status"] == "CONFLICTING_EVIDENCE"
    assert client.get(f"/cases/{case_id}/correlations").status_code == 200

    # "Como chegamos aqui?": seed → IP → ASN
    asn = next(e for e in entities if e["type"] == "ASN")
    path = client.get(f"/cases/{case_id}/entities/{asn['id']}").json()["investigative_path"]
    assert [p["value"] for p in path] == ["example.com", "93.184.216.34", "AS15133"]
    assert path[0]["origin"] == "SEED"


def test_investigate_validation(client):
    case_id = client.post("/cases", json={"name": "v"}).json()["id"]
    assert client.post(f"/cases/{case_id}/investigate", json={"inputs": []}).status_code == 422
    assert client.post(f"/cases/{case_id}/investigate", json={"inputs": ["x"], "mode": "raw"}).status_code == 422
    assert client.post("/cases/nope/investigate", json={"inputs": ["example.com"]}).status_code == 404


def test_providers_and_health(client):
    providers = {p["name"]: p for p in client.get("/providers").json()}
    assert providers["infra.dns"]["configured"] is True
    assert providers["search.brave"]["status"] == "NOT_CONFIGURED"
    assert providers["social.telegram"]["credentials"] == {
        "TELEGRAM_API_ID": "NOT CONFIGURED", "TELEGRAM_API_HASH": "NOT CONFIGURED", "TELEGRAM_SESSION": "NOT CONFIGURED"}
    assert providers["infra.rdap"]["concurrency"] == 4 and providers["infra.rdap"]["rate_limit_per_minute"] == 60
    health = {h["name"]: h for h in client.get("/providers/health").json()}
    assert health["search.brave"]["status"] == "NOT_CONFIGURED"
    assert health["infra.dns"]["status"] == "HEALTHY" and health["infra.dns"]["latency_ms"] is not None
    assert set(health["infra.rdap"]) >= {"name", "configured", "status", "last_error", "latency_ms"}


def test_api_token(monkeypatch):
    monkeypatch.setenv("OSINTIZADA_API_TOKEN", "segredo-de-teste-123")
    service, _, _ = build_service(Settings())
    client = TestClient(create_app(service))
    assert client.get("/health").status_code == 200  # liveness aberto
    assert client.get("/cases").status_code == 401
    assert client.get("/cases", headers={"Authorization": "Bearer errado"}).status_code == 401
    assert client.get("/cases", headers={"Authorization": "Bearer segredo-de-teste-123"}).status_code == 200


def test_api_never_leaks_secrets(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "BSA-super-secret-value-9876")
    monkeypatch.delenv("OSINTIZADA_API_TOKEN", raising=False)
    service, _, _ = build_service(Settings())
    client = TestClient(create_app(service))
    text = client.get("/providers").text
    assert "BSA-super-secret-value-9876" not in text and "9876" in text  # só a forma mascarada
