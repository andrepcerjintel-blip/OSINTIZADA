"""AIAnnotationRepository — análises de IA (interpretação), separadas de entidades/evidências."""

from __future__ import annotations

from sqlalchemy import select

from osintizada.core.secrets import sanitize, sanitize_payload
from osintizada.db.tables import AIAnnotationRow, new_uuid, utcnow
from osintizada.repositories.base import Repository


class AIAnnotationRepository(Repository):
    def add(self, *, case_id: str, operation: str, result: dict, input_refs: dict, output: dict | None,
            job_id: str | None = None, annotation_id: str | None = None) -> AIAnnotationRow:
        row = AIAnnotationRow(
            id=annotation_id or new_uuid(), case_id=case_id, job_id=job_id, operation=operation,
            task=result["task"], status=result["status"],
            mode=result["mode"], provider=result.get("provider"), provider_kind=result.get("provider_kind"),
            model=result.get("model"), prompt_version=result["prompt_version"],
            input_refs=sanitize_payload(input_refs), output=sanitize_payload(output or {}),
            attempts=sanitize_payload(result.get("attempts") or []), latency_ms=result.get("latency_ms"),
            reason=sanitize(result["reason"])[:2000] if result.get("reason") else None,
            output_hash=result.get("output_hash"), usage=sanitize_payload(result.get("usage") or {}),
            classification="DERIVED")
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, annotation_id: str) -> AIAnnotationRow | None:
        return self.session.get(AIAnnotationRow, annotation_id)

    def list(self, case_id: str, operation: str | None = None, limit: int = 100) -> list[AIAnnotationRow]:
        stmt = select(AIAnnotationRow).where(AIAnnotationRow.case_id == case_id)
        if operation:
            stmt = stmt.where(AIAnnotationRow.operation == operation)
        return list(self.session.scalars(stmt.order_by(AIAnnotationRow.created_at.desc()).limit(limit)))

    def set_review(self, annotation_id: str, decision: str, notes: str | None = None) -> AIAnnotationRow | None:
        row = self.get(annotation_id)
        if row is not None:
            row.review = {"decision": decision, "notes": sanitize(notes)[:1000] if notes else None,
                          "at": utcnow().isoformat()}
        return row

