"""SourceOrchestrator: escolhe fontes relevantes e executa em paralelo.

Nesta fase (Fase 1) o orquestrador cobre um único nível (depth 0):
  seeds → seleção de providers → coleta paralela → evidências → entidades.

Pivôs recursivos, correlação e persistência entram nas fases seguintes e
reutilizam ``SearchRun`` como contrato.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from pydantic import BaseModel, Field

from osintizada.config import Settings, get_settings
from osintizada.core.enums import EntityOrigin, IdentifierType, ProviderStatus, SearchMode
from osintizada.core.evidence import EvidenceEngine
from osintizada.core.models import (
    Entity,
    Evidence,
    NormalizedIdentifier,
    ProviderResponse,
    new_id,
    utcnow,
)
from osintizada.core.normalization import normalize
from osintizada.core.query_planner import PlannedQuery, QueryPlanner
from osintizada.orchestration.search_manager import AggregatedHit, SearchManager
from osintizada.providers.base import BaseProvider, ProviderRegistry, load_builtin_providers
from osintizada.resilience import ProviderRuntime


class ProviderSelection(BaseModel):
    """Decisão de seleção de um provider para um identificador, com motivo."""

    provider: str
    identifier: str
    identifier_type: str
    selected: bool
    status_if_skipped: ProviderStatus | None = None
    reason: str


class SearchLogEntry(BaseModel):
    query: str
    provider: str
    identifier_type: str | None
    started_at: str
    finished_at: str | None
    results: int
    status: ProviderStatus
    reason: str | None = None
    cache_hit: bool = False
    errors: list[str] = Field(default_factory=list)


class SearchRun(BaseModel):
    run_id: str = Field(default_factory=new_id)
    case_id: str | None = None
    mode: SearchMode
    seeds: list[Entity] = Field(default_factory=list)
    selections: list[ProviderSelection] = Field(default_factory=list)
    responses: list[ProviderResponse] = Field(default_factory=list)
    evidences: list[Evidence] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    planned_queries: list[PlannedQuery] = Field(default_factory=list)
    budget_spent: int = 0
    cancelled: bool = False

    def search_hits(self) -> list[AggregatedHit]:
        """Páginas encontradas pelos mecanismos de busca, deduplicadas entre mecanismos."""
        return SearchManager.aggregate(self.responses)

    @property
    def search_log(self) -> list[SearchLogEntry]:
        return [
            SearchLogEntry(
                query=r.query, provider=r.provider,
                identifier_type=r.identifier_type.value if r.identifier_type else None,
                started_at=r.started_at.isoformat(),
                finished_at=r.finished_at.isoformat() if r.finished_at else None,
                results=len(r.results), status=r.status, errors=r.errors,
                reason=r.metadata.get("reason"), cache_hit=bool(r.metadata.get("cache_hit")),
            )
            for r in self.responses
        ]

    def status_summary(self) -> dict[str, dict[str, int]]:
        summary: dict[str, dict[str, int]] = {}
        for r in self.responses:
            summary.setdefault(r.provider, {})
            summary[r.provider][r.status.value] = summary[r.provider].get(r.status.value, 0) + 1
        return summary


def seed_entity(identifier: NormalizedIdentifier) -> Entity:
    """Input do investigador entra como SEED, nunca como descoberta."""
    return Entity(
        type=identifier.entity_type,
        value=identifier.entity_value,
        display_value=identifier.original,
        origin=EntityOrigin.SEED,
        identifier_type=identifier.type,
        attributes={"variants": identifier.variants, **identifier.metadata},
        depth=0,
    )


class SourceOrchestrator:
    def __init__(
        self,
        registry: ProviderRegistry | None = None,
        settings: Settings | None = None,
        providers: Iterable[BaseProvider] | None = None,
        runtime: ProviderRuntime | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.runtime = runtime or ProviderRuntime()
        if providers is not None:
            self.providers = list(providers)
        else:
            registry = registry if registry is not None else load_builtin_providers()
            self.providers = registry.create_all(self.settings, self.runtime)
        self.planner = QueryPlanner(self.settings)
        self.search_manager = SearchManager(self.providers)
        self._cancel = asyncio.Event()

    def cancel(self) -> None:
        """Controle humano: cancela execuções pendentes."""
        self._cancel.set()

    # --- seleção -------------------------------------------------------------------

    def select(
        self,
        identifier: NormalizedIdentifier,
        mode: SearchMode,
        allowed: set[str] | None = None,
        blocked: set[str] | None = None,
    ) -> tuple[list[BaseProvider], list[ProviderSelection]]:
        profile = self.settings.mode(mode)
        chosen: list[BaseProvider] = []
        decisions: list[ProviderSelection] = []

        def decide(p: BaseProvider, selected: bool, reason: str, status: ProviderStatus | None = None) -> None:
            decisions.append(ProviderSelection(
                provider=p.name, identifier=identifier.value, identifier_type=identifier.type.value,
                selected=selected, status_if_skipped=status, reason=reason,
            ))

        for p in self.providers:
            if not p.supports(identifier.type):
                continue  # não aplicável: não polui o log
            if blocked and p.name in blocked:
                decide(p, False, "Bloqueado pelo investigador", ProviderStatus.SKIPPED)
            elif allowed is not None and p.name not in allowed:
                decide(p, False, "Fora da lista de providers escolhida", ProviderStatus.SKIPPED)
            elif not p.enabled:
                decide(p, False, "Desabilitado na configuração", ProviderStatus.SKIPPED)
            elif p.effective_tier > profile.max_provider_tier:
                decide(p, False, f"Tier {p.effective_tier} acima do máximo do modo ({profile.max_provider_tier})",
                       ProviderStatus.SKIPPED)
            elif not p.is_configured():
                decide(p, False, "Credenciais não configuradas", ProviderStatus.NOT_CONFIGURED)
            else:
                chosen.append(p)
                decide(p, True, f"Suporta {identifier.type.value}; tier {p.effective_tier}; custo {p.cost}")

        chosen.sort(key=lambda p: (p.effective_tier, p.cost, p.name))
        return chosen, decisions

    # --- execução ------------------------------------------------------------------

    async def run(
        self,
        identifiers: list[NormalizedIdentifier],
        mode: SearchMode = SearchMode.QUICK,
        case_id: str | None = None,
        allowed: set[str] | None = None,
        blocked: set[str] | None = None,
        planned_queries: dict[str, list[PlannedQuery]] | None = None,
    ) -> SearchRun:
        """Executa uma rodada (depth 0).

        ``planned_queries`` permite fornecer consultas prontas por valor de
        identificador (ex.: RAW SEARCH); caso contrário o QueryPlanner gera.
        """
        profile = self.settings.mode(mode)
        run = SearchRun(mode=mode, case_id=case_id, seeds=[seed_entity(i) for i in identifiers])
        budget = _Budget(self.settings.query_budget.max_cost_per_search)
        tasks: list[_Task] = []
        query_tasks: list[tuple[BaseProvider, NormalizedIdentifier, PlannedQuery]] = []

        for ident in identifiers:
            chosen, decisions = self.select(ident, mode, allowed=allowed, blocked=blocked)
            run.selections.extend(decisions)
            for sel in decisions:
                if not sel.selected:
                    run.responses.append(_skipped(sel.provider, ident, sel.reason,
                                                  sel.status_if_skipped or ProviderStatus.SKIPPED))

            direct = [p for p in chosen if not p.consumes_planned_queries]
            engines = [p for p in chosen if p.consumes_planned_queries]
            for provider in direct:
                if budget.take(provider.cost):
                    tasks.append(_Task(provider, ident, None))
                else:
                    run.responses.append(_skipped(provider.name, ident, "Orçamento de consultas esgotado"))

            if engines:
                queries = (planned_queries or {}).get(ident.value)
                if queries is None:
                    queries = self.planner.plan(ident, mode=mode)
                run.planned_queries.extend(queries)
                scheduled, incompatible = self.search_manager.schedule(queries, engines)
                for engine_name, dropped in incompatible.items():
                    run.responses.append(_skipped(
                        engine_name, ident,
                        f"{len(dropped)} consulta(s) com operadores não suportados por este mecanismo"))
                query_tasks.extend((e, ident, q) for e, q in scheduled)

        # Limite global de consultas do modo, em ordem de prioridade.
        query_tasks.sort(key=lambda t: t[2].priority, reverse=True)
        over_limit: dict[str, int] = {}
        over_budget: dict[str, int] = {}
        executed_queries: set[str] = set()
        for engine, ident, q in query_tasks:
            if q.id not in executed_queries and len(executed_queries) >= profile.max_queries:
                over_limit[engine.name] = over_limit.get(engine.name, 0) + 1
                continue
            if not budget.take(engine.cost):
                over_budget[engine.name] = over_budget.get(engine.name, 0) + 1
                continue
            executed_queries.add(q.id)
            tasks.append(_Task(engine, ident, q))
        for name, n in over_limit.items():
            run.responses.append(_skipped(name, None, f"{n} consulta(s) além do limite do modo ({profile.max_queries})"))
        for name, n in over_budget.items():
            run.responses.append(_skipped(name, None, f"{n} consulta(s) não executadas: orçamento esgotado"))

        semaphore = asyncio.Semaphore(max(1, profile.concurrency))

        async def execute(task: _Task) -> ProviderResponse:
            async with semaphore:
                if self._cancel.is_set():
                    return _skipped(task.provider.name, task.identifier, "Pesquisa cancelada pelo investigador",
                                    ProviderStatus.CANCELLED, query=task.query.query if task.query else None)
                response = await task.provider.search(task.identifier, task.query.query if task.query else None)
                if task.query is not None:
                    response.metadata.update(planned_query_id=task.query.id, reason=task.query.reason,
                                             priority=task.query.priority, category=task.query.category.value)
                return response

        responses = await asyncio.gather(*(execute(t) for t in tasks))
        run.cancelled = self._cancel.is_set()
        run.budget_spent = budget.spent

        evidence_engine = EvidenceEngine(case_id=case_id)
        for response in responses:
            run.responses.append(response)
            run.evidences.extend(evidence_engine.add_response(response))

        seed_ids = {s.id for s in run.seeds}
        run.entities = [e for e in evidence_engine.entities_from(evidence_engine.evidences, depth=1)
                        if e.id not in seed_ids]
        return run

    async def run_raw(self, query: str, allowed: set[str] | None = None,
                      blocked: set[str] | None = None) -> SearchRun:
        """RAW SEARCH: envia a consulta do investigador, sem alteração, aos mecanismos compatíveis."""
        planned = QueryPlanner.raw(query)
        ident = normalize(planned.query, IdentifierType.KEYWORD)
        return await self.run([ident], mode=SearchMode.RAW, allowed=allowed, blocked=blocked,
                              planned_queries={ident.value: [planned]})


class _Task:
    __slots__ = ("provider", "identifier", "query")

    def __init__(self, provider: BaseProvider, identifier: NormalizedIdentifier, query: PlannedQuery | None) -> None:
        self.provider = provider
        self.identifier = identifier
        self.query = query


class _Budget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.spent = 0

    def take(self, cost: int) -> bool:
        if self.spent + cost > self.limit:
            return False
        self.spent += cost
        return True


def _skipped(provider: str, ident: NormalizedIdentifier | None, reason: str,
             status: ProviderStatus = ProviderStatus.SKIPPED, query: str | None = None) -> ProviderResponse:
    return ProviderResponse(
        provider=provider, query=query or (ident.value if ident else "*"),
        identifier_type=ident.type if ident else None, status=status, finished_at=utcnow(), errors=[reason],
    )
