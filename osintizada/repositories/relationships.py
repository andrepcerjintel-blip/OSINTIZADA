from __future__ import annotations

import hashlib

from sqlalchemy import select

from osintizada.core.enums import RelationType
from osintizada.core.secrets import sanitize_payload
from osintizada.db.tables import RelationshipEvidenceRow, RelationshipRow, utcnow
from osintizada.repositories.base import Repository


def relationship_fingerprint(source_id: str, target_id: str, rel_type: RelationType | str) -> str:
    return hashlib.sha256(f"{source_id}|{RelationType(rel_type).value}|{target_id}".encode()).hexdigest()


class RelationshipRepository(Repository):
    def upsert(
        self,
        case_id: str,
        source_entity_id: str,
        target_entity_id: str,
        rel_type: RelationType,
        *,
        reason: str,
        evidence_ids: list[str],
        confidence: float,
        metadata: dict | None = None,
    ) -> tuple[RelationshipRow, bool]:
        """Nunca cria relação sem motivo e sem evidência verificável."""
        if not evidence_ids:
            raise ValueError("Relação exige ao menos uma evidência")
        if not reason or len(reason.strip()) < 3:
            raise ValueError("Relação exige motivo")
        if source_entity_id == target_entity_id:
            raise ValueError("Relação de uma entidade consigo mesma")
        fp = relationship_fingerprint(source_entity_id, target_entity_id, rel_type)
        row = self.session.scalar(select(RelationshipRow).where(RelationshipRow.case_id == case_id,
                                                                RelationshipRow.fingerprint == fp))
        created = row is None
        if created:
            row = RelationshipRow(case_id=case_id, source_entity_id=source_entity_id,
                                  target_entity_id=target_entity_id, relationship_type=RelationType(rel_type).value,
                                  confidence=confidence, reason=reason.strip(), fingerprint=fp,
                                  meta=sanitize_payload(metadata or {}))
            self.session.add(row)
            self.session.flush()
        else:
            row.confidence = max(row.confidence, confidence)
            row.updated_at = utcnow()
        linked = set(self.evidence_ids(row.id))
        for evidence_id in evidence_ids:
            if evidence_id not in linked:
                self.session.add(RelationshipEvidenceRow(relationship_id=row.id, evidence_id=evidence_id))
                linked.add(evidence_id)
        self.session.flush()
        return row, created

    def evidence_ids(self, relationship_id: str) -> list[str]:
        return list(self.session.scalars(select(RelationshipEvidenceRow.evidence_id)
                                         .where(RelationshipEvidenceRow.relationship_id == relationship_id)))

    def list(self, case_id: str) -> list[RelationshipRow]:
        return list(self.session.scalars(select(RelationshipRow).where(RelationshipRow.case_id == case_id)
                                         .order_by(RelationshipRow.created_at)))
