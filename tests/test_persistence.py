from datetime import datetime, timezone

import pytest

from osintizada.core.canonical import canonicalize, entity_fingerprint_hash, handle_of
from osintizada.core.enums import AuditEvent, CaseStatus, EntityOrigin, EntityType, ProviderStatus, RelationType
from osintizada.core.models import ProviderResponse
from osintizada.db import Database
from osintizada.repositories import (
    AuditRepository,
    CaseRepository,
    EntityRepository,
    EvidenceRepository,
    RelationshipRepository,
    SearchRepository,
    row_to_dict,
)

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)


@pytest.fixture
def db():
    database = Database("sqlite://")
    database.create_all()
    return database


def make_case(session):
    return CaseRepository(session).create("Caso teste", "descrição", {"origem": "teste"})


def add_evidence(session, case_id, entity_id, fp="fp1", provider="dns", url=None):
    return EvidenceRepository(session).add(
        case_id=case_id, entity_id=entity_id, provider=provider, source_type="PUBLIC", query="example.com",
        normalized_value="x", confidence=0.9, collected_at=NOW, content_hash="h" * 64, fingerprint=fp,
        raw_data={"rdata": "1.2.3.4", "api_key": "SEGREDO"}, source_url=url)


def test_case_lifecycle_and_persistence(tmp_path):
    url = f"sqlite:///{tmp_path}/c.db"
    first = Database(url)
    first.create_all()
    with first.session() as s:
        case = make_case(s)
        CaseRepository(s).set_status(case, CaseStatus.RUNNING)
        case_id = case.id
    with Database(url).session() as s:  # nova conexão: dado sobreviveu
        case = CaseRepository(s).get(case_id)
        assert case.status == "RUNNING" and case.meta == {"origem": "teste"}
        assert row_to_dict(case)["metadata"] == {"origem": "teste"}


@pytest.mark.parametrize(
    "etype,a,b",
    [
        (EntityType.EMAIL, "Fulano@Example.COM", "fulano@example.com"),
        (EntityType.PHONE, "(61) 99999-9999", "+55 61 99999-9999"),
        (EntityType.DOMAIN, "Example.COM.", "example.com"),
        (EntityType.IP, "2001:4860:4860:0:0:0:0:8888", "2001:4860:4860::8888"),
        (EntityType.ASN, "asn 15169", "AS15169"),
        (EntityType.URL, "http://www.site.com/a/?utm_source=x", "https://site.com/a"),
        (EntityType.USERNAME, "@Dark_Wolf", "dark_wolf"),
        (EntityType.SOCIAL_ACCOUNT, "GitHub:Torvalds", "github:torvalds"),
        (EntityType.ORGANIZATION, "GOOGLE   LLC", "Google LLC"),
        (EntityType.CPF, "529.982.247-25", "52998224725"),
    ],
)
def test_canonical_equivalence(etype, a, b):
    assert canonicalize(etype, a) == canonicalize(etype, b)
    assert entity_fingerprint_hash(etype, canonicalize(etype, a)) == entity_fingerprint_hash(etype, canonicalize(etype, b))


def test_fingerprint_distinguishes_type():
    assert entity_fingerprint_hash(EntityType.DOMAIN, "a.com") != entity_fingerprint_hash(EntityType.SUBDOMAIN, "a.com")


def test_handle_of():
    assert handle_of(EntityType.SOCIAL_ACCOUNT, "github:dark_wolf") == "dark_wolf"
    assert handle_of(EntityType.TELEGRAM_USER, "123456") is None  # ID numérico não é handle
    assert handle_of(EntityType.EMAIL, "a@b.com") is None


def test_entity_upsert_deduplicates_and_merges(db):
    with db.session() as s:
        case = make_case(s)
        repo = EntityRepository(s)
        a, created_a = repo.upsert(case.id, EntityType.EMAIL, "Fulano@Example.com", confidence=0.4, depth=2)
        b, created_b = repo.upsert(case.id, EntityType.EMAIL, "fulano@example.com", confidence=0.9, depth=1,
                                   origin=EntityOrigin.SEED)
        assert created_a and not created_b and a.id == b.id
        assert b.confidence == 0.9 and b.depth == 1 and b.origin == "SEED"
        c, _ = repo.upsert(case.id, EntityType.EMAIL, "fulano@example.com", origin=EntityOrigin.DISCOVERED)
        assert c.origin == "SEED"  # seed nunca é rebaixado
        assert a.display_value == "Fulano@Example.com" and a.canonical_value == "fulano@example.com"
        assert repo.count(case.id) == 1
        other = make_case(s)
        _, created_other = repo.upsert(other.id, EntityType.EMAIL, "fulano@example.com")
        assert created_other  # isolamento por case


def test_evidence_fingerprint_dedup_and_secret_removal(db):
    with db.session() as s:
        case = make_case(s)
        ent, _ = EntityRepository(s).upsert(case.id, EntityType.IP, "1.2.3.4")
        ev1, new1 = add_evidence(s, case.id, ent.id)
        ev2, new2 = add_evidence(s, case.id, ent.id, provider="dns2")
        assert new1 and not new2 and ev1.id == ev2.id
        assert ev1.meta["sightings"][0]["provider"] == "dns2"
        assert ev1.raw_data["api_key"] == "****"
        _, new3 = add_evidence(s, case.id, ent.id, fp="fp2", provider="rdap")
        assert new3  # outra fonte → nova evidência para a MESMA entidade
        assert len(EvidenceRepository(s).list(case.id, entity_id=ent.id)) == 2


def test_relationship_requires_evidence_and_reason(db):
    with db.session() as s:
        case = make_case(s)
        repo = EntityRepository(s)
        dom, _ = repo.upsert(case.id, EntityType.DOMAIN, "example.com")
        ip, _ = repo.upsert(case.id, EntityType.IP, "1.2.3.4")
        ev, _ = add_evidence(s, case.id, ip.id)
        rels = RelationshipRepository(s)
        with pytest.raises(ValueError):
            rels.upsert(case.id, dom.id, ip.id, RelationType.RESOLVES_TO, reason="A record", evidence_ids=[],
                        confidence=0.9)
        with pytest.raises(ValueError):
            rels.upsert(case.id, dom.id, dom.id, RelationType.RESOLVES_TO, reason="A record", evidence_ids=[ev.id],
                        confidence=0.9)
        r1, c1 = rels.upsert(case.id, dom.id, ip.id, RelationType.RESOLVES_TO, reason="Registro A",
                             evidence_ids=[ev.id], confidence=0.9)
        ev2, _ = add_evidence(s, case.id, ip.id, fp="fp9", provider="other")
        r2, c2 = rels.upsert(case.id, dom.id, ip.id, RelationType.RESOLVES_TO, reason="Registro A",
                             evidence_ids=[ev.id, ev2.id], confidence=0.8)
        assert c1 and not c2 and r1.id == r2.id
        assert sorted(rels.evidence_ids(r1.id)) == sorted([ev.id, ev2.id])


def test_search_execution_status_mapping(db):
    with db.session() as s:
        case = make_case(s)
        repo = SearchRepository(s)
        empty = ProviderResponse(provider="dns", query="x", status=ProviderStatus.NO_RESULTS, finished_at=NOW,
                                 started_at=NOW, metadata={"cache_hit": True, "reason": "teste"})
        failed = ProviderResponse(provider="rdap", query="x", status=ProviderStatus.FAILED, error_code="HTTP_500",
                                  errors=["HTTP 500 token=abc123"], started_at=NOW, finished_at=NOW)
        e = repo.record(case.id, empty, depth=0, identifier_value="x")
        f = repo.record(case.id, failed, depth=1, identifier_value="x")
        assert e.status == "EMPTY" and e.cache_hit and e.reason == "teste" and e.duration_ms == 0
        assert f.status == "FAILED" and f.error_code == "HTTP_500" and "abc123" not in f.error_message


def test_audit_log_is_sanitized(db):
    with db.session() as s:
        case = make_case(s)
        audit = AuditRepository(s)
        audit.log(case.id, AuditEvent.CASE_CREATED, "test", "criado com password=hunter2",
                  {"token": "abc", "nested": {"api_key": "k"}, "ok": 1})
        s.flush()
        [row] = audit.list(case.id)
        assert "hunter2" not in row.message
        assert row.meta == {"token": "****", "nested": {"api_key": "****"}, "ok": 1}


def test_alembic_migration_matches_models(tmp_path):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from osintizada.db.tables import Base

    database = Database(f"sqlite:///{tmp_path}/m.db")
    database.upgrade()
    with database.engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
