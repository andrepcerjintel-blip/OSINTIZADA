from __future__ import annotations

from sqlalchemy import func, select

from osintizada.core.enums import PROVIDER_TO_EXECUTION_STATUS
from osintizada.core.models import ProviderResponse
from osintizada.core.secrets import sanitize, sanitize_payload
from osintizada.db.tables import SearchExecutionRow
from osintizada.repositories.base import Repository


class SearchRepository(Repository):
    def record(self, case_id: str, response: ProviderResponse, *, depth: int, identifier_value: str | None,
               investigation_id: str | None = None) -> SearchExecutionRow:
        meta = dict(response.metadata)
        row = SearchExecutionRow(
            case_id=case_id,
            investigation_id=investigation_id,
            provider=response.provider,
            query=response.query,
            identifier_type=response.identifier_type.value if response.identifier_type else None,
            identifier_value=identifier_value,
            depth=depth,
            reason=meta.pop("reason", None),
            started_at=response.started_at,
            finished_at=response.finished_at,
            status=PROVIDER_TO_EXECUTION_STATUS[response.status].value,
            result_count=len(response.results),
            error_code=response.error_code,
            error_message=sanitize("; ".join(response.errors)) if response.errors else None,
            cache_hit=bool(meta.get("cache_hit")),
            duration_ms=round(response.duration_ms, 1) if response.duration_ms is not None else None,
            meta=sanitize_payload(meta),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def list(self, case_id: str) -> list[SearchExecutionRow]:
        return list(self.session.scalars(select(SearchExecutionRow).where(SearchExecutionRow.case_id == case_id)
                                         .order_by(SearchExecutionRow.started_at)))

    def find_reusable(self, case_id: str, provider: str, identifier_type: str, identifier_value: str,
                      query: str, since) -> str | None:
        """Execução SUCCESS/EMPTY recente da mesma (case, provider, identificador, consulta)."""
        stmt = (select(SearchExecutionRow.id)
                .where(SearchExecutionRow.case_id == case_id, SearchExecutionRow.provider == provider,
                       SearchExecutionRow.identifier_type == identifier_type,
                       SearchExecutionRow.identifier_value == identifier_value,
                       SearchExecutionRow.query == query,
                       SearchExecutionRow.status.in_(["SUCCESS", "EMPTY"]),
                       SearchExecutionRow.finished_at >= since)
                .order_by(SearchExecutionRow.finished_at.desc()).limit(1))
        return self.session.scalar(stmt)

    def count_executed(self, case_id: str, investigation_id: str) -> int:
        """Chamadas efetivamente feitas (exclui SKIPPED/NOT_CONFIGURED e respostas de cache)."""
        stmt = select(func.count()).select_from(SearchExecutionRow).where(
            SearchExecutionRow.case_id == case_id, SearchExecutionRow.investigation_id == investigation_id,
            SearchExecutionRow.status.not_in(["SKIPPED", "NOT_CONFIGURED", "CANCELLED"]))
        return int(self.session.scalar(stmt) or 0)
