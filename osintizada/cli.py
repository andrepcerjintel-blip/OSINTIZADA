"""CLI do RINO — Plataforma de Investigação OSINT.

Uso: ``rino <comando>`` (o comando legado ``osintizada`` continua disponível como alias).

Comandos:
  detect <input>             hipóteses de tipo + normalização
  plan <input> [--mode]      consultas planejadas com prioridade e motivo
  raw "<consulta>"           registra uma RAW SEARCH (sem executar)
  run <input>... [--mode]    executa providers disponíveis (depth 0)
  search "<consulta>"        executa RAW SEARCH nos mecanismos configurados
  extract [texto|--file]     extrai entidades de texto livre (offline)
  providers [--health]       status/configuração dos providers
  investigate <input>...     investigação persistente em Case (pivôs, correlação, auditoria)
  cases / case <id>          lista Cases / mostra resultado de um Case
  serve                      inicia a API HTTP (FastAPI/uvicorn) — só enfileira jobs
  worker [--burst]           processo worker (fila Redis/RQ, heartbeat, reconciliador)
  reconcile                  executa uma reconciliação de jobs (recupera abandonados, republica)
  db upgrade                 aplica migrações (Alembic)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from osintizada import __version__
from osintizada.branding import FULL_TITLE, PRODUCT_NAME
from osintizada.branding import env as branding_env
from osintizada.config import get_settings
from osintizada.core.enums import IdentifierType, SearchMode
from osintizada.core.identifiers import IdentifierEngine
from osintizada.core.normalization import normalize
from osintizada.core.query_planner import QueryPlanner
from osintizada.core.secrets import load_dotenv
from osintizada.extractors import ExtractionPipeline
from osintizada.orchestration.source_orchestrator import SourceOrchestrator
from osintizada.providers.base import load_builtin_providers


def _dump(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _identifiers(raw_values: list[str], forced_type: str | None, min_conf: float):
    engine = IdentifierEngine()
    result = []
    for raw in raw_values:
        if forced_type:
            result.append(normalize(raw, IdentifierType(forced_type)))
        else:
            analyzed = engine.analyze(raw, min_confidence=min_conf)
            if analyzed:
                result.append(analyzed[0])  # hipótese principal; demais ficam visíveis em `detect`
    return result


def cmd_detect(args: argparse.Namespace) -> int:
    engine = IdentifierEngine()
    detection = engine.detect(args.input)
    normalized = engine.analyze(args.input, min_confidence=args.min_confidence)
    payload = {
        "detection": detection.model_dump(mode="json"),
        "ambiguous": detection.is_ambiguous,
        "normalized": [n.model_dump(mode="json") for n in normalized],
    }
    if args.json:
        _dump(payload)
        return 0
    print(f"INPUT: {args.input}")
    for c in detection.candidates:
        extra = f" → {c.value}" if c.value else ""
        print(f"  {c.confidence:>4.2f}  {c.type.value:<20}{extra}  ({c.reason})")
    if detection.is_ambiguous:
        print("  ! Entrada ambígua: múltiplas hipóteses plausíveis.")
    for n in normalized:
        print(f"\n[{n.type.value}] valor canônico: {n.value}")
        if n.variants:
            print("  variantes de busca: " + " | ".join(n.variants))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    planner = QueryPlanner()
    idents = _identifiers(args.input, args.type, 0.3)
    mode = SearchMode(args.mode)
    queries = planner.plan_many(idents, mode=mode) if len(idents) > 1 else (
        planner.plan(idents[0], mode=mode, max_queries=args.max) if idents else [])
    if args.json:
        _dump([q.model_dump(mode="json") for q in queries])
        return 0
    for q in queries:
        print(f"[{q.priority:>3}] {q.category.value:<14} {q.query}")
        print(f"      reason: {q.reason}")
    print(f"\n{len(queries)} consultas planejadas (modo {mode.value}).")
    return 0


def cmd_raw(args: argparse.Namespace) -> int:
    q = QueryPlanner.raw(args.query)
    _dump(q.model_dump(mode="json"))
    return 0


def _print_run(run, as_json: bool) -> None:
    if as_json:
        payload = run.model_dump(mode="json")
        payload["search_log"] = [e.model_dump(mode="json") for e in run.search_log]
        payload["search_hits"] = [h.model_dump(mode="json") | {"engines": h.engines} for h in run.search_hits()]
        _dump(payload)
        return
    print("SEEDS:")
    for s in run.seeds:
        print(f"  [SEED] {s.type.value}: {s.value}")
    print("\nEXECUÇÃO:")
    for entry in run.search_log:
        err = f" — {'; '.join(entry.errors)}" if entry.errors else ""
        cache = " (cache)" if entry.cache_hit else ""
        query = f"  {entry.query}" if entry.reason else ""
        print(f"  {entry.provider:<28} {entry.status.value:<15} {entry.results:>3} resultado(s){cache}{query}{err}")
    hits = run.search_hits()
    if hits:
        print("\nPÁGINAS (deduplicadas entre mecanismos):")
        for h in hits[:30]:
            flag = "✓" if h.identifier_in_snippet else " "
            print(f"  {flag} [{', '.join(h.engines)}] {h.title or ''}\n      {h.url}")
    print("\nENTIDADES:")
    shown = [e for e in run.entities if e.type.value != "URL"]
    for e in shown:
        print(f"  [{e.origin.value}] {e.type.value}: {e.value}  (evidências: {len(e.evidence_ids)})")
    if not shown:
        print("  (nenhuma)")
    print(f"\nOrçamento usado: {run.budget_spent}")


def cmd_run(args: argparse.Namespace) -> int:
    idents = _identifiers(args.input, args.type, 0.3)
    orchestrator = SourceOrchestrator()
    run = asyncio.run(orchestrator.run(idents, mode=SearchMode(args.mode),
                                       allowed=set(args.providers) if args.providers else None))
    _print_run(run, args.json)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    orchestrator = SourceOrchestrator()
    run = asyncio.run(orchestrator.run_raw(args.query, allowed=set(args.providers) if args.providers else None))
    _print_run(run, args.json)
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    if args.file:
        with open(args.file, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    elif args.text:
        text = " ".join(args.text)
    else:
        text = sys.stdin.read()
    extractions = ExtractionPipeline().extract(text)
    if args.json:
        _dump([e.model_dump(mode="json") for e in extractions])
        return 0
    for e in extractions:
        print(f"  {e.confidence:>4.2f}  {e.type.value:<18} {e.value}")
        print(f"        {e.context}")
    if not extractions:
        print("  (nenhuma entidade encontrada)")
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    settings = get_settings()
    registry = load_builtin_providers()
    providers = registry.create_all(settings)
    health = {}
    if args.health:
        async def check():
            return await asyncio.gather(*(p.healthcheck() for p in providers))
        health = {h.provider: h for h in asyncio.run(check())}
    rows = []
    for p in providers:
        h = health.get(p.name)
        rows.append({
            "name": p.name,
            "type": p.provider_type.value,
            "tier": p.effective_tier,
            "tag": p.source_tag,
            "enabled": p.enabled,
            "status": "CONNECTED" if p.is_configured() else "NOT CONFIGURED",
            "credentials": p.masked_credentials(),
            "supports": "all" if p.consumes_planned_queries else sorted(t.value for t in p.supported_identifiers),
            "health": h.model_dump(mode="json") if h else None,
        })
    if args.json:
        _dump(rows)
        return 0
    for r in rows:
        state = r["status"] if r["enabled"] else "DISABLED"
        supports = r["supports"] if isinstance(r["supports"], str) else ", ".join(r["supports"])
        print(f"{r['name']:<28} {state:<15} tier {r['tier']}  [{r['tag']}]  {supports}")
        if r["health"]:
            h = r["health"]
            latency = f" {h['latency_ms']}ms" if h["latency_ms"] is not None else ""
            print(f"    health: {h['status']}{latency}  circuit: {h['circuit']}  {h['detail'] or ''}")
    return 0


def _service():
    from osintizada.db import Database
    from osintizada.investigation.service import InvestigationService

    db = Database()
    db.upgrade()
    return InvestigationService(db)


def cmd_investigate(args: argparse.Namespace) -> int:
    from osintizada.investigation.service import InputSpec, InvestigationRequest

    service = _service()
    inputs = [InputSpec(value=v, type=IdentifierType(args.type) if args.type else None) for v in args.input]
    request = InvestigationRequest(inputs=inputs, mode=SearchMode(args.mode), max_depth=args.max_depth,
                                   max_entities=args.max_entities, max_pivots=args.max_pivots,
                                   providers=args.providers or None, blocked_values=args.block or [])
    case_id = args.case or service.create_case(args.name or f"Investigação: {', '.join(args.input)[:150]}")
    summary = asyncio.run(service.investigate(case_id, request))
    if args.json:
        _dump({"case_id": case_id, "summary": summary})
        return 0
    print(f"CASE {case_id}")
    _print_case(service, case_id)
    print(f"\nResumo: profundidade {summary['depth_reached']}, {summary['provider_calls']} chamada(s), "
          f"{summary['entities_total']} entidade(s), {summary['pivots_scheduled']} pivô(s), "
          f"{summary['correlations']} correlação(ões), {summary['conflicts']} conflito(s)")
    if summary["budget_exhausted"]:
        print("BUDGET_EXHAUSTED: " + ", ".join(summary["budget_exhausted"]))
    return 0


def _print_case(service, case_id: str) -> None:
    from osintizada.repositories import (
        EntityRepository,
        EvidenceRepository,
        RelationshipRepository,
        SearchRepository,
    )

    with service.db.session() as s:
        entities = {e.id: e for e in EntityRepository(s).list(case_id)}
        ev_count: dict[str, int] = {}
        for ev in EvidenceRepository(s).list(case_id):
            ev_count[ev.entity_id] = ev_count.get(ev.entity_id, 0) + 1
        print("\nENTIDADES:")
        for e in entities.values():
            tag = "[SEED]" if e.origin == "SEED" else f"[d{e.depth}]"
            print(f"  {tag:<7} {e.type:<16} {e.display_value or e.canonical_value}  ({ev_count.get(e.id, 0)} evid.)")
        print("\nRELAÇÕES:")
        for r in RelationshipRepository(s).list(case_id):
            src, tgt = entities.get(r.source_entity_id), entities.get(r.target_entity_id)
            if src and tgt:
                print(f"  {src.canonical_value} —{r.relationship_type}→ {tgt.canonical_value}")
        print("\nBUSCAS:")
        for ex in SearchRepository(s).list(case_id):
            if ex.status in ("SKIPPED",):
                continue
            extra = f" [{ex.error_code}]" if ex.error_code else ""
            cache = " (cache)" if ex.cache_hit else ""
            print(f"  d{ex.depth} {ex.provider:<22} {ex.status:<15} {ex.result_count:>3}  {ex.identifier_value or ex.query}"
                  f"{cache}{extra}")


def cmd_cases(args: argparse.Namespace) -> int:
    from osintizada.repositories import CaseRepository, row_to_dict

    service = _service()
    with service.db.session() as s:
        rows = [row_to_dict(c) for c in CaseRepository(s).list()]
    if args.json:
        _dump(rows)
        return 0
    for c in rows:
        print(f"{c['id']}  {c['status']:<10} {c['created_at'][:19]}  {c['name']}")
    return 0


def cmd_case(args: argparse.Namespace) -> int:
    service = _service()
    _print_case(service, args.case_id)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from osintizada.observability import configure_logging

    configure_logging(args.log_level or "INFO")
    if args.host not in ("127.0.0.1", "localhost", "::1") and not branding_env("API_TOKEN"):
        print("erro: para expor a API do RINO fora de localhost defina RINO_API_TOKEN", file=sys.stderr)
        return 2
    uvicorn.run("osintizada.api.app:create_app", factory=True, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from osintizada.bootstrap import StartupError, validate_database, validate_redis
    from osintizada.db import Database
    from osintizada.infrastructure.redis_client import RedisNotConfigured, create_redis
    from osintizada.jobs.worker import build_worker_context, run_worker
    from osintizada.observability import configure_logging

    configure_logging(args.log_level or "INFO")
    try:
        redis = create_redis()
        validate_redis(redis)
        db = Database()
        validate_database(db, auto_migrate=branding_env("AUTO_MIGRATE", "1") != "0")
    except (RedisNotConfigured, StartupError) as exc:
        # Sem Redis não existe fila persistente: o worker se recusa a fingir que funciona.
        print(f"erro: {exc}", file=sys.stderr)
        return 2
    ctx = build_worker_context(redis=redis, db=db)
    ctx.queue_redis = create_redis(blocking=True)  # escuta da fila sem timeout de leitura
    run_worker(ctx=ctx, burst=args.burst)
    return 0


def cmd_worker_status(args: argparse.Namespace) -> int:
    """Status dos workers pelo heartbeat no Redis (usado também como healthcheck do container)."""
    import socket

    from osintizada.config import get_settings
    from osintizada.infrastructure.redis_client import RedisNotConfigured, create_redis
    from osintizada.jobs.heartbeat import workers_status

    try:
        redis = create_redis()
    except RedisNotConfigured:
        redis = None
    status = workers_status(redis, get_settings().cache.prefix)
    if args.local:  # só os workers deste host/container (worker_id começa pelo hostname)
        host = socket.gethostname() + "-"
        status["workers"] = [w for w in status["workers"] if str(w.get("worker_id", "")).startswith(host)]
        online = any(w["status"] == "ONLINE" for w in status["workers"])
        status["status"] = "ONLINE" if online else ("STALE" if status["workers"] else "OFFLINE")
    _dump(status)
    return 0 if (not args.require_online or status["status"] == "ONLINE") else 1


def cmd_reconcile(args: argparse.Namespace) -> int:
    from osintizada.db import Database
    from osintizada.jobs.recovery import JobRecoveryService
    from osintizada.jobs.worker import build_worker_context

    ctx = build_worker_context(db=Database())
    report = JobRecoveryService(ctx.db, ctx.settings, ctx.jobs, ctx.queue, ctx.redis).reconcile()
    _dump(report)
    return 0


def cmd_db(args: argparse.Namespace) -> int:
    from osintizada.db import Database

    db = Database()
    db.upgrade()
    print(f"Migrações aplicadas em {db.url.split('@')[-1]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rino", description=FULL_TITLE,
        epilog="Variáveis de ambiente usam o prefixo RINO_ (o prefixo legado OSINTIZADA_ também é aceito).")
    parser.add_argument("--version", action="version", version=f"{PRODUCT_NAME} {__version__}")
    parser.add_argument("--log-level", default=None, help="Logs estruturados (JSON) em stderr: DEBUG, INFO, WARNING")
    sub = parser.add_subparsers(dest="command", required=True)
    modes = [m.value for m in SearchMode if m != SearchMode.RAW]
    types = [t.value for t in IdentifierType]

    p = sub.add_parser("detect", help="Detecta e normaliza um identificador")
    p.add_argument("input")
    p.add_argument("--min-confidence", type=float, default=0.3)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_detect)

    p = sub.add_parser("plan", help="Planeja consultas para um ou mais identificadores")
    p.add_argument("input", nargs="+")
    p.add_argument("--mode", choices=modes, default=SearchMode.DEEP.value)
    p.add_argument("--type", choices=types, help="Força o tipo (seleção manual)")
    p.add_argument("--max", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("raw", help="Registra uma consulta manual (RAW SEARCH)")
    p.add_argument("query")
    p.set_defaults(func=cmd_raw)

    p = sub.add_parser("run", help="Executa os providers disponíveis (depth 0)")
    p.add_argument("input", nargs="+")
    p.add_argument("--mode", choices=modes, default=SearchMode.QUICK.value)
    p.add_argument("--type", choices=types)
    p.add_argument("--providers", nargs="*", help="Restringe aos providers informados")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("search", help="Executa uma RAW SEARCH nos mecanismos configurados")
    p.add_argument("query")
    p.add_argument("--providers", nargs="*")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("extract", help="Extrai entidades de texto (argumento, --file ou stdin)")
    p.add_argument("text", nargs="*")
    p.add_argument("--file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("investigate", help="Investigação persistente (Case, pivôs, correlação, auditoria)")
    p.add_argument("input", nargs="+")
    p.add_argument("--mode", choices=modes, default=SearchMode.DEEP.value)
    p.add_argument("--type", choices=types, help="Força o tipo de todos os inputs")
    p.add_argument("--max-depth", type=int)
    p.add_argument("--max-entities", type=int)
    p.add_argument("--max-pivots", type=int)
    p.add_argument("--providers", nargs="*")
    p.add_argument("--block", nargs="*", help="Valores/domínios que não devem ser pivotados")
    p.add_argument("--case", help="Case existente (padrão: cria um novo)")
    p.add_argument("--name", help="Nome do novo Case")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_investigate)

    p = sub.add_parser("cases", help="Lista Cases")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_cases)

    p = sub.add_parser("case", help="Mostra entidades, relações e buscas de um Case")
    p.add_argument("case_id")
    p.set_defaults(func=cmd_case)

    p = sub.add_parser("serve", help="Inicia a API HTTP")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("worker", help="Inicia um worker de jobs (requer REDIS_URL)")
    p.add_argument("--burst", action="store_true", help="Processa a fila até esvaziar e encerra")
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("worker-status", help="Workers ONLINE/STALE/OFFLINE (heartbeat no Redis)")
    p.add_argument("--local", action="store_true", help="Somente workers deste host/container")
    p.add_argument("--require-online", action="store_true", help="Código de saída 1 se não houver worker ONLINE")
    p.set_defaults(func=cmd_worker_status)

    p = sub.add_parser("reconcile", help="Reconcilia jobs (abandonados, PENDING, QUEUED perdidos)")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("db", help="Banco de dados")
    p.add_argument("action", choices=["upgrade"])
    p.set_defaults(func=cmd_db)

    p = sub.add_parser("providers", help="Lista providers e status de configuração")
    p.add_argument("--health", action="store_true", help="Executa healthcheck (pode consumir quota)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_providers)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(branding_env("ENV_FILE", ".env"))
    if args.log_level:
        from osintizada.observability import configure_logging

        configure_logging(args.log_level)
    try:
        return args.func(args)
    except (ValueError, LookupError, RuntimeError) as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
