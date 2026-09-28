from __future__ import annotations

from sqlalchemy import select

from osintizada.db.tables import ConflictRow, CorrelationRow, PivotRow, utcnow
from osintizada.repositories.base import Repository


class PivotRepository(Repository):
    def add(self, **fields) -> PivotRow:
        row = PivotRow(**fields)
        self.session.add(row)
        self.session.flush()
        return row

    def list(self, case_id: str) -> list[PivotRow]:
        return list(self.session.scalars(select(PivotRow).where(PivotRow.case_id == case_id)
                                         .order_by(PivotRow.created_at)))


class CorrelationRepository(Repository):
    def upsert(self, case_id: str, entity_a_id: str, entity_b_id: str, **fields) -> CorrelationRow:
        a, b = sorted((entity_a_id, entity_b_id))
        row = self.session.scalar(select(CorrelationRow).where(
            CorrelationRow.case_id == case_id, CorrelationRow.entity_a_id == a, CorrelationRow.entity_b_id == b))
        if row is None:
            row = CorrelationRow(case_id=case_id, entity_a_id=a, entity_b_id=b, **fields)
            self.session.add(row)
        else:
            for key, value in fields.items():
                setattr(row, key, value)
            row.updated_at = utcnow()
        self.session.flush()
        return row

    def list(self, case_id: str) -> list[CorrelationRow]:
        return list(self.session.scalars(select(CorrelationRow).where(CorrelationRow.case_id == case_id)
                                         .order_by(CorrelationRow.score.desc())))


class ConflictRepository(Repository):
    def upsert(self, case_id: str, entity_id: str, attribute: str, observations: list[dict]) -> tuple[ConflictRow, bool]:
        row = self.session.scalar(select(ConflictRow).where(
            ConflictRow.case_id == case_id, ConflictRow.entity_id == entity_id, ConflictRow.attribute == attribute))
        if row is None:
            row = ConflictRow(case_id=case_id, entity_id=entity_id, attribute=attribute, observations=observations)
            self.session.add(row)
            self.session.flush()
            return row, True
        row.observations = observations
        row.updated_at = utcnow()
        return row, False

    def list(self, case_id: str) -> list[ConflictRow]:
        return list(self.session.scalars(select(ConflictRow).where(ConflictRow.case_id == case_id)))
