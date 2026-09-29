from osintizada.core.enums import (
    DataClassification,
    EntityOrigin,
    EntityType,
    ProviderStatus,
)
from osintizada.core.evidence import EvidenceEngine
from osintizada.core.models import ProviderResponse, ProviderResult


def response(provider, url, value="fearless1999"):
    return ProviderResponse(
        provider=provider, query=value, status=ProviderStatus.SUCCESS,
        results=[ProviderResult(type=EntityType.USERNAME, value=value, source_url=url, raw={"p": provider},
                                confidence=0.8)],
    )


def test_evidence_has_provenance_and_hash():
    engine = EvidenceEngine(case_id="case-1")
    [ev] = engine.add_response(response("bing", "https://forum.example/u/fearless1999"))
    assert ev.case_id == "case-1"
    assert ev.provider == "bing"
    assert ev.query == "fearless1999"
    assert ev.source_url == "https://forum.example/u/fearless1999"
    assert len(ev.content_hash) == 64
    assert ev.collected_at is not None


def test_same_page_from_many_engines_is_one_evidence():
    engine = EvidenceEngine()
    engine.add_response(response("bing", "https://forum.example/u/fearless1999/?utm_source=x"))
    engine.add_response(response("brave", "http://www.forum.example/u/fearless1999"))
    engine.add_response(response("ddg", "https://forum.example/u/fearless1999#top"))
    assert len(engine.evidences) == 1
    assert len(engine.evidences[0].duplicate_sightings) == 2


def test_different_pages_are_distinct_evidence():
    engine = EvidenceEngine()
    engine.add_response(response("bing", "https://a.example/1"))
    engine.add_response(response("bing", "https://a.example/2"))
    assert len(engine.evidences) == 2


def test_entities_aggregate_evidence_and_origin():
    engine = EvidenceEngine()
    engine.add_response(response("bing", "https://a.example/1"))
    engine.add_response(response("bing", "https://a.example/2"))
    derived = ProviderResponse(provider="local", query="x@y.com", status=ProviderStatus.SUCCESS, results=[
        ProviderResult(type=EntityType.DOMAIN, value="y.com", classification=DataClassification.DERIVED,
                       raw={"rule": "email_domain"})])
    engine.add_response(derived)
    ents = {e.value: e for e in engine.entities_from(engine.evidences)}
    assert len(ents["fearless1999"].evidence_ids) == 2
    assert ents["fearless1999"].origin == EntityOrigin.DISCOVERED
    assert ents["y.com"].origin == EntityOrigin.DERIVED
