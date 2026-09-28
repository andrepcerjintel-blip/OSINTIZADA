"""SearchManager: coordena múltiplos mecanismos de busca.

Responsabilidades:
  * decidir quais (consulta, mecanismo) são compatíveis (operadores suportados);
  * agregar hits de vários mecanismos deduplicando por URL canônica — a mesma
    página vista por N mecanismos é UM resultado com N fontes, nunca N
    evidências independentes.

A execução (concorrência, orçamento, cancelamento) fica no SourceOrchestrator.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from pydantic import BaseModel, Field

from osintizada.core.enums import EntityType
from osintizada.core.models import ProviderResponse
from osintizada.core.query_planner import PlannedQuery
from osintizada.core.urls import canonical_url
from osintizada.providers.base import BaseProvider


class EngineSighting(BaseModel):
    engine: str
    rank: int
    query: str


class AggregatedHit(BaseModel):
    canonical_url: str
    url: str
    title: str | None = None
    snippet: str | None = None
    observed_at: datetime | None = None
    identifier_in_snippet: bool = False
    sightings: list[EngineSighting] = Field(default_factory=list)

    @property
    def engines(self) -> list[str]:
        return sorted({s.engine for s in self.sightings})

    @property
    def best_rank(self) -> int:
        return min(s.rank for s in self.sightings)


class SearchManager:
    def __init__(self, engines: Iterable[BaseProvider]) -> None:
        self.engines = [e for e in engines if e.consumes_planned_queries]

    def schedule(self, queries: Iterable[PlannedQuery], engines: Iterable[BaseProvider] | None = None
                 ) -> tuple[list[tuple[BaseProvider, PlannedQuery]], dict[str, list[str]]]:
        """Pares (mecanismo, consulta) compatíveis, em ordem de prioridade.

        Retorna também, por mecanismo, as consultas descartadas por operador não suportado.
        """
        pool = list(engines) if engines is not None else self.engines
        scheduled: list[tuple[BaseProvider, PlannedQuery]] = []
        incompatible: dict[str, list[str]] = {}
        for q in sorted(queries, key=lambda q: q.priority, reverse=True):
            for engine in pool:
                if engine.supports_query(q.query):  # type: ignore[attr-defined]
                    scheduled.append((engine, q))
                else:
                    incompatible.setdefault(engine.name, []).append(q.query)
        return scheduled, incompatible

    @staticmethod
    def aggregate(responses: Iterable[ProviderResponse]) -> list[AggregatedHit]:
        hits: dict[str, AggregatedHit] = {}
        for response in responses:
            for result in response.results:
                raw = result.raw
                if result.type != EntityType.URL or "engine" not in raw or not result.source_url:
                    continue
                key = canonical_url(result.source_url)
                hit = hits.get(key)
                if hit is None:
                    hit = AggregatedHit(canonical_url=key, url=result.source_url, title=result.title,
                                        snippet=result.snippet, observed_at=result.observed_at)
                    hits[key] = hit
                hit.identifier_in_snippet = hit.identifier_in_snippet or bool(raw.get("identifier_in_snippet"))
                hit.sightings.append(EngineSighting(engine=raw["engine"], rank=int(raw.get("rank", 0)),
                                                    query=str(raw.get("query", response.query))))
        return sorted(hits.values(), key=lambda h: (-len(h.engines), not h.identifier_in_snippet, h.best_rank))
