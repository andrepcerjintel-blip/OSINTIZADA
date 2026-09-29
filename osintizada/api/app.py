"""RINO API (FastAPI) — Plataforma de Investigação OSINT.

A API NÃO executa investigação: ela valida, grava um Job no banco, publica na fila (Redis/RQ)
e responde imediatamente. Workers separados (``rino worker``) executam os Jobs. Reiniciar
a API não afeta investigações em andamento; os resultados permanecem no banco.

Segurança:
  * ``RINO_API_TOKEN`` (legado: ``OSINTIZADA_API_TOKEN``) definido → exige ``Authorization: Bearer <token>``
    (exceto ``/health``, ``/`` e ``/branding/*``, que não expõem dados);
  * respostas nunca incluem secrets (credenciais mascaradas; só NOMES das variáveis ausentes).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, Field

from osintizada import __version__
from osintizada.api.pages import home_html
from osintizada.bootstrap import database_status
from osintizada.branding import (
    LOGO_HEADER,
    LOGO_ICON,
    PRODUCT_NAME,
    TAGLINE,
    logo_bytes,
)
from osintizada.branding import env as branding_env
from osintizada.core.enums import (
    TERMINAL_JOB_STATUSES,
    AuditEvent,
    EntityType,
    JobStatus,
    JobType,
)
from osintizada.db import Database
from osintizada.infrastructure.redis_client import redis_status
from osintizada.investigation.service import InvestigationRequest, InvestigationService
from osintizada.jobs.heartbeat import workers_status
from osintizada.jobs.service import EXPORT_FORMATS, JobService
from osintizada.observability.metrics import metrics
from osintizada.repositories import (
    AuditRepository,
    CaseRepository,
    ConflictRepository,
    CorrelationRepository,
    EntityRepository,
    EvidenceRepository,
    InvestigationRepository,
    JobRepository,
    PivotRepository,
    RelationshipRepository,
    SearchRepository,
    row_to_dict,
)
from osintizada.timeline.service import TimelineService

log = logging.getLogger("osintizada.api")

HEALTH_STATUS = {"healthy": "HEALTHY", "degraded": "DEGRADED", "unhealthy": "UNAVAILABLE",
                 "not_configured": "NOT_CONFIGURED", "unknown": "DEGRADED"}


class CaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExportCreate(BaseModel):
    format: str = Field(default="json", description="json | csv | html")


class EntityFlags(BaseModel):
    low_identity_value: bool
    reason: str = Field(default="marcada pelo investigador", max_length=500)


def build_default_services() -> tuple[InvestigationService, JobService, Any]:
    """Montagem de produção: banco (migrações), Redis opcional, cache configurado, fila RQ."""
    from osintizada.bootstrap import validate_database
    from osintizada.config import get_settings
    from osintizada.infrastructure.queue import RQJobQueue
    from osintizada.infrastructure.redis_cache import build_runtime
    from osintizada.infrastructure.redis_client import RedisNotConfigured, create_redis
    from osintizada.orchestration.source_orchestrator import SourceOrchestrator

    settings = get_settings()
    db = Database()
    validate_database(db, auto_migrate=branding_env("AUTO_MIGRATE", "1") != "0")
    try:
        redis = create_redis(settings=settings)
        redis.ping()
    except RedisNotConfigured:
        redis = None
        log.warning("REDIS_URL não configurada: jobs ficarão PENDING (QUEUE_UNAVAILABLE)")
    except Exception as exc:  # noqa: BLE001 - API sobe; health mostra Redis indisponível
        log.warning("Redis indisponível na inicialização", extra={"error": type(exc).__name__})
    runtime = build_runtime(settings, redis)
    orchestrator = SourceOrchestrator(settings=settings, runtime=runtime)
    service = InvestigationService(db, orchestrator, settings)
    queue = RQJobQueue(redis, settings.jobs.queue_name) if redis is not None else None
    return service, JobService(db, settings, queue, redis), redis


def create_app(service: InvestigationService | None = None, jobs: JobService | None = None,
               redis: Any = None, reconcile_on_startup: bool = True) -> FastAPI:
    if service is None:
        service, jobs, redis = build_default_services()
    if jobs is None:
        jobs = JobService(service.db, service.settings, None, redis)
    redis = redis if redis is not None else jobs.redis
    db = service.db
    settings = service.settings
    if redis is not None:  # contadores agregados entre API e workers
        metrics.bind_redis(redis, settings.cache.prefix)

    app = FastAPI(title=f"{PRODUCT_NAME} API", version=__version__,
                  description=f"API da plataforma {PRODUCT_NAME} para investigação OSINT ({TAGLINE}). "
                              "Toda conclusão é rastreável até a evidência que a originou.",
                  docs_url=None, redoc_url=None)  # /docs e /redoc servidos abaixo com a identidade RINO
    app.state.service, app.state.jobs, app.state.redis = service, jobs, redis

    @app.on_event("startup")
    def reconcile_at_startup() -> None:
        # Reconciliação cuidadosa: decide por heartbeat/fila/checkpoint, nunca "zera" Cases RUNNING.
        if not reconcile_on_startup or jobs.queue is None:
            return
        from osintizada.jobs.recovery import JobRecoveryService

        try:
            JobRecoveryService(db, settings, jobs, jobs.queue, redis).reconcile_with_lock()
        except Exception:  # noqa: BLE001
            log.exception("reconciliação na inicialização falhou")

    def require_token(request: Request) -> None:
        expected = branding_env("API_TOKEN")
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

    def describe_or_404(job_id: str) -> dict:
        try:
            return jobs.describe(job_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # --- identidade visual / documentação ---------------------------------------------------------

    def branded_openapi() -> dict:
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, description=app.description,
                                 routes=app.routes)
            schema["info"]["x-logo"] = {"url": "/branding/logo.png", "altText": f"{PRODUCT_NAME}"}
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = branded_openapi

    @app.get("/", include_in_schema=False)
    def home() -> HTMLResponse:
        return HTMLResponse(home_html(__version__))

    @app.get("/docs", include_in_schema=False)
    def swagger_docs() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{PRODUCT_NAME} API — Swagger",
                                   swagger_favicon_url="/branding/icon.png")

    @app.get("/redoc", include_in_schema=False)
    def redoc_docs() -> HTMLResponse:
        return get_redoc_html(openapi_url="/openapi.json", title=f"{PRODUCT_NAME} API — ReDoc",
                              redoc_favicon_url="/branding/icon.png")

    @app.get("/branding/logo.png", include_in_schema=False)
    def brand_logo() -> Response:
        return Response(logo_bytes(LOGO_HEADER), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/branding/icon.png", include_in_schema=False)
    def brand_icon() -> Response:
        return Response(logo_bytes(LOGO_ICON), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    # --- saúde / métricas ----------------------------------------------------------------------

    @app.get("/health")
    def health() -> dict:
        database = database_status(db)
        redis_info = redis_status(redis)
        worker = workers_status(redis, settings.cache.prefix)
        queue: dict[str, Any] = {"status": "QUEUE_UNAVAILABLE"}
        if jobs.queue is not None and redis_info["status"] == "ok":
            try:
                queue = {"status": "ok", "size": jobs.queue.size()}
            except Exception as exc:  # noqa: BLE001
                queue = {"status": "UNAVAILABLE", "error": type(exc).__name__}
        with db.session() as s:
            job_counts = JobRepository(s).count_by_status()
        ok = (database["status"] == "ok" and redis_info["status"] == "ok" and worker["status"] == "ONLINE")
        return {"status": "ok" if ok else "degraded", "product": PRODUCT_NAME,
                "api": {"status": "ok", "name": f"{PRODUCT_NAME} API", "version": __version__},
                "database": database, "redis": redis_info, "worker": worker, "queue": queue, "jobs": job_counts}

    @app.get("/metrics", dependencies=auth)
    def prometheus_metrics() -> PlainTextResponse:
        with db.session() as s:
            counts = JobRepository(s).count_by_status()
        gauges = {f'jobs{{status="{status.value}"}}': counts.get(status.value, 0) for status in JobStatus}
        worker = workers_status(redis, settings.cache.prefix)
        gauges["workers_online"] = sum(1 for w in worker["workers"] if w["status"] == "ONLINE")
        return PlainTextResponse(metrics.render(gauges), media_type="text/plain; version=0.0.4")

    # --- cases ----------------------------------------------------------------------------------

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
            data["jobs"] = [{k: v for k, v in row_to_dict(j).items() if k not in ("checkpoint", "execution_token")}
                            for j in JobRepository(s).list(case_id)]
            data["counts"] = {
                "entities": len(EntityRepository(s).list(case_id)),
                "evidence": len(EvidenceRepository(s).list(case_id)),
                "relationships": len(RelationshipRepository(s).list(case_id)),
                "searches": len(SearchRepository(s).list(case_id)),
            }
            return data

    @app.post("/cases/{case_id}/investigate", status_code=202, dependencies=auth)
    def investigate(case_id: str, body: InvestigationRequest) -> dict:
        """Cria e enfileira um Job. NÃO executa a investigação neste processo."""
        try:
            job = jobs.submit_investigation(case_id, body)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _submission(job)

    @app.get("/cases/{case_id}/jobs", dependencies=auth)
    def case_jobs(case_id: str) -> list[dict]:
        with db.session() as s:
            get_case_or_404(s, case_id)
            ids = [j.id for j in JobRepository(s).list(case_id)]
        return [describe_or_404(i) for i in ids]

    @app.post("/cases/{case_id}/exports", status_code=202, dependencies=auth)
    def create_export(case_id: str, body: ExportCreate) -> dict:
        if body.format.lower() not in EXPORT_FORMATS:
            raise HTTPException(status_code=422, detail=f"Formato deve ser um de {EXPORT_FORMATS}")
        try:
            job = jobs.submit_export(case_id, body.format)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _submission(job)

    def _submission(job: dict) -> dict:
        warnings = []
        if job.get("dispatch") != "QUEUED":
            warnings.append("QUEUE_UNAVAILABLE")
        if workers_status(redis, settings.cache.prefix)["status"] != "ONLINE":
            warnings.append("WORKER_UNAVAILABLE")
        return {"case_id": job["case_id"], "job_id": job["id"], "job_type": job["job_type"],
                "status": job["status"], "warnings": warnings}

    # --- jobs -------------------------------------------------------------------------------------

    @app.get("/jobs/{job_id}", dependencies=auth)
    def get_job(job_id: str) -> dict:
        return describe_or_404(job_id)

    @app.post("/jobs/{job_id}/cancel", dependencies=auth)
    def cancel_job(job_id: str) -> dict:
        try:
            return jobs.cancel(job_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/jobs/{job_id}/retry", status_code=202, dependencies=auth)
    def retry_job(job_id: str) -> dict:
        try:
            return jobs.retry(job_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/jobs/{job_id}/events", dependencies=auth)
    async def job_events(job_id: str, request: Request, poll: float = Query(0.5, ge=0.05, le=10)) -> StreamingResponse:
        """Server-Sent Events a partir da tabela persistente ``job_events`` (retomável via Last-Event-ID)."""
        describe_or_404(job_id)
        start_after = int(request.headers.get("last-event-id") or 0)

        async def stream():
            last, idle = start_after, 0.0
            while True:
                if await request.is_disconnected():
                    break
                with db.session() as s:
                    repo = JobRepository(s)
                    rows = [(e.id, e.event_type, e.timestamp.isoformat(), e.data)
                            for e in repo.events_after(job_id, last)]
                    status = repo.get(job_id).status
                for event_id, kind, ts, data in rows:
                    last = event_id
                    payload = json.dumps({"type": kind, "timestamp": ts, **(data or {})}, ensure_ascii=False)
                    yield f"id: {event_id}\nevent: {kind}\ndata: {payload}\n\n"
                if not rows and JobStatus(status) in TERMINAL_JOB_STATUSES:
                    yield f"event: end\ndata: {json.dumps({'status': status})}\n\n"
                    break
                await asyncio.sleep(poll)
                idle = 0.0 if rows else idle + poll
                if idle >= 15:
                    idle = 0.0
                    yield ": keepalive\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/jobs/{job_id}/download", dependencies=auth)
    def download_export(job_id: str) -> FileResponse:
        job = describe_or_404(job_id)
        if job["job_type"] != JobType.EXPORT.value or job["status"] != JobStatus.COMPLETED.value:
            raise HTTPException(status_code=409, detail="Export ainda não concluído")
        root = Path(settings.jobs.export_dir).resolve()
        path = Path(job["result"].get("path", "")).resolve()
        if root not in path.parents or not path.is_file():  # nunca servir fora do diretório de exports
            raise HTTPException(status_code=404, detail="Arquivo de export não encontrado")
        return FileResponse(path, media_type=job["result"].get("mime"), filename=job["result"].get("filename"))

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

    @app.post("/cases/{case_id}/entities/{entity_id}/flags", dependencies=auth)
    def set_entity_flags(case_id: str, entity_id: str, body: EntityFlags) -> dict:
        """Marca/desmarca LOW_IDENTITY_VALUE (ex.: logo, meme, avatar padrão). Vale na próxima correlação."""
        with db.session() as s:
            get_case_or_404(s, case_id)
            entity = EntityRepository(s).get(entity_id)
            if entity is None or entity.case_id != case_id:
                raise HTTPException(status_code=404, detail="Entidade não encontrada")
            meta = dict(entity.meta or {})
            flags = dict(meta.get("flags") or {})
            if body.low_identity_value:
                flags["low_identity_value"] = {"reason": body.reason, "source": "manual"}
            else:
                flags.pop("low_identity_value", None)
            meta["flags"] = flags
            entity.meta = meta
            AuditRepository(s).log(case_id, AuditEvent.ENTITY_MERGED, "API",
                                   f"LOW_IDENTITY_VALUE={body.low_identity_value} em {entity.type}",
                                   {"entity_id": entity_id, "reason": body.reason})
            return row_to_dict(entity)

    @app.get("/cases/{case_id}/timeline", dependencies=auth)
    def timeline(case_id: str, entity_type: EntityType | None = None, provider: str | None = None,
                 date_from: str | None = None, date_to: str | None = None,
                 include_collection_only: bool = False) -> list[dict]:
        with db.session() as s:
            get_case_or_404(s, case_id)
        try:
            events = TimelineService(db).build(case_id, entity_type.value if entity_type else None, provider,
                                               date_from, date_to, include_collection_only)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Data inválida: {exc}") from exc
        return [e.model_dump() for e in events]

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
            "missing": p.missing_secrets(),  # só NOMES de variáveis, nunca valores
            "requires_auth": p.requires_auth, "credentials": p.masked_credentials(),
            "supports": "all" if p.consumes_planned_queries else sorted(t.value for t in p.supported_identifiers),
            "rate_limit_per_minute": p.rate_limit_per_minute, "concurrency": p.max_concurrency,
            "cache_ttl_seconds": p.cache_ttl,
        } for p in provider_list()]

    @app.post("/providers/{name}/validate-credentials", dependencies=auth)
    async def validate_credentials(name: str) -> dict:
        """Validação manual (pode consumir quota do serviço externo)."""
        provider = next((p for p in provider_list() if p.name == name), None)
        if provider is None:
            raise HTTPException(status_code=404, detail="Provider não encontrado")
        return await provider.validate_credentials()

    @app.get("/providers/health", dependencies=auth)
    async def providers_health() -> list[dict]:
        runtime = service.orchestrator.runtime
        checks = await asyncio.gather(*(p.healthcheck() for p in provider_list()), return_exceptions=True)
        out = []
        for provider, check in zip(provider_list(), checks, strict=True):
            last_error = runtime.last_errors.get(provider.name)
            if isinstance(check, BaseException):
                out.append({"name": provider.name, "configured": provider.is_configured(), "status": "UNAVAILABLE",
                            "last_error": last_error, "latency_ms": None, "detail": str(check)[:300],
                            "missing": provider.missing_secrets()})
                continue
            status = HEALTH_STATUS.get(check.status, "DEGRADED")
            if status == "HEALTHY" and (last_error or check.circuit != "closed"):
                status = "DEGRADED"  # responde agora, mas falhou recentemente / circuito não fechado
            out.append({"name": provider.name, "configured": check.configured, "status": status,
                        "last_error": last_error, "latency_ms": check.latency_ms, "detail": check.detail,
                        "circuit": check.circuit, "missing": provider.missing_secrets()})
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
