from __future__ import annotations

import logging

from sqlalchemy import select

from osintizada.core.enums import AuditEvent
from osintizada.core.secrets import sanitize, sanitize_payload
from osintizada.db.tables import AuditLogRow
from osintizada.repositories.base import Repository

logger = logging.getLogger("osintizada.audit")


class AuditRepository(Repository):
    def log(self, case_id: str | None, event_type: AuditEvent, component: str, message: str,
            metadata: dict | None = None) -> AuditLogRow:
        """Registra evento de auditoria. Mensagem e metadata são sanitizadas (sem credenciais)."""
        row = AuditLogRow(case_id=case_id, event_type=AuditEvent(event_type).value, component=component,
                          message=sanitize(message), meta=sanitize_payload(metadata or {}))
        self.session.add(row)
        logger.info(row.message, extra={"case_id": case_id, "event_type": row.event_type, "component": component})
        return row

    def list(self, case_id: str, event_type: AuditEvent | None = None) -> list[AuditLogRow]:
        stmt = select(AuditLogRow).where(AuditLogRow.case_id == case_id)
        if event_type is not None:
            stmt = stmt.where(AuditLogRow.event_type == AuditEvent(event_type).value)
        return list(self.session.scalars(stmt.order_by(AuditLogRow.id)))
