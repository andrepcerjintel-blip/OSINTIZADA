"""InvestigationService — ciclo investigativo completo e persistente.

    SEED → PROVIDERS → EVIDENCE → ENTITIES → PIVOTS → (próxima profundidade) … → CORRELATION → PERSISTÊNCIA

Garantias:
  * todo input do investigador vira SEED (``case_inputs`` + entidade ``origin=SEED``);
  * cada resposta de provider vira uma ``SearchExecution`` (inclusive SKIPPED/NOT_CONFIGURED);
  * cada item de resultado vira Evidence ligada a UMA Entity (fingerprint tipo+valor canônico);
  * relações só existem com motivo + evidência;
  * sem loops: fingerprints visitados/agendados; profundidade limitada por ``max_depth``;
  * orçamentos (entidades, pivôs, chamadas, tempo) terminam a investigação com BUDGET_EXHAUSTED,
    nunca com erro;
  * nenhum dado é inventado: providers sem credencial aparecem como NOT_CONFIGURED.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from pydantic import BaseModel, Field, field_validator

from osintizada.config import Settings, get_settings
from osintizada.core.canonical import canonicalize, entity_fingerprint_hash
from osintizada.core.enums import (
    CLASSIFICATION_TO_SOURCE_TYPE,
    AuditEvent,
    CaseStatus,
    DataClassification,
    DataTemporality,
    EntityOrigin,
    EntityType,
    IdentifierType,
    PivotStatus,
    ProviderStatus,
    SearchMode,
    SourceType,
)
from osintizada.core.evidence import content_hash, evidence_fingerprint_for
from osintizada.core.identifiers import IdentifierEngine
from osintizada.core.models import (
    NormalizedIdentifier,
    ProviderResponse,
    ProviderResult,
)
from osintizada.core.normalization import normalize
from osintizada.core.secrets import sanitize
from osintizada.core.urls import canonical_url
from osintizada.db import Database
from osintizada.db.tables import EntityRow, utcnow
from osintizada.investigation.control import (
    ExecutionControl,
    InvestigationCancelled,
    NullControl,
)
from osintizada.investigation.correlation import (
    CorrelationEngine,
    RelationView,
    detect_conflicts,
)
from osintizada.investigation.pivot import EntitySnapshot, PivotDecision, PivotEngine
from osintizada.observability import case_context
from osintizada.orchestration.source_orchestrator import (
    RunHooks,
    SearchRun,
    SourceOrchestrator,
)
from osintizada.repositories import (
    AuditRepository,
    CaseRepository,
    ConflictRepository,
    CorrelationRepository,
    EntityRepository,
    EvidenceRepository,
    InvestigationRepository,
    PivotRepository,
    RelationshipRepository,
    SearchRepository,
)

log = logging.getLogger("osintizada.investigation")
COMPONENT = "InvestigationService"
_NOT_CALLED = {ProviderStatus.SKIPPED, ProviderStatus.NOT_CONFIGURED, ProviderStatus.CANCELLED}


class InputSpec(BaseModel):
    value: str = Field(min_length=1, max_length=2048)
    type: IdentifierType | None = None  # seleção manual do tipo (opcional)


class InvestigationRequest(BaseModel):
    inputs: list[InputSpec | str] = Field(min_length=1, max_length=50)
    mode: SearchMode = SearchMode.DEEP
    max_depth: int | None = Field(default=None, ge=0)
    max_entities: int | None = Field(default=None, ge=1)
    max_pivots: int | None = Field(default=None, ge=0)
    max_provider_calls: int | None = Field(default=None, ge=1)
    max_runtime_seconds: float | None = Field(default=None, gt=0)
    providers: list[str] | None = None          # restringe aos providers informados
    blocked_providers: list[str] = Field(default_factory=list)
    blocked_values: list[str] = Field(default_factory=list)  # entidades/domínios que não devem ser pivotados
    # False (padrão): consultas já concluídas com sucesso neste Case (dentro da janela configurada)
    # não são repetidas — o banco é a memória investigativa. True força nova coleta.
    refresh: bool = False

    @field_validator("inputs")
    @classmethod
    def _specs(cls, value: list) -> list[InputSpec]:
        return [v if isinstance(v, InputSpec) else InputSpec(value=v) for v in value]

    @field_validator("mode")
    @classmethod
    def _no_raw(cls, value: SearchMode) -> SearchMode:
        if value == SearchMode.RAW:
            raise ValueError("Use RAW SEARCH pelo endpoint/CLI próprio")
        return value


@dataclass
class Limits:
    max_depth: int
    max_entities: int
    max_pivots: int
    max_provider_calls: int
    max_runtime_seconds: float


@dataclass
class Target:
    identifier: NormalizedIdentifier
    entity_id: str
    entity_type: EntityType
    canonical_value: str
    fingerprint: str
    depth: int


@dataclass
class RunState:
    limits: Limits
    visited: set[str] = field(default_factory=set)
    scheduled: set[str] = field(default_factory=set)
    entity_count: int = 0
    pivots_scheduled: int = 0
    provider_calls: int = 0
    budget_events: list[str] = field(default_factory=list)
    stats: Counter = field(default_factory=Counter)
    blocked_values: set[str] = field(default_factory=set)
    control: ExecutionControl = field(default_factory=NullControl)
    depth: int = 0
    round_started: int = 0
    round_finished: int = 0
    pending_events: list = field(default_factory=list)

    def defer(self, event_type: str, data: dict) -> None:
        """Eventos gerados dentro de uma transação só são emitidos após o commit."""
        self.pending_events.append((event_type, data))

    def flush_events(self) -> None:
        events, self.pending_events = self.pending_events, []
        for event_type, data in events:
            self.control.emit(event_type, data)


class InvestigationService:
    def __init__(self, db: Database, orchestrator: SourceOrchestrator | None = None,
                 settings: Settings | None = None) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.orchestrator = orchestrator or SourceOrchestrator(settings=self.settings)
        self.identifiers = IdentifierEngine()
        self.pivots = PivotEngine(self.settings)
        self.correlation = CorrelationEngine(self.settings)

    # --- Case --------------------------------------------------------------------------

    def create_case(self, name: str, description: str | None = None, metadata: dict | None = None) -> str:
        with self.db.session() as s:
            case = CaseRepository(s).create(name, description, metadata)
            AuditRepository(s).log(case.id, AuditEvent.CASE_CREATED, COMPONENT, f"Case '{case.name}' criado")
            return case.id

    def resolve_limits(self, request: InvestigationRequest) -> Limits:
        profile = self.settings.mode(request.mode)
        depth = profile.max_depth if request.max_depth is None else request.max_depth
        return Limits(
            max_depth=min(depth, self.settings.search.hard_max_depth),
            max_entities=request.max_entities or profile.max_entities,
            max_pivots=profile.max_pivots if request.max_pivots is None else request.max_pivots,
            max_provider_calls=request.max_provider_calls or profile.max_provider_calls,
            max_runtime_seconds=request.max_runtime_seconds or profile.max_runtime_seconds,
        )

    def start(self, case_id: str, request: InvestigationRequest, enforce_idle: bool = True) -> str:
        """Valida e registra o início (síncrono). A coleta roda em ``run``.

        ``enforce_idle=False`` é usado pelo worker: a exclusividade é garantida pelo lock
        distribuído do Case, não pelo status (que é derivado dos Jobs).
        """
        limits = self.resolve_limits(request)
        with self.db.session() as s:
            cases = CaseRepository(s)
            case = cases.get(case_id)
            if case is None:
                raise LookupError(f"Case {case_id} não encontrado")
            if enforce_idle and case.status == CaseStatus.RUNNING.value:
                raise RuntimeError("Case já possui investigação em andamento")
            if case.status == CaseStatus.ARCHIVED.value:
                raise RuntimeError("Case arquivado não aceita novas investigações")
            inv = InvestigationRepository(s).create(case_id, request.mode.value, limits.max_depth,
                                                    request.model_dump(mode="json"))
            cases.set_status(case, CaseStatus.RUNNING)
            AuditRepository(s).log(case_id, AuditEvent.CASE_STARTED, COMPONENT,
                                   f"Investigação {request.mode.value} iniciada (max_depth={limits.max_depth})",
                                   {"investigation_id": inv.id, "limits": limits.__dict__,
                                    "inputs": [i.value for i in request.inputs]})
            return inv.id

    async def investigate(self, case_id: str, request: InvestigationRequest) -> dict:
        investigation_id = self.start(case_id, request)
        return await self.run(case_id, investigation_id, request)

    # --- ciclo -----------------------------------------------------------------------------

    async def run(self, case_id: str, investigation_id: str, request: InvestigationRequest,
                  control: ExecutionControl | None = None) -> dict:
        control = control or NullControl()
        with case_context(case_id):
            state = RunState(limits=self.resolve_limits(request),
                             blocked_values={v.lower() for v in request.blocked_values}, control=control)
            try:
                summary = await self._run(case_id, investigation_id, request, state)
            except InvestigationCancelled:
                with self.db.session() as s:
                    InvestigationRepository(s).finish(InvestigationRepository(s).get(investigation_id), "CANCELLED",
                                                      {"cancelled": True, "provider_calls": state.provider_calls,
                                                       "entities_total": state.entity_count})
                    AuditRepository(s).log(case_id, AuditEvent.CASE_FINISHED, COMPONENT,
                                           "Investigação cancelada; dados já coletados foram preservados",
                                           {"investigation_id": investigation_id, "cancelled": True})
                raise
            except Exception as exc:
                log.exception("investigation failed", extra={"investigation_id": investigation_id})
                with self.db.session() as s:
                    case = CaseRepository(s).get(case_id)
                    CaseRepository(s).set_status(case, CaseStatus.FAILED)
                    inv = InvestigationRepository(s).get(investigation_id)
                    InvestigationRepository(s).finish(inv, CaseStatus.FAILED.value,
                                                      {"error": sanitize(f"{type(exc).__name__}: {exc}")})
                    AuditRepository(s).log(case_id, AuditEvent.CASE_FAILED, COMPONENT,
                                           f"Investigação falhou: {type(exc).__name__}: {exc}",
                                           {"investigation_id": investigation_id})
                raise
            return summary

    async def _run(self, case_id: str, investigation_id: str, request: InvestigationRequest,
                   state: RunState) -> dict:
        limits, control = state.limits, state.control
        started = time.monotonic()
        deadline = started + limits.max_runtime_seconds
        checkpoint = control.load_checkpoint() or {}

        if checkpoint.get("name") == "CORRELATION_COMPLETED":
            self._restore_state(case_id, checkpoint, state)
            analysis = checkpoint.get("analysis", {})
            return self._complete(case_id, investigation_id, request, state, checkpoint.get("depth", 0),
                                  analysis.get("correlations", 0), analysis.get("conflicts", 0), started)
        if checkpoint.get("frontier") is not None:
            frontier = self._restore_state(case_id, checkpoint, state)
            depth = int(checkpoint.get("depth", 0))
            control.emit("JOB_CHECKPOINT", {"resumed_from": checkpoint.get("name"), "depth": depth,
                                            "pending_identifiers": len(frontier)})
            with self.db.session() as s:
                AuditRepository(s).log(case_id, AuditEvent.JOB_RECOVERED, COMPONENT,
                                       f"Investigação retomada a partir do checkpoint {checkpoint.get('name')}",
                                       {"investigation_id": investigation_id, "depth": depth})
        else:
            control.progress("SEEDS", progress=1)
            frontier = self._register_seeds(case_id, investigation_id, request, state)
            depth = 0
            control.checkpoint("SEEDS_PROCESSED", self._snapshot(depth, frontier, state))

        while frontier:
            control.check_cancelled()
            if time.monotonic() >= deadline:
                self._budget(case_id, state, "max_runtime", f"Tempo máximo ({limits.max_runtime_seconds}s) atingido")
                break
            remaining_calls = limits.max_provider_calls - state.provider_calls
            if remaining_calls <= 0:
                self._budget(case_id, state, "max_provider_calls",
                             f"Limite de chamadas a providers ({limits.max_provider_calls}) atingido")
                break

            state.depth, state.round_started, state.round_finished = depth, 0, 0
            stage = "INITIAL_PROVIDERS" if depth == 0 else f"PIVOT_DEPTH_{depth}"
            self._progress(state, stage)
            with self.db.session() as s:
                AuditRepository(s).log(case_id, AuditEvent.SEARCH_STARTED, "SourceOrchestrator",
                                       f"Rodada depth={depth}: {len(frontier)} identificador(es)",
                                       {"investigation_id": investigation_id, "depth": depth,
                                        "identifiers": [t.canonical_value for t in frontier][:50]})
            hooks = RunHooks(
                is_cancelled=control.is_cancelled,
                already_executed=None if request.refresh else self._execution_memory(case_id, frontier),
                on_start=lambda provider, ident, query: self._on_provider_start(state, provider, ident, query),
                on_finish=lambda response: self._on_provider_finish(state, response),
            )
            run = await self.orchestrator.run(
                [t.identifier for t in frontier], mode=request.mode, case_id=case_id,
                allowed=set(request.providers) if request.providers else None,
                blocked=set(request.blocked_providers) or None, depth=depth,
                call_limit=remaining_calls, deadline=deadline, hooks=hooks,
            )
            for t in frontier:
                state.visited.add(t.fingerprint)
            new_entities = self._persist_round(case_id, investigation_id, run, frontier, depth, state)
            frontier = self._pivot(case_id, investigation_id, new_entities, state)
            name = "INITIAL_PROVIDERS_COMPLETED" if depth == 0 else f"PIVOTS_DEPTH_{depth}_COMPLETED"
            depth += 1
            control.checkpoint(name, self._snapshot(depth, frontier, state))
            control.check_cancelled()  # após persistir: nada coletado é perdido

        self._progress(state, "CORRELATION")
        correlations, conflicts = self._finalize_analysis(case_id)
        snapshot = self._snapshot(depth, [], state)
        snapshot["analysis"] = {"correlations": correlations, "conflicts": conflicts}
        control.checkpoint("CORRELATION_COMPLETED", snapshot)
        return self._complete(case_id, investigation_id, request, state, depth, correlations, conflicts, started)

    def _complete(self, case_id: str, investigation_id: str, request: InvestigationRequest, state: RunState,
                  depth: int, correlations: int, conflicts: int, started: float) -> dict:
        summary = {
            "investigation_id": investigation_id,
            "mode": request.mode.value,
            "limits": state.limits.__dict__,
            "depth_reached": max(0, depth - 1),
            "provider_calls": state.provider_calls,
            "entities_total": state.entity_count,
            "pivots_scheduled": state.pivots_scheduled,
            "budget_exhausted": state.budget_events,
            "correlations": correlations,
            "conflicts": conflicts,
            "stats": dict(state.stats),
            "runtime_seconds": round(time.monotonic() - started, 2),
        }
        with self.db.session() as s:
            case = CaseRepository(s).get(case_id)
            CaseRepository(s).set_status(case, CaseStatus.COMPLETED)
            InvestigationRepository(s).finish(InvestigationRepository(s).get(investigation_id),
                                              CaseStatus.COMPLETED.value, summary)
            AuditRepository(s).log(case_id, AuditEvent.CASE_FINISHED, COMPONENT,
                                   f"Investigação concluída: {state.entity_count} entidades, "
                                   f"{state.provider_calls} chamadas, {state.pivots_scheduled} pivôs",
                                   {"investigation_id": investigation_id, "summary": summary})
        self._progress(state, "COMPLETED", progress=100)
        return summary

    # --- checkpoints / retomada ---------------------------------------------------------------

    @staticmethod
    def _snapshot(depth: int, frontier: list[Target], state: RunState) -> dict:
        """Estado mínimo para reconstruir o trabalho pendente (não serializa pilha interna)."""
        return {
            "depth": depth,
            "frontier": [{"entity_id": t.entity_id, "entity_type": t.entity_type.value,
                          "canonical_value": t.canonical_value, "fingerprint": t.fingerprint, "depth": t.depth,
                          "identifier_type": t.identifier.type.value, "identifier_value": t.identifier.original}
                         for t in frontier],
            "visited": sorted(state.visited),
            "scheduled": sorted(state.scheduled),
            "provider_calls": state.provider_calls,
            "pivots_scheduled": state.pivots_scheduled,
            "budget_events": list(state.budget_events),
            "stats": dict(state.stats),
        }

    def _restore_state(self, case_id: str, checkpoint: dict, state: RunState) -> list[Target]:
        state.visited = set(checkpoint.get("visited", []))
        state.scheduled = set(checkpoint.get("scheduled", []))
        state.provider_calls = int(checkpoint.get("provider_calls", 0))
        state.pivots_scheduled = int(checkpoint.get("pivots_scheduled", 0))
        state.budget_events = list(checkpoint.get("budget_events", []))
        state.stats.update(checkpoint.get("stats", {}))
        with self.db.session() as s:
            state.entity_count = EntityRepository(s).count(case_id)
        frontier = []
        for item in checkpoint.get("frontier") or []:
            ident = normalize(item["identifier_value"], IdentifierType(item["identifier_type"]))
            frontier.append(Target(ident, item["entity_id"], EntityType(item["entity_type"]),
                                   item["canonical_value"], item["fingerprint"], int(item["depth"])))
        return frontier

    def _execution_memory(self, case_id: str, frontier: list[Target]):
        """Consulta o banco antes de chamar um provider: SUCCESS/EMPTY recente não é repetido."""
        by_value = {t.identifier.value: t for t in frontier}
        cutoff = utcnow() - timedelta(hours=self.settings.jobs.reuse_executions_max_age_hours)

        def lookup(provider: str, ident: NormalizedIdentifier, query: str) -> str | None:
            target = by_value.get(ident.value)
            if target is None:
                return None
            with self.db.session() as s:
                return SearchRepository(s).find_reusable(case_id, provider, ident.type.value,
                                                         target.canonical_value, query, cutoff)
        return lookup

    # --- progresso ------------------------------------------------------------------------------

    def _progress(self, state: RunState, stage: str, progress: int | None = None) -> None:
        if progress is None:
            rounds = state.limits.max_depth + 2  # rodadas possíveis + correlação (aproximado)
            fraction = state.round_finished / state.round_started if state.round_started else 0.0
            progress = min(99, int(100 * (state.depth + fraction) / rounds))
        state.control.progress(
            stage, progress=progress, depth=state.depth, providers_completed=state.round_finished,
            providers_total=state.round_started, provider_calls=state.provider_calls,
            entities_found=state.entity_count, evidence_found=state.stats.get("evidence_created", 0),
            pivots_processed=state.pivots_scheduled)

    def _on_provider_start(self, state: RunState, provider: str, ident: NormalizedIdentifier, query) -> None:
        state.round_started += 1
        state.control.emit("PROVIDER_STARTED", {"provider": provider, "identifier": ident.value, "query": query,
                                                "depth": state.depth})

    def _on_provider_finish(self, state: RunState, response: ProviderResponse) -> None:
        state.round_finished += 1
        state.control.emit("PROVIDER_FINISHED", {
            "provider": response.provider, "status": response.status.value, "results": len(response.results),
            "error_code": response.error_code, "cache_hit": bool(response.metadata.get("cache_hit"))})
        self._progress(state, "INITIAL_PROVIDERS" if state.depth == 0 else f"PIVOT_DEPTH_{state.depth}")

    # --- seeds -----------------------------------------------------------------------------

    def _register_seeds(self, case_id: str, investigation_id: str, request: InvestigationRequest,
                        state: RunState) -> list[Target]:
        frontier: list[Target] = []
        with self.db.session() as s:
            entities, cases, audit = EntityRepository(s), CaseRepository(s), AuditRepository(s)
            state.entity_count = entities.count(case_id)
            for spec in request.inputs:
                detection = self.identifiers.detect(spec.value)
                if spec.type is not None:
                    ident = normalize(spec.value, spec.type)
                elif detection.primary is not None:
                    cand = detection.primary
                    ident = normalize(cand.value or spec.value, cand.type)
                else:
                    audit.log(case_id, AuditEvent.SEED_REGISTERED, COMPONENT, "Input vazio ignorado")
                    continue
                entity, created = entities.upsert(
                    case_id, ident.entity_type, ident.entity_value, display_value=spec.value.strip(),
                    origin=EntityOrigin.SEED, classification=DataClassification.MANUAL.value, confidence=1.0, depth=0,
                    metadata={"source_type": SourceType.USER_INPUT.value, "identifier_type": ident.type.value,
                              "variants": ident.variants})
                if created:
                    state.entity_count += 1
                cases.add_input(case_id, spec.value, ident.type.value, ident.value, entity.id,
                                detection.model_dump(mode="json"), investigation_id)
                audit.log(case_id, AuditEvent.SEED_REGISTERED, COMPONENT,
                          f"Seed {ident.type.value} '{ident.value}' (USER_INPUT)",
                          {"entity_id": entity.id, "ambiguous": detection.is_ambiguous,
                           "hypotheses": [c.type.value for c in detection.candidates]})
                if entity.fingerprint not in state.scheduled:
                    state.scheduled.add(entity.fingerprint)
                    frontier.append(Target(ident, entity.id, EntityType(entity.type), entity.canonical_value,
                                           entity.fingerprint, 0))
        return frontier

    # --- persistência de uma rodada ----------------------------------------------------------

    def _persist_round(self, case_id: str, investigation_id: str, run: SearchRun, frontier: list[Target],
                       depth: int, state: RunState) -> list[tuple[EntityRow, str, str]]:
        """Persiste execuções, entidades, evidências e relações. Retorna entidades NOVAS (p/ pivô)."""
        by_value = {t.identifier.value: t for t in frontier}
        new_entities: list[tuple[EntityRow, str, str]] = []
        with self.db.session() as s:
            repos = _Repos(s)
            for response in run.responses:
                target = by_value.get(response.metadata.get("identifier_value", ""))
                execution = repos.searches.record(case_id, response, depth=depth,
                                                  identifier_value=target.canonical_value if target else None,
                                                  investigation_id=investigation_id)
                state.stats[f"status:{response.status.value}"] += 1
                if response.status not in _NOT_CALLED and not response.metadata.get("cache_hit"):
                    state.provider_calls += 1
                if response.status in (ProviderStatus.SUCCESS, ProviderStatus.NO_RESULTS):
                    repos.audit.log(case_id, AuditEvent.PROVIDER_CALLED, response.provider,
                                    f"{response.provider}: {response.status.value} ({len(response.results)} resultado(s))",
                                    {"search_execution_id": execution.id, "query": response.query,
                                     "cache_hit": bool(response.metadata.get("cache_hit"))})
                elif response.status not in _NOT_CALLED:
                    repos.audit.log(case_id, AuditEvent.PROVIDER_FAILED, response.provider,
                                    f"{response.provider}: {response.status.value} {response.error_code or ''}",
                                    {"search_execution_id": execution.id, "errors": response.errors})
                if target is not None and response.error_code == "ALREADY_EXECUTED":
                    # Retomada: entidades descobertas por aquela execução voltam a ser candidatas a pivô.
                    previous = response.metadata.get("previous_execution_id")
                    for ent in repos.entities.created_by_execution(case_id, previous, min_depth=target.depth + 1):
                        new_entities.append((ent, target.entity_id, response.provider))
                    state.stats["executions_reused"] += 1
                    continue
                if target is None or not response.results:
                    continue
                # Resultados sem source_entity primeiro: garantem que a origem exista antes das relações derivadas.
                ordered = sorted(response.results, key=lambda r: r.source_entity is not None)
                for result in ordered:
                    created = self._persist_result(case_id, repos, response, result, target, execution.id, state)
                    if created is not None:
                        new_entities.append(created)
            repos.audit.log(case_id, AuditEvent.SEARCH_FINISHED, "SourceOrchestrator",
                            f"Rodada depth={depth} concluída: {len(run.responses)} execução(ões), "
                            f"{len(new_entities)} entidade(s) nova(s)",
                            {"investigation_id": investigation_id, "depth": depth,
                             "status": dict(Counter(r.status.value for r in run.responses))})
        state.flush_events()
        return new_entities

    def _persist_result(self, case_id: str, repos: _Repos, response: ProviderResponse, result: ProviderResult,
                        target: Target, execution_id: str, state: RunState) -> tuple[EntityRow, str, str] | None:
        try:
            canonical = canonicalize(result.type, result.value)
        except ValueError:
            state.stats["invalid_values"] += 1
            return None
        is_self = result.type == target.entity_type and canonical == target.canonical_value
        fingerprint = entity_fingerprint_hash(result.type, canonical)
        exists = repos.entities.by_fingerprint(case_id, fingerprint) is not None
        if not is_self and not exists and state.entity_count >= state.limits.max_entities:
            self._budget(case_id, state, "max_entities",
                         f"Limite de entidades ({state.limits.max_entities}) atingido; novas descobertas descartadas",
                         repos=repos)
            state.stats["entities_dropped_budget"] += 1
            return None

        origin = EntityOrigin.DERIVED if result.classification == DataClassification.DERIVED else EntityOrigin.DISCOVERED
        entity, created = repos.entities.upsert(
            case_id, result.type, result.value, display_value=result.value, origin=origin,
            classification=result.classification.value, confidence=result.confidence,
            depth=target.depth if is_self else target.depth + 1,
            metadata={"first_seen_by": response.provider})
        if created:
            state.entity_count += 1
            state.defer("ENTITY_CREATED", {"entity_id": entity.id, "type": entity.type,
                                                  "value": entity.canonical_value, "depth": entity.depth,
                                                  "provider": response.provider})
            repos.audit.log(case_id, AuditEvent.ENTITY_CREATED, response.provider,
                            f"{entity.type} '{entity.canonical_value}' (depth {entity.depth})",
                            {"entity_id": entity.id, "search_execution_id": execution_id})

        source_type = result.source_type or CLASSIFICATION_TO_SOURCE_TYPE[result.classification]
        evidence, ev_created = repos.evidence.add(
            case_id=case_id, entity_id=entity.id, provider=response.provider, source_type=source_type.value,
            source_name=result.source_name, source_url=result.source_url,
            canonical_url=canonical_url(result.source_url) if result.source_url else None,
            query=response.query, raw_data={"provider_raw": result.raw}, normalized_value=canonical,
            confidence=result.confidence, collected_at=result.collected_at, observed_at=result.observed_at,
            temporality=(DataTemporality.HISTORICAL_DATA if result.is_historical else DataTemporality.CURRENT_DATA).value,
            content_hash=content_hash(result.raw),
            fingerprint=evidence_fingerprint_for(result.type, canonical, result.source_url, result.raw),
            search_execution_id=execution_id,
            metadata={"title": result.title, "snippet": result.snippet, "relation_reason": result.relation_reason,
                      "attributes": result.attributes},
        )
        if ev_created:
            state.stats["evidence_created"] += 1
            repos.audit.log(case_id, AuditEvent.EVIDENCE_CREATED, response.provider,
                            f"Evidência de {entity.type} '{entity.canonical_value}' via {response.provider}",
                            {"evidence_id": evidence.id, "entity_id": entity.id})
            if not created:
                repos.audit.log(case_id, AuditEvent.ENTITY_MERGED, response.provider,
                                f"Nova evidência anexada a {entity.type} '{entity.canonical_value}' existente",
                                {"entity_id": entity.id, "evidence_id": evidence.id})
        for key, value in result.attributes.items():
            repos.entities.add_attribute_observation(entity, key, value, evidence.id, response.provider)

        if result.relation_to_query is not None and not is_self:
            source_id = target.entity_id
            if result.source_entity is not None:
                src = repos.entities.find(case_id, result.source_entity.type, result.source_entity.value)
                source_id = src.id if src is not None else None
            if source_id and source_id != entity.id:
                a, b = (source_id, entity.id) if result.relation_direction == "forward" else (entity.id, source_id)
                rel, rel_created = repos.relationships.upsert(
                    case_id, a, b, result.relation_to_query,
                    reason=result.relation_reason or f"Relação informada por {response.provider}",
                    evidence_ids=[evidence.id], confidence=result.confidence)
                if rel_created:
                    repos.audit.log(case_id, AuditEvent.RELATIONSHIP_CREATED, response.provider,
                                    f"{rel.relationship_type}: {rel.reason}",
                                    {"relationship_id": rel.id, "evidence_id": evidence.id})
        if created and not is_self:
            source_entity_id = target.entity_id
            return entity, source_entity_id, response.provider
        return None

    # --- pivôs ------------------------------------------------------------------------------

    def _pivot(self, case_id: str, investigation_id: str, new_entities: list[tuple[EntityRow, str, str]],
               state: RunState) -> list[Target]:
        limits = state.limits
        decisions: list[PivotDecision] = []
        seen: set[str] = set()
        for row, source_id, provider in new_entities:
            if row.fingerprint in seen:
                continue
            seen.add(row.fingerprint)
            snapshot = EntitySnapshot(row.id, EntityType(row.type), row.canonical_value, row.display_value,
                                      row.depth, row.confidence, row.origin, row.fingerprint)
            decisions.append(self.pivots.evaluate(
                snapshot, max_depth=limits.max_depth, visited=state.visited, scheduled=state.scheduled,
                blocked_values=state.blocked_values, source_entity_id=source_id, provider_origin=provider))
        remaining = limits.max_pivots - state.pivots_scheduled
        self.pivots.plan(decisions, remaining)

        frontier: list[Target] = []
        counts: Counter = Counter()
        with self.db.session() as s:
            pivots, audit = PivotRepository(s), AuditRepository(s)
            for d in decisions:
                counts[d.status.value] += 1
                if d.status in (PivotStatus.SKIPPED_DEPTH, PivotStatus.SKIPPED_LOW_PRIORITY,
                                PivotStatus.SKIPPED_VISITED):
                    continue  # decisões triviais: contabilizadas no resumo, sem linha própria
                pivots.add(case_id=case_id, investigation_id=investigation_id, source_entity_id=d.source_entity_id,
                           target_entity_id=d.entity.id, depth=d.entity.depth, reason=d.reason,
                           provider_origin=d.provider_origin, confidence=d.entity.confidence,
                           priority=d.priority, status=d.status.value)
                if d.status == PivotStatus.SCHEDULED and d.identifier is not None:
                    state.scheduled.add(d.entity.fingerprint)
                    state.pivots_scheduled += 1
                    frontier.append(Target(d.identifier, d.entity.id, d.entity.type, d.entity.canonical_value,
                                           d.entity.fingerprint, d.entity.depth))
                    state.defer("PIVOT_CREATED", {
                        "entity_id": d.entity.id, "type": d.entity.type.value, "value": d.entity.canonical_value,
                        "depth": d.entity.depth, "priority": d.priority})
                    audit.log(case_id, AuditEvent.PIVOT_CREATED, "PivotEngine",
                              f"Pivô {d.entity.type.value} '{d.entity.canonical_value}' (depth {d.entity.depth})",
                              {"entity_id": d.entity.id, "source_entity_id": d.source_entity_id,
                               "priority": d.priority, "reason": d.reason})
            skipped = {k: v for k, v in counts.items() if k != PivotStatus.SCHEDULED.value}
            if skipped:
                audit.log(case_id, AuditEvent.PIVOT_SKIPPED, "PivotEngine", "Entidades não pivotadas nesta rodada",
                          {"investigation_id": investigation_id, "by_status": skipped})
            if counts.get(PivotStatus.SKIPPED_BUDGET.value):
                self._budget(case_id, state, "max_pivots", f"Orçamento de pivôs ({limits.max_pivots}) atingido",
                             audit=audit)
        state.stats.update({f"pivot:{k}": v for k, v in counts.items()})
        state.flush_events()
        return frontier

    # --- correlação e contradições ---------------------------------------------------------------

    def _finalize_analysis(self, case_id: str) -> tuple[int, int]:
        with self.db.session() as s:
            repos = _Repos(s)
            rows = repos.entities.list(case_id)
            snapshots = [EntitySnapshot(r.id, EntityType(r.type), r.canonical_value, r.display_value, r.depth,
                                        r.confidence, r.origin, r.fingerprint) for r in rows]
            rels = [RelationView(r.id, r.source_entity_id, r.target_entity_id, r.relationship_type,
                                 repos.relationships.evidence_ids(r.id)) for r in repos.relationships.list(case_id)]
            attributes = {r.id: (r.meta or {}).get("attributes", {}) for r in rows}
            entity_evidence: dict[str, list[str]] = {}
            for ev in repos.evidence.list(case_id):
                entity_evidence.setdefault(ev.entity_id, []).append(ev.id)

            flags = {r.id: (r.meta or {}).get("flags", {}) for r in rows}
            for image_id, reason in self.correlation.low_identity_images(snapshots, rels, flags).items():
                row = repos.entities.get(image_id)
                meta = dict(row.meta or {})
                current = dict(meta.get("flags") or {})
                if not current.get("low_identity_value"):
                    current["low_identity_value"] = {"reason": reason, "source": "automatic"}
                    meta["flags"] = current
                    row.meta = meta
                    flags[image_id] = current
                    repos.audit.log(case_id, AuditEvent.ENTITY_MERGED, "CorrelationEngine",
                                    f"Imagem marcada LOW_IDENTITY_VALUE: {reason}", {"entity_id": image_id})
            results = self.correlation.correlate(snapshots, rels, attributes, entity_evidence, flags)
            stored = 0
            for res in results:
                if res.level.value == "UNRELATED":
                    continue
                rel_id = None
                if res.relation_type is not None and res.evidence_ids:
                    rel, created = repos.relationships.upsert(
                        case_id, res.entity_a_id, res.entity_b_id, res.relation_type, reason=res.explanation(),
                        evidence_ids=res.evidence_ids, confidence=res.score / 100,
                        metadata={"source": "CorrelationEngine", "classification": "DERIVED"})
                    rel_id = rel.id
                    if created:
                        repos.audit.log(case_id, AuditEvent.RELATIONSHIP_CREATED, "CorrelationEngine",
                                        f"{rel.relationship_type}: {res.explanation()}", {"relationship_id": rel.id})
                repos.correlations.upsert(
                    case_id, res.entity_a_id, res.entity_b_id, score=res.score, level=res.level.value,
                    positive_signals=[p.as_dict() for p in res.positive_signals],
                    negative_signals=[n.as_dict() for n in res.negative_signals],
                    evidence_ids=res.evidence_ids, relationship_id=rel_id)
                repos.audit.log(case_id, AuditEvent.CORRELATION_CREATED, "CorrelationEngine", res.explanation(),
                                {"entity_a_id": res.entity_a_id, "entity_b_id": res.entity_b_id})
                stored += 1

            conflicts = detect_conflicts(attributes, self.settings.correlation.conflict_attributes)
            for entity_id, key, observations in conflicts:
                _, created = repos.conflicts.upsert(case_id, entity_id, key, observations)
                if created:
                    repos.audit.log(case_id, AuditEvent.CONFLICT_DETECTED, "CorrelationEngine",
                                    f"CONFLICTING_EVIDENCE em '{key}'",
                                    {"entity_id": entity_id, "values": [o.get("value") for o in observations]})
        return stored, len(conflicts)

    def _budget(self, case_id: str, state: RunState, name: str, message: str, repos: _Repos | None = None,
                audit: AuditRepository | None = None) -> None:
        """BUDGET_EXHAUSTED é um evento de controle, não um erro. Registrado uma vez por orçamento."""
        if name in state.budget_events:
            return
        state.budget_events.append(name)
        metadata = {"budget": name}
        if audit is None and repos is not None:
            audit = repos.audit
        if audit is not None:
            audit.log(case_id, AuditEvent.BUDGET_EXHAUSTED, COMPONENT, message, metadata)
        else:
            with self.db.session() as s:
                AuditRepository(s).log(case_id, AuditEvent.BUDGET_EXHAUSTED, COMPONENT, message, metadata)


class _Repos:
    def __init__(self, session) -> None:
        self.entities = EntityRepository(session)
        self.evidence = EvidenceRepository(session)
        self.relationships = RelationshipRepository(session)
        self.searches = SearchRepository(session)
        self.audit = AuditRepository(session)
        self.correlations = CorrelationRepository(session)
        self.conflicts = ConflictRepository(session)
