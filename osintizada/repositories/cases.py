from __future__ import annotations

from sqlalchemy import select

from osintizada.core.enums import CaseStatus
from osintizada.core.secrets import sanitize_payload
from osintizada.db.tables import CaseInputRow, CaseRow, InvestigationRow, utcnow
from osintizada.repositories.base import Repository


class CaseRepository(Repository):
    def create(self, name: str, description: str | None = None, metadata: dict | None = None) -> CaseRow:
        row = CaseRow(name=name.strip()[:200] or "Caso sem nome", description=description,
                      status=CaseStatus.OPEN.value, meta=sanitize_payload(metadata or {}))
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, case_id: str) -> CaseRow | None:
        return self.session.get(CaseRow, case_id)

    def list(self, limit: int = 100) -> list[CaseRow]:
        return list(self.session.scalars(select(CaseRow).order_by(CaseRow.created_at.desc()).limit(limit)))

    def set_status(self, case: CaseRow, status: CaseStatus) -> None:
        case.status = status.value
        case.updated_at = utcnow()

    def add_input(self, case_id: str, raw_input: str, identifier_type: str, normalized_value: str,
                  entity_id: str | None, detection: dict, investigation_id: str | None = None) -> CaseInputRow:
        row = CaseInputRow(case_id=case_id, raw_input=raw_input, identifier_type=identifier_type,
                           normalized_value=normalized_value, entity_id=entity_id, detection=detection,
                           investigation_id=investigation_id)
        self.session.add(row)
        self.session.flush()
        return row

    def inputs(self, case_id: str) -> list[CaseInputRow]:
        return list(self.session.scalars(select(CaseInputRow).where(CaseInputRow.case_id == case_id)
                                         .order_by(CaseInputRow.created_at)))


class InvestigationRepository(Repository):
    def create(self, case_id: str, mode: str, max_depth: int, request: dict) -> InvestigationRow:
        row = InvestigationRow(case_id=case_id, mode=mode, max_depth=max_depth, request=sanitize_payload(request))
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, investigation_id: str) -> InvestigationRow | None:
        return self.session.get(InvestigationRow, investigation_id)

    def finish(self, row: InvestigationRow, status: str, summary: dict) -> None:
        row.status = status
        row.finished_at = utcnow()
        row.summary = sanitize_payload(summary)

    def list(self, case_id: str) -> list[InvestigationRow]:
        return list(self.session.scalars(select(InvestigationRow).where(InvestigationRow.case_id == case_id)
                                         .order_by(InvestigationRow.started_at)))
