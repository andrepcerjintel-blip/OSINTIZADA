"""PivotEngine: decide quais entidades descobertas viram novas investigações.

Regras (na ordem):
  1. já visitada/agendada (fingerprint)        → SKIPPED_VISITED   (evita loops)
  2. profundidade acima de ``max_depth``        → SKIPPED_DEPTH
  3. tipo sem valor de pivô (prioridade 0)      → SKIPPED_LOW_PRIORITY
  4. bloqueada pelo investigador/blocklist/IP não público → SKIPPED_BLOCKED
  5. confiança abaixo de ``min_confidence``     → SKIPPED_LOW_CONFIDENCE
  6. sem mapeamento para identificador          → SKIPPED_LOW_PRIORITY
  7. caso contrário → candidato; os candidatos são ordenados por prioridade
     (HIGH > MEDIUM > LOW), confiança e profundidade e cortados pelo orçamento
     ``max_pivots`` → SCHEDULED ou SKIPPED_BUDGET.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from osintizada.config import Settings, get_settings
from osintizada.core.domains import get_domain_parser
from osintizada.core.enums import IDENTIFIER_PLATFORM, EntityType, IdentifierType, PivotStatus
from osintizada.core.models import NormalizedIdentifier
from osintizada.core.normalization import normalize

_PLATFORM_TO_IDENTIFIER = {platform: id_type for id_type, platform in IDENTIFIER_PLATFORM.items()}
_HASH_BY_LEN = {32: IdentifierType.MD5, 40: IdentifierType.SHA1, 64: IdentifierType.SHA256, 128: IdentifierType.SHA512}


@dataclass
class EntitySnapshot:
    """Visão mínima de uma entidade para o PivotEngine (independente do banco)."""

    id: str
    type: EntityType
    canonical_value: str
    display_value: str | None
    depth: int
    confidence: float
    origin: str
    fingerprint: str


@dataclass
class PivotDecision:
    entity: EntitySnapshot
    status: PivotStatus
    reason: str
    priority: int = 0
    identifier: NormalizedIdentifier | None = None
    source_entity_id: str | None = None
    provider_origin: str | None = None
    extra: dict = field(default_factory=dict)


def entity_to_identifier(etype: EntityType, value: str, display: str | None = None) -> NormalizedIdentifier | None:
    """Converte uma entidade em identificador investigável (ou ``None``)."""
    v = value
    try:
        if etype == EntityType.EMAIL:
            return normalize(v, IdentifierType.EMAIL)
        if etype == EntityType.PHONE:
            return normalize(v, IdentifierType.PHONE)
        if etype == EntityType.DOMAIN:
            return normalize(v, IdentifierType.DOMAIN)
        if etype == EntityType.SUBDOMAIN:
            return normalize(v, IdentifierType.SUBDOMAIN)
        if etype == EntityType.IP:
            ip = ipaddress.ip_address(v)
            return normalize(v, IdentifierType.IPV4 if ip.version == 4 else IdentifierType.IPV6)
        if etype == EntityType.ASN:
            return normalize(v, IdentifierType.ASN)
        if etype == EntityType.NETWORK:
            return normalize(v, IdentifierType.CIDR)
        if etype == EntityType.USERNAME:
            return normalize(display or v, IdentifierType.USERNAME)
        if etype in (EntityType.TELEGRAM_USER, EntityType.TELEGRAM_CHANNEL, EntityType.TELEGRAM_GROUP):
            if v.lstrip("-").isdigit():
                return normalize(v, IdentifierType.TELEGRAM_ID)
            if v.startswith("invite:"):
                return None
            return normalize(v, IdentifierType.TELEGRAM_USERNAME)
        if etype == EntityType.SOCIAL_ACCOUNT:
            platform, _, handle = v.partition(":")
            id_type = _PLATFORM_TO_IDENTIFIER.get(platform)
            return normalize(handle, id_type) if id_type and handle else None
        if etype == EntityType.URL:
            return normalize(v, IdentifierType.URL)
        if etype == EntityType.CPF:
            return normalize(v, IdentifierType.CPF)
        if etype == EntityType.CNPJ:
            return normalize(v, IdentifierType.CNPJ)
        if etype == EntityType.CRYPTO_ADDRESS:
            return normalize(v, IdentifierType.EVM_ADDRESS if v.startswith("0x") else IdentifierType.BTC_ADDRESS)
        if etype == EntityType.CRYPTO_TRANSACTION:
            return normalize(v, IdentifierType.TX_HASH)
        if etype == EntityType.HASH and len(v) in _HASH_BY_LEN:
            return normalize(v, _HASH_BY_LEN[len(v)])
        if etype in (EntityType.ORGANIZATION, EntityType.PERSON):
            return normalize(display or v, IdentifierType.FULL_NAME)
    except ValueError:
        return None
    return None


class PivotEngine:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.cfg = self.settings.pivots
        self._blocklist = {d.lower() for d in self.cfg.blocklist_domains}

    def priority(self, etype: EntityType) -> int:
        return int(self.cfg.priorities.get(EntityType(etype).value, 0))

    def _blocked(self, entity: EntitySnapshot, blocked_values: set[str]) -> str | None:
        value = entity.canonical_value.lower()
        if value in blocked_values or (entity.display_value or "").lower() in blocked_values:
            return "Entidade bloqueada pelo investigador"
        if entity.type == EntityType.IP and self.cfg.skip_non_public_ips:
            try:
                if not ipaddress.ip_address(value).is_global:
                    return "IP não público (privado/reservado)"
            except ValueError:
                return "IP inválido"
        host = None
        if entity.type in (EntityType.DOMAIN, EntityType.SUBDOMAIN):
            host = value
        elif entity.type == EntityType.EMAIL:
            host = value.rsplit("@", 1)[-1]
        if host:
            registrable = get_domain_parser().registrable_domain(host) or host
            if registrable in self._blocklist and entity.type != EntityType.EMAIL:
                return f"Domínio de plataforma/infraestrutura na blocklist ({registrable})"
            if registrable in blocked_values:
                return "Domínio bloqueado pelo investigador"
        return None

    def evaluate(
        self,
        entity: EntitySnapshot,
        *,
        max_depth: int,
        visited: set[str],
        scheduled: set[str],
        blocked_values: set[str] | None = None,
        source_entity_id: str | None = None,
        provider_origin: str | None = None,
    ) -> PivotDecision:
        blocked_values = {b.lower() for b in (blocked_values or set())}

        def decision(status: PivotStatus, reason: str, **kw) -> PivotDecision:
            return PivotDecision(entity=entity, status=status, reason=reason, priority=self.priority(entity.type),
                                 source_entity_id=source_entity_id, provider_origin=provider_origin, **kw)

        if entity.fingerprint in visited or entity.fingerprint in scheduled:
            return decision(PivotStatus.SKIPPED_VISITED, "Entidade já investigada ou agendada")
        if entity.depth > max_depth:
            return decision(PivotStatus.SKIPPED_DEPTH, f"Profundidade {entity.depth} acima do máximo ({max_depth})")
        if self.priority(entity.type) <= 0:
            return decision(PivotStatus.SKIPPED_LOW_PRIORITY, f"Tipo {entity.type.value} não gera pivô")
        if reason := self._blocked(entity, blocked_values):
            return decision(PivotStatus.SKIPPED_BLOCKED, reason)
        if entity.origin != "SEED" and entity.confidence < self.cfg.min_confidence:
            return decision(PivotStatus.SKIPPED_LOW_CONFIDENCE,
                            f"Confiança {entity.confidence:.2f} abaixo do mínimo {self.cfg.min_confidence:.2f}")
        identifier = entity_to_identifier(entity.type, entity.canonical_value, entity.display_value)
        if identifier is None:
            return decision(PivotStatus.SKIPPED_LOW_PRIORITY, "Sem identificador investigável para esta entidade")
        return decision(PivotStatus.SCHEDULED, "", identifier=identifier)

    def plan(self, candidates: list[PivotDecision], remaining_pivots: int) -> list[PivotDecision]:
        """Ordena candidatos por valor investigativo e aplica o orçamento de pivôs."""
        eligible = [c for c in candidates if c.status == PivotStatus.SCHEDULED]
        eligible.sort(key=lambda c: (-c.priority, -c.entity.confidence, c.entity.depth, c.entity.canonical_value))
        labels = {3: "HIGH", 2: "MEDIUM", 1: "LOW"}
        for index, cand in enumerate(eligible):
            label = labels.get(cand.priority, str(cand.priority))
            if index < max(0, remaining_pivots):
                cand.reason = (f"Pivô {label} ({cand.entity.type.value}) descoberto via {cand.provider_origin}; "
                               f"confiança {cand.entity.confidence:.2f}")
            else:
                cand.status = PivotStatus.SKIPPED_BUDGET
                cand.reason = f"Orçamento de pivôs esgotado (prioridade {label})"
        return candidates
