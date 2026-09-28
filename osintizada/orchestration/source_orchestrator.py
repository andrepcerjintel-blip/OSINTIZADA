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
from osintizada.core.enums import EntityOrigin, ProviderStatus, SearchMode
from osintizada.core.evidence import EvidenceEngine
from osintizada.core.models import (
    Entity,
    Evidence,
    NormalizedIdentifier,
    ProviderResponse,
    new_id,
    utcnow,
)
from osintizada.providers.base import BaseProvider, ProviderRegistry, load_builtin_providers


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
    cancelled: bool = False

    @property
    def search_log(self) -> list[SearchLogEntry]:
        return [
            SearchLogEntry(
                query=r.query, provider=r.provider,
                identifier_type=r.identifier_type.value if r.identifier_type else None,
                started_at=r.started_at.isoformat(),
                finished_at=r.finished_at.isoformat() if r.finished_at else None,
                results=len(r.results), status=r.status, errors=r.errors,
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
        value=identifier.value,
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
    ) -> None:
        self.settings = settings or get_settings()
        if providers is not None:
            self.providers = list(providers)
        else:
            registry = registry if registry is not None else load_builtin_providers()
            self.providers = registry.create_all(self.settings)
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
    ) -> SearchRun:
        profile = self.settings.mode(mode)
        run = SearchRun(mode=mode, case_id=case_id, seeds=[seed_entity(i) for i in identifiers])
        evidence_engine = EvidenceEngine(case_id=case_id)
        semaphore = asyncio.Semaphore(max(1, profile.concurrency))
        budget = self.settings.query_budget.max_cost_per_search
        spent = 0

        tasks: list[tuple[BaseProvider, NormalizedIdentifier]] = []
        for ident in identifiers:
            chosen, decisions = self.select(ident, mode, allowed=allowed, blocked=blocked)
            run.selections.extend(decisions)
            for sel in decisions:
                if not sel.selected:
                    run.responses.append(ProviderResponse(
                        provider=sel.provider, query=ident.value, identifier_type=ident.type,
                        status=sel.status_if_skipped or ProviderStatus.SKIPPED, finished_at=utcnow(),
                        errors=[sel.reason],
                    ))
            for provider in chosen:
                if spent + provider.cost > budget:
                    run.responses.append(ProviderResponse(
                        provider=provider.name, query=ident.value, identifier_type=ident.type,
                        status=ProviderStatus.SKIPPED, finished_at=utcnow(),
                        errors=["Orçamento de consultas esgotado"],
                    ))
                    continue
                spent += provider.cost
                tasks.append((provider, ident))

        async def execute(provider: BaseProvider, ident: NormalizedIdentifier) -> ProviderResponse:
            async with semaphore:
                if self._cancel.is_set():
                    return ProviderResponse(provider=provider.name, query=ident.value, identifier_type=ident.type,
                                            status=ProviderStatus.CANCELLED, finished_at=utcnow(),
                                            errors=["Pesquisa cancelada pelo investigador"])
                return await provider.search(ident)

        responses = await asyncio.gather(*(execute(p, i) for p, i in tasks))
        run.cancelled = self._cancel.is_set()
        for response in responses:
            run.responses.append(response)
            run.evidences.extend(evidence_engine.add_response(response))

        seed_ids = {s.id for s in run.seeds}
        run.entities = [e for e in evidence_engine.entities_from(evidence_engine.evidences, depth=1)
                        if e.id not in seed_ids]
        return run
