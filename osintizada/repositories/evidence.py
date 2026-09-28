from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from osintizada.core.secrets import sanitize_payload
from osintizada.db.tables import EvidenceRow
from osintizada.repositories.base import Repository


class EvidenceRepository(Repository):
    def add(
        self,
        *,
        case_id: str,
        entity_id: str,
        provider: str,
        source_type: str,
        query: str,
        normalized_value: str,
        confidence: float,
        collected_at: datetime,
        content_hash: str,
        fingerprint: str,
        raw_data: dict,
        source_name: str | None = None,
        source_url: str | None = None,
        canonical_url: str | None = None,
        observed_at: datetime | None = None,
        temporality: str = "CURRENT_DATA",
        search_execution_id: str | None = None,
        metadata: dict | None = None,
    ) -> tuple[EvidenceRow, bool]:
        """Registra evidência. A mesma informação da mesma fonte (fingerprint) não é duplicada:
        um novo avistamento é anexado em ``metadata.sightings``."""
        existing = self.by_fingerprint(case_id, fingerprint)
        if existing is not None:
            meta = dict(existing.meta or {})
            sightings = list(meta.get("sightings", []))
            sightings.append({"provider": provider, "query": query, "collected_at": collected_at.isoformat(),
                              "search_execution_id": search_execution_id})
            meta["sightings"] = sightings
            existing.meta = sanitize_payload(meta)
            return existing, False
        row = EvidenceRow(
            case_id=case_id, entity_id=entity_id, provider=provider, source_type=source_type,
            source_name=source_name, source_url=source_url, canonical_url=canonical_url, query=query,
            raw_data=sanitize_payload(raw_data), normalized_value=normalized_value, confidence=confidence,
            temporality=temporality, observed_at=observed_at, collected_at=collected_at, content_hash=content_hash,
            fingerprint=fingerprint, search_execution_id=search_execution_id,
            meta=sanitize_payload(metadata or {}),
        )
        self.session.add(row)
        self.session.flush()
        return row, True

    def by_fingerprint(self, case_id: str, fingerprint: str) -> EvidenceRow | None:
        return self.session.scalar(select(EvidenceRow).where(EvidenceRow.case_id == case_id,
                                                             EvidenceRow.fingerprint == fingerprint))

    def get(self, evidence_id: str) -> EvidenceRow | None:
        return self.session.get(EvidenceRow, evidence_id)

    def list(self, case_id: str, entity_id: str | None = None) -> list[EvidenceRow]:
        stmt = select(EvidenceRow).where(EvidenceRow.case_id == case_id)
        if entity_id is not None:
            stmt = stmt.where(EvidenceRow.entity_id == entity_id)
        return list(self.session.scalars(stmt.order_by(EvidenceRow.collected_at)))
