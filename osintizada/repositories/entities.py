from __future__ import annotations

from sqlalchemy import select

from osintizada.core.canonical import canonicalize, entity_fingerprint_hash
from osintizada.core.enums import EntityOrigin, EntityType
from osintizada.core.secrets import sanitize_payload
from osintizada.db.tables import EntityRow, utcnow
from osintizada.repositories.base import Repository

# Ordem de "força" da origem: um SEED nunca é rebaixado a DISCOVERED.
_ORIGIN_RANK = {EntityOrigin.SEED.value: 3, EntityOrigin.MANUAL.value: 2, EntityOrigin.DISCOVERED.value: 1,
                EntityOrigin.DERIVED.value: 0}


class EntityRepository(Repository):
    def upsert(
        self,
        case_id: str,
        entity_type: EntityType,
        value: str,
        *,
        display_value: str | None = None,
        origin: EntityOrigin = EntityOrigin.DISCOVERED,
        classification: str = "PUBLIC",
        confidence: float = 0.5,
        depth: int = 0,
        metadata: dict | None = None,
    ) -> tuple[EntityRow, bool]:
        """Cria ou funde pelo fingerprint (tipo + valor canônico). Retorna (linha, criada)."""
        canonical = canonicalize(entity_type, value)
        fingerprint = entity_fingerprint_hash(entity_type, canonical)
        row = self.by_fingerprint(case_id, fingerprint)
        if row is None:
            row = EntityRow(case_id=case_id, type=EntityType(entity_type).value, canonical_value=canonical,
                            display_value=display_value or value.strip(), fingerprint=fingerprint,
                            origin=origin.value, classification=classification, confidence=confidence,
                            depth=depth, meta=sanitize_payload(metadata or {}))
            self.session.add(row)
            self.session.flush()
            return row, True
        # Fusão: nunca apaga informação anterior.
        row.confidence = max(row.confidence, confidence)
        row.depth = min(row.depth, depth)
        if _ORIGIN_RANK.get(origin.value, 0) > _ORIGIN_RANK.get(row.origin, 0):
            row.origin = origin.value
        if metadata:
            merged = dict(row.meta or {})
            for key, val in sanitize_payload(metadata).items():
                merged.setdefault(key, val)
            row.meta = merged
        row.updated_at = utcnow()
        return row, False

    def by_fingerprint(self, case_id: str, fingerprint: str) -> EntityRow | None:
        return self.session.scalar(select(EntityRow).where(EntityRow.case_id == case_id,
                                                           EntityRow.fingerprint == fingerprint))

    def find(self, case_id: str, entity_type: EntityType, value: str) -> EntityRow | None:
        try:
            canonical = canonicalize(entity_type, value)
        except ValueError:
            return None
        return self.by_fingerprint(case_id, entity_fingerprint_hash(entity_type, canonical))

    def get(self, entity_id: str) -> EntityRow | None:
        return self.session.get(EntityRow, entity_id)

    def list(self, case_id: str, entity_type: EntityType | None = None) -> list[EntityRow]:
        stmt = select(EntityRow).where(EntityRow.case_id == case_id)
        if entity_type is not None:
            stmt = stmt.where(EntityRow.type == EntityType(entity_type).value)
        return list(self.session.scalars(stmt.order_by(EntityRow.depth, EntityRow.created_at)))

    def count(self, case_id: str) -> int:
        return len(self.list(case_id))

    def created_by_execution(self, case_id: str, execution_id: str | None, min_depth: int = 0) -> list[EntityRow]:
        """Entidades com evidência produzida por uma SearchExecution (para retomada de pivôs)."""
        if not execution_id:
            return []
        from osintizada.db.tables import EvidenceRow

        ids = select(EvidenceRow.entity_id).where(EvidenceRow.case_id == case_id,
                                                   EvidenceRow.search_execution_id == execution_id)
        return list(self.session.scalars(select(EntityRow).where(EntityRow.id.in_(ids),
                                                                 EntityRow.depth >= min_depth)))

    def add_attribute_observation(self, row: EntityRow, key: str, value, evidence_id: str, provider: str) -> None:
        """Atributos são append-only: cada valor observado mantém sua evidência (base de contradições)."""
        meta = dict(row.meta or {})
        attrs = {k: list(v) for k, v in (meta.get("attributes") or {}).items()}
        observations = attrs.setdefault(key, [])
        if not any(o["value"] == value and o["evidence_id"] == evidence_id for o in observations):
            observations.append({"value": value, "evidence_id": evidence_id, "provider": provider})
        meta["attributes"] = attrs
        row.meta = sanitize_payload(meta)
