"""Evidence Engine: transforma resultados de providers em evidências com proveniência.

Responsabilidades:
  * criar ``Evidence`` com hash de conteúdo e fingerprint;
  * deduplicar: a mesma página/valor vista por vários providers é UMA evidência
    com ``duplicate_sightings`` (não N evidências independentes);
  * manter separação entre RAW EVIDENCE (``raw_data``) e informação processada.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from osintizada.core.enums import DataClassification, EntityOrigin
from osintizada.core.models import (
    Entity,
    Evidence,
    ProviderResponse,
    ProviderResult,
    entity_id_for,
)
from osintizada.core.urls import canonical_url


def content_hash(payload: dict) -> str:
    """SHA-256 determinístico do conteúdo bruto (JSON com chaves ordenadas)."""
    data = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def evidence_fingerprint(result: ProviderResult) -> str:
    """Chave de deduplicação independente de provider.

    Mesma entidade + mesma URL canônica => mesma evidência.
    Sem URL, usa o hash do conteúdo bruto.
    """
    entity_key = f"{result.type.value}:{result.value}"
    if result.source_url:
        anchor = canonical_url(result.source_url)
    else:
        anchor = "raw:" + content_hash(result.raw)
    return hashlib.sha256(f"{entity_key}|{anchor}".encode()).hexdigest()


class EvidenceEngine:
    def __init__(self, case_id: str | None = None) -> None:
        self.case_id = case_id
        self._by_fingerprint: dict[str, Evidence] = {}

    @property
    def evidences(self) -> list[Evidence]:
        return list(self._by_fingerprint.values())

    def get(self, evidence_id: str) -> Evidence | None:
        return next((e for e in self._by_fingerprint.values() if e.evidence_id == evidence_id), None)

    def add_result(self, response: ProviderResponse, result: ProviderResult) -> tuple[Evidence, bool]:
        """Registra um resultado. Retorna (evidência, criada_agora)."""
        fp = evidence_fingerprint(result)
        existing = self._by_fingerprint.get(fp)
        if existing is not None:
            existing.duplicate_sightings.append(
                {
                    "provider": response.provider,
                    "query": response.query,
                    "collected_at": result.collected_at.isoformat(),
                    "source_url": result.source_url,
                }
            )
            return existing, False

        evidence = Evidence(
            case_id=self.case_id,
            provider=response.provider,
            source=result.source_name or response.provider,
            source_url=result.source_url,
            canonical_url=canonical_url(result.source_url) if result.source_url else None,
            query=response.query,
            collected_at=result.collected_at,
            observed_at=result.observed_at,
            is_historical=result.is_historical,
            raw_data=result.raw,
            normalized_value=result.value,
            entity_type=result.type,
            entity_id=entity_id_for(result.type, result.value),
            confidence=result.confidence,
            classification=result.classification,
            content_hash=content_hash(result.raw),
            fingerprint=fp,
        )
        self._by_fingerprint[fp] = evidence
        return evidence, True

    def add_response(self, response: ProviderResponse) -> list[Evidence]:
        """Registra todos os resultados de uma resposta; retorna apenas os novos."""
        created: list[Evidence] = []
        for result in response.results:
            evidence, is_new = self.add_result(response, result)
            if is_new:
                created.append(evidence)
        return created

    def entities_from(self, evidences: Iterable[Evidence], depth: int = 0) -> list[Entity]:
        """Materializa entidades a partir de evidências, agregando proveniência."""
        entities: dict[str, Entity] = {}
        for ev in evidences:
            ent = entities.get(ev.entity_id)
            if ent is None:
                origin = (EntityOrigin.DERIVED if ev.classification == DataClassification.DERIVED
                          else EntityOrigin.DISCOVERED)
                ent = Entity(type=ev.entity_type, value=ev.normalized_value, classification=ev.classification,
                             origin=origin, depth=depth, first_seen=ev.collected_at, last_seen=ev.collected_at)
                entities[ev.entity_id] = ent
            ent.evidence_ids.append(ev.evidence_id)
            ent.first_seen = min(ent.first_seen, ev.collected_at)
            ent.last_seen = max(ent.last_seen, ev.collected_at)
        return list(entities.values())
