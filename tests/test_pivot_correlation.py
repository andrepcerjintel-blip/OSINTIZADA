from osintizada.config import Settings
from osintizada.core.canonical import entity_fingerprint_hash
from osintizada.core.enums import CorrelationLevel, EntityType, IdentifierType, PivotStatus, RelationType
from osintizada.investigation.correlation import (
    CorrelationEngine,
    RelationView,
    detect_conflicts,
    is_rare_handle,
)
from osintizada.investigation.pivot import EntitySnapshot, PivotEngine, entity_to_identifier


def snap(etype, value, depth=1, confidence=0.9, origin="DISCOVERED", id=None, display=None):
    return EntitySnapshot(id or f"{etype.value}:{value}", etype, value, display, depth, confidence, origin,
                          entity_fingerprint_hash(etype, value))


# --- Pivot ------------------------------------------------------------------------------


def evaluate(entity, **kw):
    engine = PivotEngine(Settings())
    params = {"max_depth": 2, "visited": set(), "scheduled": set()}
    params.update(kw)
    return engine.evaluate(entity, **params)


def test_pivot_schedules_valuable_entity():
    d = evaluate(snap(EntityType.EMAIL, "alvo@empresa.com.br"))
    assert d.status == PivotStatus.SCHEDULED and d.identifier.type == IdentifierType.EMAIL and d.priority == 3


def test_pivot_skips_visited_and_scheduled():
    e = snap(EntityType.DOMAIN, "alvo.com")
    assert evaluate(e, visited={e.fingerprint}).status == PivotStatus.SKIPPED_VISITED
    assert evaluate(e, scheduled={e.fingerprint}).status == PivotStatus.SKIPPED_VISITED


def test_pivot_respects_max_depth():
    assert evaluate(snap(EntityType.DOMAIN, "alvo.com", depth=3), max_depth=2).status == PivotStatus.SKIPPED_DEPTH
    assert evaluate(snap(EntityType.DOMAIN, "alvo.com", depth=2), max_depth=2).status == PivotStatus.SCHEDULED


def test_pivot_blocks_private_ip_platform_domains_and_user_blocklist():
    assert evaluate(snap(EntityType.IP, "10.0.0.1")).status == PivotStatus.SKIPPED_BLOCKED
    assert evaluate(snap(EntityType.SUBDOMAIN, "hera.ns.cloudflare.com")).status == PivotStatus.SKIPPED_BLOCKED
    assert evaluate(snap(EntityType.DOMAIN, "alvo.com"), blocked_values={"ALVO.com"}).status == PivotStatus.SKIPPED_BLOCKED
    # email em provedor gratuito continua pivotável (o email é do alvo, o domínio não)
    assert evaluate(snap(EntityType.EMAIL, "alvo123@gmail.com")).status == PivotStatus.SCHEDULED


def test_pivot_low_confidence_and_low_priority():
    assert evaluate(snap(EntityType.USERNAME, "talvez", confidence=0.3)).status == PivotStatus.SKIPPED_LOW_CONFIDENCE
    assert evaluate(snap(EntityType.LOCATION, "Brasília")).status == PivotStatus.SKIPPED_LOW_PRIORITY
    # seed nunca é descartado por confiança
    assert evaluate(snap(EntityType.USERNAME, "x", confidence=0.1, origin="SEED")).status == PivotStatus.SCHEDULED


def test_pivot_plan_prefers_high_value_and_applies_budget():
    engine = PivotEngine(Settings())
    params = {"max_depth": 2, "visited": set(), "scheduled": set()}
    decisions = [engine.evaluate(e, **params) for e in (
        snap(EntityType.URL, "https://a.com/x", confidence=0.99),
        snap(EntityType.SUBDOMAIN, "api.alvo.com"),
        snap(EntityType.EMAIL, "a@alvo.com", confidence=0.6),
        snap(EntityType.IP, "8.8.8.8"),
    )]
    engine.plan(decisions, remaining_pivots=2)
    scheduled = [d.entity.type for d in decisions if d.status == PivotStatus.SCHEDULED]
    assert sorted(scheduled) == sorted([EntityType.IP, EntityType.EMAIL])  # HIGH antes de MEDIUM/LOW
    budget = [d for d in decisions if d.status == PivotStatus.SKIPPED_BUDGET]
    assert len(budget) == 2 and all("Orçamento" in d.reason for d in budget)


def test_entity_to_identifier_mapping():
    assert entity_to_identifier(EntityType.IP, "2001:db8::1").type == IdentifierType.IPV6
    assert entity_to_identifier(EntityType.SOCIAL_ACCOUNT, "github:torvalds").type == IdentifierType.GITHUB_USERNAME
    assert entity_to_identifier(EntityType.TELEGRAM_USER, "1006503122").type == IdentifierType.TELEGRAM_ID
    assert entity_to_identifier(EntityType.TELEGRAM_USER, "durov").type == IdentifierType.TELEGRAM_USERNAME
    assert entity_to_identifier(EntityType.TELEGRAM_GROUP, "invite:AbC") is None
    assert entity_to_identifier(EntityType.NETWORK, "8.8.8.0/24").type == IdentifierType.CIDR
    assert entity_to_identifier(EntityType.LOCATION, "Brasília") is None


# --- Correlation -------------------------------------------------------------------------


def rel(src, tgt, ev, type_=RelationType.USES_EMAIL):
    return RelationView(f"r-{src}-{tgt}", src, tgt, type_.value, [ev])


def test_shared_email_and_telegram_id_is_high_confidence():
    a = snap(EntityType.SOCIAL_ACCOUNT, "github:fearless1999", id="A")
    b = snap(EntityType.SOCIAL_ACCOUNT, "twitter:outro_nome", id="B")
    email = snap(EntityType.EMAIL, "f@proton.me", id="E")
    tg = snap(EntityType.TELEGRAM_USER, "123456789", id="T")
    rels = [rel("A", "E", "ev1"), rel("B", "E", "ev2"), rel("T", "A", "ev3", RelationType.ASSOCIATED_WITH),
            rel("T", "B", "ev4", RelationType.ASSOCIATED_WITH)]
    [res] = CorrelationEngine(Settings()).correlate([a, b, email, tg], rels)
    assert res.level == CorrelationLevel.HIGH_CONFIDENCE and res.relation_type == RelationType.SAME_AS
    assert {s.name for s in res.positive_signals} == {"same_email", "same_telegram_id"}
    assert res.score == 100 and res.evidence_ids == ["ev1", "ev2", "ev3", "ev4"]
    assert "same_email" in res.explanation()


def test_weak_signals_alone_never_same_as():
    s = Settings()
    s.correlation.weights["same_username_rare"] = 90  # mesmo com peso exagerado…
    a = snap(EntityType.SOCIAL_ACCOUNT, "github:darkwolf_1988", id="A")
    b = snap(EntityType.SOCIAL_ACCOUNT, "reddit:darkwolf_1988", id="B")
    [res] = CorrelationEngine(s).correlate([a, b], [], entity_evidence={"A": ["e1"], "B": ["e2"]})
    assert res.level == CorrelationLevel.POSSIBLE and res.relation_type == RelationType.POSSIBLY_SAME_AS


def test_common_username_is_weak():
    a = snap(EntityType.SOCIAL_ACCOUNT, "github:joao", id="A")
    b = snap(EntityType.SOCIAL_ACCOUNT, "instagram:joao", id="B")
    [res] = CorrelationEngine(Settings()).correlate([a, b], [])
    assert res.level == CorrelationLevel.WEAK and res.relation_type is None
    assert not is_rare_handle("joao") and is_rare_handle("darkwolf_1988")


def test_negative_signal_reduces_score_deterministically():
    a = snap(EntityType.SOCIAL_ACCOUNT, "github:darkwolf_1988", id="A")
    b = snap(EntityType.SOCIAL_ACCOUNT, "reddit:darkwolf_1988", id="B")
    attrs = {"A": {"country": [{"value": "BR", "evidence_id": "e1"}]},
             "B": {"country": [{"value": "RU", "evidence_id": "e2"}]}}
    engine = CorrelationEngine(Settings())
    first = engine.correlate([a, b], [], attrs)
    second = engine.correlate([b, a], [], attrs)
    assert first[0].score == second[0].score == 25 - 15
    assert first[0].negative_signals[0].name == "conflicting_country"


def test_directly_related_entities_are_not_correlated():
    a = snap(EntityType.EMAIL, "a@x.com", id="A")
    b = snap(EntityType.SOCIAL_ACCOUNT, "github:a", id="B")
    assert CorrelationEngine(Settings()).correlate([a, b], [rel("B", "A", "ev")]) == []


def test_detect_conflicts():
    attrs = {"N": {"country": [{"value": "US", "evidence_id": "1"}, {"value": "EU", "evidence_id": "2"}],
                   "as_name": [{"value": "X", "evidence_id": "1"}]},
             "M": {"country": [{"value": "br", "evidence_id": "3"}, {"value": "BR", "evidence_id": "4"}]}}
    [(entity_id, key, obs)] = detect_conflicts(attrs, ["country"])
    assert entity_id == "N" and key == "country" and len(obs) == 2
