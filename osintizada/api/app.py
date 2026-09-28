"""API FastAPI do OSINTIZADA.

Rotas: cases, investigate (execução em background), entidades, evidências, relações,
buscas, auditoria, pivôs, correlações, conflitos, providers e health.

Segurança:
  * ``OSINTIZADA_API_TOKEN`` definido → exige ``Authorization: Bearer <token>`` em todas as rotas
    (exceto ``/health``). Sem token, sirva apenas em 127.0.0.1 (padrão do ``osintizada serve``).
  * respostas nunca incluem secrets: credenciais aparecem mascaradas; raw_data já é sanitizado.

A investigação roda em background no próprio processo (BackgroundTasks). A camada
``InvestigationService.start/run`` já separa agendamento de execução, pronta para um
worker/fila (Celery/RQ/Dramatiq) sem mudar os contratos da API.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from osintizada import __version__
from osintizada.core.enums import EntityType
from osintizada.db import Database
from osintizada.investigation.service import InvestigationRequest, InvestigationService
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
    row_to_dict,
)

log = logging.getLogger("osintizada.api")

HEALTH_STATUS = {"healthy": "HEALTHY", "degraded": "DEGRADED", "unhealthy": "UNAVAILABLE",
                 "not_configured": "NOT_CONFIGURED", "unknown": "DEGRADED"}


class CaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


def create_app(service: InvestigationService | None = None) -> FastAPI:
    if service is None:
        db = Database()
        if os.environ.get("OSINTIZADA_AUTO_MIGRATE", "1") != "0":
            db.upgrade()
        service = InvestigationService(db)

    app = FastAPI(title="OSINTIZADA", version=__version__,
                  description="OSINT Investigation Orchestrator — toda conclusão rastreável até a evidência.")
    app.state.service = service
    db = service.db

    def require_token(request: Request) -> None:
        expected = os.environ.get("OSINTIZADA_API_TOKEN")
        if not expected:
            return
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="Token inválido ou ausente")

    auth = [Depends(require_token)]

    def get_case_or_404(session, case_id: str):
        case = CaseRepository(session).get(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Case não encontrado")
        return case

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    # --- cases -----------------------------------------------------------------------------

    @app.post("/cases", status_code=201, dependencies=auth)
    def create_case(body: CaseCreate) -> dict:
        case_id = service.create_case(body.name, body.description, body.metadata)
        with db.session() as s:
            return row_to_dict(CaseRepository(s).get(case_id))

    @app.get("/cases", dependencies=auth)
    def list_cases(limit: int = Query(100, ge=1, le=1000)) -> list[dict]:
        with db.session() as s:
            return [row_to_dict(c) for c in CaseRepository(s).list(limit)]

    @app.get("/cases/{case_id}", dependencies=auth)
    def get_case(case_id: str) -> dict:
        with db.session() as s:
            case = get_case_or_404(s, case_id)
            data = row_to_dict(case)
            data["inputs"] = [row_to_dict(i) for i in CaseRepository(s).inputs(case_id)]
            data["investigations"] = [row_to_dict(i) for i in InvestigationRepository(s).list(case_id)]
            data["counts"] = {
                "entities": len(EntityRepository(s).list(case_id)),
                "evidence": len(EvidenceRepository(s).list(case_id)),
                "relationships": len(RelationshipRepository(s).list(case_id)),
                "searches": len(SearchRepository(s).list(case_id)),
            }
            return data

    @app.post("/cases/{case_id}/investigate", status_code=202, dependencies=auth)
    async def investigate(case_id: str, body: InvestigationRequest, background: BackgroundTasks) -> dict:
        try:
            investigation_id = service.start(case_id, body)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        async def job() -> None:
            try:
                await service.run(case_id, investigation_id, body)
            except Exception:  # já registrado como CASE_FAILED pelo serviço
                log.warning("investigação terminou com falha", extra={"case_id": case_id})

        background.add_task(job)
        return {"case_id": case_id, "investigation_id": investigation_id, "status": "RUNNING"}

    # --- dados do case ------------------------------------------------------------------------

    @app.get("/cases/{case_id}/entities", dependencies=auth)
    def entities(case_id: str, type: EntityType | None = None) -> list[dict]:  # noqa: A002
        with db.session() as s:
            get_case_or_404(s, case_id)
            counts: dict[str, int] = {}
            for ev in EvidenceRepository(s).list(case_id):
                counts[ev.entity_id] = counts.get(ev.entity_id, 0) + 1
            return [row_to_dict(e) | {"evidence_count": counts.get(e.id, 0)}
                    for e in EntityRepository(s).list(case_id, type)]

    @app.get("/cases/{case_id}/entities/{entity_id}", dependencies=auth)
    def entity_detail(case_id: str, entity_id: str) -> dict:
        """Entidade + evidências + relações + "como chegamos aqui?" (cadeia de pivôs até o seed)."""
        with db.session() as s:
            get_case_or_404(s, case_id)
            entity = EntityRepository(s).get(entity_id)
            if entity is None or entity.case_id != case_id:
                raise HTTPException(status_code=404, detail="Entidade não encontrada")
            rels = RelationshipRepository(s)
            related = [row_to_dict(r) | {"evidence_ids": rels.evidence_ids(r.id)} for r in rels.list(case_id)
                       if entity_id in (r.source_entity_id, r.target_entity_id)]
            return row_to_dict(entity) | {
                "evidence": [row_to_dict(e) for e in EvidenceRepository(s).list(case_id, entity_id=entity_id)],
                "relationships": related,
                "investigative_path": _path(s, case_id, entity_id),
            }

    @app.get("/cases/{case_id}/evidence", dependencies=auth)
    def evidence(case_id: str, entity_id: str | None = None) -> list[dict]:
        with db.session() as s:
            get_case_or_404(s, case_id)
            return [row_to_dict(e) for e in EvidenceRepository(s).list(case_id, entity_id=entity_id)]

    @app.get("/cases/{case_id}/relationships", dependencies=auth)
    def relationships(case_id: str) -> list[dict]:
        with db.session() as s:
            get_case_or_404(s, case_id)
            rels = RelationshipRepository(s)
            return [row_to_dict(r) | {"evidence_ids": rels.evidence_ids(r.id)} for r in rels.list(case_id)]

    @app.get("/cases/{case_id}/searches", dependencies=auth)
    def searches(case_id: str) -> list[dict]:
        with db.session() as s:
            get_case_or_404(s, case_id)
            return [row_to_dict(e) for e in SearchRepository(s).list(case_id)]

    @app.get("/cases/{case_id}/audit", dependencies=auth)
    def audit(case_id: str) -> list[dict]:
        with db.session() as s:
            get_case_or_404(s, case_id)
            return [row_to_dict(a) for a in AuditRepository(s).list(case_id)]

    @app.get("/cases/{case_id}/pivots", dependencies=auth)
    def pivots(case_id: str) -> list[dict]:
        with db.session() as s:
            get_case_or_404(s, case_id)
            return [row_to_dict(p) for p in PivotRepository(s).list(case_id)]

    @app.get("/cases/{case_id}/correlations", dependencies=auth)
    def correlations(case_id: str) -> list[dict]:
        with db.session() as s:
            get_case_or_404(s, case_id)
            return [row_to_dict(c) for c in CorrelationRepository(s).list(case_id)]

    @app.get("/cases/{case_id}/conflicts", dependencies=auth)
    def conflicts(case_id: str) -> list[dict]:
        with db.session() as s:
            get_case_or_404(s, case_id)
            return [row_to_dict(c) for c in ConflictRepository(s).list(case_id)]

    # --- providers -----------------------------------------------------------------------------

    def provider_list():
        return service.orchestrator.providers

    @app.get("/providers", dependencies=auth)
    def providers() -> list[dict]:
        return [{
            "name": p.name, "display_name": p.display_name, "description": p.description,
            "type": p.provider_type.value, "tier": p.effective_tier, "source_tag": p.source_tag,
            "enabled": p.enabled, "configured": p.is_configured(),
            "status": ("CONFIGURED" if p.is_configured() else "NOT_CONFIGURED") if p.enabled else "DISABLED",
            "not_configured_reason": None if p.is_configured() else p.not_configured_reason(),
            "requires_auth": p.requires_auth, "credentials": p.masked_credentials(),
            "supports": "all" if p.consumes_planned_queries else sorted(t.value for t in p.supported_identifiers),
            "rate_limit_per_minute": p.rate_limit_per_minute, "concurrency": p.max_concurrency,
            "cache_ttl_seconds": p.cache_ttl,
        } for p in provider_list()]

    @app.get("/providers/health", dependencies=auth)
    async def providers_health() -> list[dict]:
        runtime = service.orchestrator.runtime
        checks = await asyncio.gather(*(p.healthcheck() for p in provider_list()), return_exceptions=True)
        out = []
        for provider, check in zip(provider_list(), checks, strict=True):
            last_error = runtime.last_errors.get(provider.name)
            if isinstance(check, BaseException):
                out.append({"name": provider.name, "configured": provider.is_configured(), "status": "UNAVAILABLE",
                            "last_error": last_error, "latency_ms": None, "detail": str(check)[:300]})
                continue
            status = HEALTH_STATUS.get(check.status, "DEGRADED")
            if status == "HEALTHY" and (last_error or check.circuit != "closed"):
                status = "DEGRADED"  # responde agora, mas falhou recentemente / circuito não fechado
            out.append({"name": provider.name, "configured": check.configured, "status": status,
                        "last_error": last_error, "latency_ms": check.latency_ms, "detail": check.detail,
                        "circuit": check.circuit})
        return out

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    return app


def _path(session, case_id: str, entity_id: str) -> list[dict]:
    """Reconstrói a cadeia seed → … → entidade usando os pivôs e relações registrados."""
    entities = EntityRepository(session)
    pivots = [p for p in PivotRepository(session).list(case_id) if p.status in ("SCHEDULED", "EXECUTED")]
    by_target = {p.target_entity_id: p for p in pivots}
    rels = RelationshipRepository(session).list(case_id)
    chain: list[dict] = []
    current = entities.get(entity_id)
    seen: set[str] = set()
    while current is not None and current.id not in seen:
        seen.add(current.id)
        chain.append({"entity_id": current.id, "type": current.type, "value": current.canonical_value,
                      "depth": current.depth, "origin": current.origin})
        if current.origin == "SEED":
            break
        pivot = by_target.get(current.id)
        parent_id = pivot.source_entity_id if pivot else None
        if parent_id is None:  # entidade não pivotada: usa a relação com a entidade mais rasa
            candidates = [r for r in rels if current.id in (r.source_entity_id, r.target_entity_id)]
            parents = [entities.get(r.target_entity_id if r.source_entity_id == current.id else r.source_entity_id)
                       for r in candidates]
            parents = [p for p in parents if p is not None and p.depth < current.depth]
            parent_id = min(parents, key=lambda p: p.depth).id if parents else None
        if pivot is not None:
            chain[-1]["via"] = {"provider": pivot.provider_origin, "reason": pivot.reason}
        current = entities.get(parent_id) if parent_id else None
    return list(reversed(chain))
