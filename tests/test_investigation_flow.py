from collections import Counter

import pytest

from osintizada.config import Settings
from osintizada.core.enums import AuditEvent, SearchMode
from osintizada.investigation.service import InvestigationRequest
from osintizada.repositories import (
    AuditRepository,
    CaseRepository,
    ConflictRepository,
    EntityRepository,
    EvidenceRepository,
    RelationshipRepository,
    SearchRepository,
)
from tests.fixtures.environment import build_service


@pytest.fixture
def env(monkeypatch):
    for var in ("BRAVE_SEARCH_API_KEY", "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION"):
        monkeypatch.delenv(var, raising=False)
    return build_service(Settings())


def entity_map(session, case_id):
    return {(e.type, e.canonical_value): e for e in EntityRepository(session).list(case_id)}


async def test_full_flow_domain_to_asn(env):
    service, db, runtime = env
    case_id = service.create_case("example.com", "fluxo completo")
    summary = await service.investigate(case_id, InvestigationRequest(inputs=["example.com"], mode=SearchMode.DEEP))

    with db.session() as s:
        case = CaseRepository(s).get(case_id)
        assert case.status == "COMPLETED"
        ents = entity_map(s, case_id)
        seed = ents[("DOMAIN", "example.com")]
        assert seed.origin == "SEED" and seed.depth == 0
        [case_input] = CaseRepository(s).inputs(case_id)
        assert case_input.source_type == "USER_INPUT" and case_input.entity_id == seed.id

        ip = ents[("IP", "93.184.216.34")]
        asn = ents[("ASN", "AS15133")]
        www = ents[("SUBDOMAIN", "www.example.com")]
        net = ents[("NETWORK", "93.184.216.0/24")]
        org = ents[("ORGANIZATION", "edgecast inc.")]
        assert (ip.depth, www.depth, asn.depth, net.depth) == (1, 1, 2, 2)
        assert org.display_value == "Edgecast Inc."

        # Sem duplicatas: o IP foi encontrado via example.com E via www.example.com → UMA entidade.
        fingerprints = [e.fingerprint for e in EntityRepository(s).list(case_id)]
        assert len(fingerprints) == len(set(fingerprints))

        # Uma entidade, várias evidências (DNS de example.com, DNS de www, RDAP...).
        ip_evidence = EvidenceRepository(s).list(case_id, entity_id=ip.id)
        assert len(ip_evidence) >= 2 and {e.provider for e in ip_evidence} >= {"infra.dns"}

        rels = RelationshipRepository(s)
        by_pair = {(r.source_entity_id, r.relationship_type, r.target_entity_id): r for r in rels.list(case_id)}
        resolves = by_pair[(seed.id, "RESOLVES_TO", ip.id)]
        assert rels.evidence_ids(resolves.id), "relação sem evidência"
        assert (ip.id, "PART_OF", asn.id) in by_pair
        assert (net.id, "REGISTERED_TO", org.id) in by_pair
        assert (www.id, "PART_OF", seed.id) in by_pair  # CT: subdomínio PART_OF domínio
        for rel in rels.list(case_id):
            assert rels.evidence_ids(rel.id) and rel.reason

        # Sem loops: cada (provider, identificador) consultado no máximo uma vez.
        executions = SearchRepository(s).list(case_id)
        called = Counter((e.provider, e.identifier_value) for e in executions
                         if e.status not in ("SKIPPED", "NOT_CONFIGURED") and e.identifier_value)
        assert max(called.values()) == 1
        dns_domains = [e.identifier_value for e in executions if e.provider == "infra.dns"]
        assert dns_domains.count("example.com") == 1  # PTR → example.com não reinvestigou o seed

        # Profundidade respeitada: nada investigado além de max_depth=2; entidades no máximo depth 3.
        assert max(e.depth for e in executions) <= 2
        assert max(e.depth for e in EntityRepository(s).list(case_id)) <= 3

        # Providers sem credenciais: NOT_CONFIGURED registrado, nenhum resultado inventado.
        brave = [e for e in executions if e.provider == "search.brave"]
        assert brave and all(e.status == "NOT_CONFIGURED" and e.result_count == 0 for e in brave)

        audit = Counter(a.event_type for a in AuditRepository(s).list(case_id))
        for event in (AuditEvent.CASE_CREATED, AuditEvent.CASE_STARTED, AuditEvent.SEARCH_STARTED,
                      AuditEvent.SEARCH_FINISHED, AuditEvent.PROVIDER_CALLED, AuditEvent.ENTITY_CREATED,
                      AuditEvent.EVIDENCE_CREATED, AuditEvent.RELATIONSHIP_CREATED, AuditEvent.PIVOT_CREATED,
                      AuditEvent.CASE_FINISHED):
            assert audit[event.value] > 0, event

        # Contradição: Team Cymru diz EU, RDAP diz US para o mesmo prefixo → ambas preservadas.
        [conflict] = ConflictRepository(s).list(case_id)
        assert conflict.entity_id == net.id and conflict.attribute == "country"
        assert conflict.status == "CONFLICTING_EVIDENCE"
        assert {o["value"] for o in conflict.observations} == {"EU", "US"}
        assert {o["provider"] for o in conflict.observations} == {"infra.dns", "infra.rdap"}

    assert summary["depth_reached"] == 2 and summary["entities_total"] >= 8


async def test_quick_mode_depth_zero_only_seed(env):
    service, db, _ = env
    case_id = service.create_case("q")
    await service.investigate(case_id, InvestigationRequest(inputs=["example.com"], mode=SearchMode.QUICK,
                                                            max_depth=0))
    with db.session() as s:
        executions = SearchRepository(s).list(case_id)
        assert {e.identifier_value for e in executions if e.identifier_value} == {"example.com"}
        assert max(e.depth for e in EntityRepository(s).list(case_id)) == 1  # descobertas guardadas, não pivotadas


async def test_budget_exhaustion_is_not_an_error(env):
    service, db, _ = env
    case_id = service.create_case("budget")
    summary = await service.investigate(case_id, InvestigationRequest(
        inputs=["example.com"], mode=SearchMode.DEEP, max_entities=3, max_pivots=1))
    with db.session() as s:
        assert CaseRepository(s).get(case_id).status == "COMPLETED"
        assert EntityRepository(s).count(case_id) <= 3
        events = [a for a in AuditRepository(s).list(case_id) if a.event_type == "BUDGET_EXHAUSTED"]
        assert {e.meta["budget"] for e in events} >= {"max_entities"}
    assert "max_entities" in summary["budget_exhausted"]


async def test_blocked_values_are_not_pivoted(env):
    service, db, _ = env
    case_id = service.create_case("blocked")
    await service.investigate(case_id, InvestigationRequest(
        inputs=["example.com"], mode=SearchMode.DEEP, blocked_values=["93.184.216.34"]))
    with db.session() as s:
        executions = SearchRepository(s).list(case_id)
        assert "93.184.216.34" not in {e.identifier_value for e in executions}


async def test_second_investigation_reuses_entities(env):
    service, db, _ = env
    case_id = service.create_case("repeat")
    await service.investigate(case_id, InvestigationRequest(inputs=["example.com"], mode=SearchMode.QUICK))
    with db.session() as s:
        first = EntityRepository(s).count(case_id)
    await service.investigate(case_id, InvestigationRequest(inputs=["example.com"], mode=SearchMode.QUICK))
    with db.session() as s:
        assert EntityRepository(s).count(case_id) == first  # nada duplicado entre execuções
        assert len(CaseRepository(s).inputs(case_id)) == 2  # cada execução registra seu seed


async def test_case_state_guards(env):
    service, db, _ = env
    case_id = service.create_case("guard")
    service.start(case_id, InvestigationRequest(inputs=["example.com"]))
    with pytest.raises(RuntimeError):
        service.start(case_id, InvestigationRequest(inputs=["example.com"]))
    with pytest.raises(LookupError):
        service.start("inexistente", InvestigationRequest(inputs=["example.com"]))
