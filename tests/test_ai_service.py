"""Análises de IA sobre um Case: proveniência, DERIVED, revisão humana, triagem, map-reduce e API."""

import asyncio
import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from osintizada.ai.providers.claude import ClaudeProvider
from osintizada.ai.providers.llama import LlamaProvider
from osintizada.ai.router import AIRouter
from osintizada.ai.service import AI_DERIVED_CONFIDENCE, AIService
from osintizada.api import create_app
from osintizada.config import AISettings
from osintizada.core.enums import EntityType
from osintizada.exports.service import ExportService
from osintizada.investigation.service import InvestigationRequest
from osintizada.jobs.embedded import DatabaseJobQueue, build_embedded_executor
from osintizada.jobs.service import JobService
from osintizada.repositories import (
    AIAnnotationRepository,
    AuditRepository,
    CaseRepository,
    EntityRepository,
    EvidenceRepository,
    JobRepository,
    RelationshipRepository,
    SearchRepository,
)
from osintizada.resilience import MemoryCacheBackend
from tests.fixtures.ai import FakeOllama
from tests.fixtures.jobs_env import JobsEnv


@pytest.fixture
def env(tmp_path, monkeypatch):
    for var in ("BRAVE_SEARCH_API_KEY", "TELEGRAM_API_ID", "RINO_API_TOKEN", "OSINTIZADA_API_TOKEN",
                "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "RINO_AI_MODE", "RINO_EXECUTOR"):
        monkeypatch.delenv(var, raising=False)
    e = JobsEnv(tmp_path)
    e.settings.jobs.export_dir = str(tmp_path / "exports")
    e.jobs = JobService(e.db, e.settings, DatabaseJobQueue(e.db), None)
    return e


def local_router(fake: FakeOllama, **cfg_extra) -> AIRouter:
    cfg = AISettings()
    cfg.llama.enabled, cfg.llama.model = True, "llama3.1:8b"
    for k, v in cfg_extra.items():
        setattr(cfg.llama if hasattr(cfg.llama, k) else cfg, k, v)
    return AIRouter(cfg, [LlamaProvider(cfg.llama, transport=fake.transport), ClaudeProvider(cfg.claude)],
                    cache=MemoryCacheBackend())


def investigated_case(env) -> str:
    case_id = env.service.create_case("ia", "mapear infraestrutura de example.com")
    job = env.jobs.submit_investigation(case_id, InvestigationRequest(inputs=["example.com"], mode="deep", max_depth=1))
    assert build_embedded_executor(env.service, env.jobs).run_once() == job["id"]
    with env.db.session() as s:
        assert JobRepository(s).get(job["id"]).status == "COMPLETED"   # base real para a IA
    return case_id


def page_case(env, texts: list[str]) -> str:
    """Case com evidências de página (texto coletado) — sem rede."""
    case_id = env.service.create_case("paginas", "achar contatos")
    with env.db.session() as s:
        for n, text in enumerate(texts):
            ent, _ = EntityRepository(s).upsert(case_id, EntityType.URL, f"https://example.com/p{n}")
            EvidenceRepository(s).add(
                case_id=case_id, entity_id=ent.id, provider="search.brave", source_type="SEARCH_ENGINE", query="q",
                normalized_value=ent.canonical_value, confidence=0.6, collected_at=datetime.now(timezone.utc),
                content_hash=f"h{n}", fingerprint=f"fp{n}", raw_data={"title": f"Página {n}", "snippet": text})
    return case_id


def snapshot(env, case_id):
    with env.db.session() as s:
        return (len(EntityRepository(s).list(case_id)), len(RelationshipRepository(s).list(case_id)),
                len(SearchRepository(s).list(case_id)),
                sorted((e.id, e.confidence, json.dumps(e.raw_data, sort_keys=True))
                       for e in EvidenceRepository(s).list(case_id)))


def run(coro):
    return asyncio.run(coro)


def test_summary_provenance_and_invented_ids_dropped(env):
    case_id = investigated_case(env)
    before = snapshot(env, case_id)
    ann = run(AIService(env.db, local_router(FakeOllama())).run_operation(case_id, "summary"))
    assert ann["status"] == "OK" and ann["provider"] == "ai.llama" and ann["model"] == "llama3.1:8b"
    assert ann["prompt_version"] == "summarization_v1" and ann["classification"] == "DERIVED"
    assert len(ann["output_hash"]) == 64 and ann["usage"]["cost_external_usd"] == 0.0
    with env.db.session() as s:
        evidence_ids = {e.id for e in EvidenceRepository(s).list(case_id)}
        events = [a.event_type for a in AuditRepository(s).list(case_id)]
    assert set(ann["input_refs"]["evidence_ids"]) <= evidence_ids
    assert ann["output"]["validation"]["dropped_ids"] == ["f" * 32]
    assert snapshot(env, case_id) == before                    # resumo não altera nada no Core
    assert "AI_ANALYSIS_STARTED" in events and "AI_ANALYSIS_COMPLETED" in events


def test_map_reduce_for_large_content(env):
    long = "conteúdo " * 400
    case_id = page_case(env, [long, long + "b", long + "c"])
    fake = FakeOllama()
    ann = run(AIService(env.db, local_router(fake, max_input_chars=5_000)).run_operation(case_id, "summary"))
    assert ann["status"] == "OK" and ann["output"]["map_reduce"]["chunks"] > 1
    assert fake.chat_calls == ann["output"]["map_reduce"]["chunks"] + 1       # parciais + síntese final


def test_extraction_creates_derived_entities_linked_to_original_evidence(env):
    case_id = page_case(env, ["Fale com contato@example.com — responsável Maria Silva"])
    with env.db.session() as s:
        evidence = EvidenceRepository(s).list(case_id)[0]
        ev_id, page_entity = evidence.id, evidence.entity_id
        n_evidence = len(EvidenceRepository(s).list(case_id))
    ann = run(AIService(env.db, local_router(FakeOllama())).run_operation(case_id, "extract"))
    kept = {c["value"]: c for c in ann["output"]["candidates"]}
    assert set(kept) == {"contato@example.com", "Maria Silva"}
    reasons = {d["value"]: d["reason"] for d in ann["output"]["discarded"]}
    assert "literalmente" in reasons["inventado@naoexiste.com"]
    assert "validador determinístico" in reasons["contato"]               # PHONE "contato" rejeitado
    with env.db.session() as s:
        ents = {e.canonical_value: e for e in EntityRepository(s).list(case_id)}
        email = ents["contato@example.com"]
        assert email.origin == "DERIVED" and email.confidence == AI_DERIVED_CONFIDENCE
        ai = email.meta["ai"]
        assert ai["suggested"] and not ai["reviewed"] and ai["source_evidence_id"] == ev_id
        assert ai["provider"] == "ai.llama" and ai["model"] == "llama3.1:8b" and ai["ai_confidence"] == 0.9
        rel = [r for r in RelationshipRepository(s).list(case_id) if r.source_entity_id == email.id][0]
        assert rel.relationship_type == "DERIVED_FROM" and rel.target_entity_id == page_entity
        assert RelationshipRepository(s).evidence_ids(rel.id) == [ev_id]       # prova = evidência ORIGINAL
        evidence_after = EvidenceRepository(s).list(case_id)
        assert len(evidence_after) == n_evidence                               # nenhuma evidência "Llama"
        assert all(e.provider != "ai.llama" for e in evidence_after)


def test_ai_suggestions_stay_out_of_correlation_until_reviewed(env):
    case_id = page_case(env, ["Contato: contato@example.com"])
    ann = run(AIService(env.db, local_router(FakeOllama())).run_operation(case_id, "extract"))
    entity_id = ann["output"]["created_entity_ids"][0]
    from osintizada.investigation.service import _pending_ai_suggestion

    with env.db.session() as s:
        assert _pending_ai_suggestion(EntityRepository(s).get(entity_id).meta)
    reviewed = AIService(env.db, local_router(FakeOllama())).review_entity(case_id, entity_id, accepted=True,
                                                                          notes="conferido na página")
    assert reviewed["metadata"]["ai"]["review"] == "ACCEPTED" and reviewed["confidence"] == 0.6
    with env.db.session() as s:
        assert not _pending_ai_suggestion(EntityRepository(s).get(entity_id).meta)
        assert "AI_REVIEWED" in [a.event_type for a in AuditRepository(s).list(case_id)]


def test_relevance_triage_prioritizes_without_discarding(env):
    texts = [f"resultado {n} sobre example.com" for n in range(6)]
    case_id = page_case(env, texts + [texts[0]])
    before = snapshot(env, case_id)
    ann = run(AIService(env.db, local_router(FakeOllama())).run_operation(case_id, "relevance"))
    out = ann["output"]
    assert out["stages"]["input"] == 7
    assert out["stages"]["assessed"] == out["stages"]["after_deterministic_filters"] == 7   # URLs distintas
    assert out["stages"]["high_interest"] == 3
    assert all("relevant" in i and "score" in i and i["ai_suggested"] for i in out["items"])
    assert out["items"] == sorted(out["items"], key=lambda i: -i["score"])
    assert snapshot(env, case_id) == before                   # nada descartado/alterado


def test_batching_groups_items(env):
    case_id = page_case(env, [f"texto {n}" for n in range(45)])
    fake = FakeOllama()
    ann = run(AIService(env.db, local_router(fake, max_batch_items=20)).run_operation(case_id, "classify"))
    assert fake.chat_calls == 3                                # 45 itens → lotes de 20, 20, 5
    assert len(ann["output"]["items"]) == 45 and ann["output"]["counts"] == {"infraestrutura": 45}


def test_translation_is_derived_and_keeps_original(env):
    case_id = page_case(env, ["Hello, contact us at the office"])
    ann = run(AIService(env.db, local_router(FakeOllama())).run_operation(case_id, "translate"))
    item = ann["output"]["items"][0]
    assert item["original"].endswith("Hello, contact us at the office") and item["translation"].startswith("tradução")
    assert item["source_language"] == "en" and item["classification"] == "DERIVED"
    assert item["provider"] == "ai.llama" and item["model"] == "llama3.1:8b" and ann["created_at"]


def test_pivot_suggestions_not_executed(env):
    case_id = investigated_case(env)
    before = snapshot(env, case_id)
    ann = run(AIService(env.db, local_router(FakeOllama())).run_operation(case_id, "pivots"))
    queries = ann["output"]["queries"]
    assert queries and all(q["executed"] is False and "reason" in q for q in queries)
    assert {q["query"] for q in queries if q["already_executed"]} == {"example.com"}
    assert "id-inventado" in ann["output"]["validation"]["dropped_ids"]
    assert snapshot(env, case_id) == before


def test_case_ai_mode_local_only_and_failure_recorded(env):
    case_id = page_case(env, ["x"])
    with env.db.session() as s:
        case = CaseRepository(s).get(case_id)
        case.meta = {**(case.meta or {}), "ai_mode": "LOCAL_ONLY"}
    ann = run(AIService(env.db, local_router(FakeOllama(down=True))).run_operation(case_id, "summary"))
    assert ann["status"] == "AI_UNAVAILABLE" and ann["mode"] == "LOCAL_ONLY" and ann["output"] == {}
    with env.db.session() as s:
        assert "AI_ANALYSIS_FAILED" in [a.event_type for a in AuditRepository(s).list(case_id)]


def test_nothing_to_analyze_is_reported_honestly(env):
    case_id = env.service.create_case("vazio")
    fake = FakeOllama()
    ann = run(AIService(env.db, local_router(fake)).run_operation(case_id, "extract"))
    assert ann["status"] == "NO_INPUT" and ann["provider"] is None and fake.chat_calls == 0
    assert ann["mode"] == "LOCAL_ONLY" and "nenhum modelo foi chamado" in ann["reason"]


def test_core_works_with_ai_disabled(env):
    case_id = investigated_case(env)            # nenhum provider de IA configurado
    with env.db.session() as s:
        assert EntityRepository(s).list(case_id) and not AIAnnotationRepository(s).list(case_id)


def test_api_end_to_end(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SEGREDO-que-nao-pode-vazar-1234")
    app = create_app(env.service, env.jobs, None, ai_router=local_router(FakeOllama()))
    with TestClient(app) as client:
        for path in ("/ai/status", "/ai/providers", "/health"):
            r = client.get(path)
            assert r.status_code == 200 and "SEGREDO" not in r.text and "sk-ant" not in r.text
        health = client.get("/health").json()
        assert health["ai"]["mode"] == "LOCAL_ONLY" and health["ai"]["llama"]["status"] == "READY"
        case_id = client.post("/cases", json={"name": "api-ia"}).json()["id"]
        inv = client.post(f"/cases/{case_id}/investigate", json={"inputs": ["example.com"], "mode": "quick"}).json()
        wait(client, inv["job_id"])
        assert client.put(f"/cases/{case_id}/ai-mode", json={"ai_mode": "local_only"}).json()["metadata"]["ai_mode"] \
            == "LOCAL_ONLY"
        assert client.put(f"/cases/{case_id}/ai-mode", json={"ai_mode": "TOTAL"}).status_code == 422
        sub = client.post(f"/cases/{case_id}/ai/analyze", json={"operation": "summary"}).json()
        assert sub["job_type"] == "AI_ANALYSIS" and sub["status"] == "QUEUED"
        job = wait(client, sub["job_id"])
        assert job["status"] == "COMPLETED" and job["result"]["ai_status"] == "OK"
        ann = client.get(f"/cases/{case_id}/ai").json()[0]
        assert ann["operation"] == "summary" and ann["mode"] == "LOCAL_ONLY"
        rev = client.post(f"/cases/{case_id}/ai/{ann['id']}/review", json={"accepted": True})
        assert rev.json()["review"]["decision"] == "ACCEPTED"
        assert client.post(f"/cases/{case_id}/ai/analyze", json={"operation": "inventada"}).status_code == 422
        task = client.post("/ai/tasks", json={"task": "classify", "text": "servidor dns", "labels": ["infraestrutura"]})
        assert task.json()["output"]["items"][0]["label"] == "infraestrutura"
    bundle = ExportService(env.db, env.settings).bundle(case_id)
    assert bundle["ai_annotations"][0]["ai_generated"] is True
    assert "Análises por IA" in ExportService.to_html(bundle)
    assert json.loads(ExportService.to_json(bundle))["ai_annotations"]


def wait(client, job_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/jobs/{job_id}").json()
        if body["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return body
        time.sleep(0.1)
    raise AssertionError(body["status"])
