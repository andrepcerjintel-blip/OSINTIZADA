"""CLI do OSINTIZADA (Fase 1).

Comandos:
  detect <input>             hipóteses de tipo + normalização
  plan <input> [--mode]      consultas planejadas com prioridade e motivo
  raw "<consulta>"           registra uma RAW SEARCH (sem executar)
  run <input>... [--mode]    executa providers disponíveis (depth 0)
  providers                  status/configuração dos providers
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from osintizada import __version__
from osintizada.config import get_settings
from osintizada.core.enums import IdentifierType, SearchMode
from osintizada.core.identifiers import IdentifierEngine
from osintizada.core.normalization import normalize
from osintizada.core.query_planner import QueryPlanner
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


def cmd_run(args: argparse.Namespace) -> int:
    idents = _identifiers(args.input, args.type, 0.3)
    orchestrator = SourceOrchestrator()
    run = asyncio.run(orchestrator.run(idents, mode=SearchMode(args.mode),
                                       allowed=set(args.providers) if args.providers else None))
    if args.json:
        payload = run.model_dump(mode="json")
        payload["search_log"] = [e.model_dump(mode="json") for e in run.search_log]
        _dump(payload)
        return 0
    print("SEEDS:")
    for s in run.seeds:
        print(f"  [SEED] {s.type.value}: {s.value}")
    print("\nEXECUÇÃO:")
    for entry in run.search_log:
        err = f" — {'; '.join(entry.errors)}" if entry.errors else ""
        print(f"  {entry.provider:<32} {entry.status.value:<15} {entry.results} resultado(s){err}")
    print("\nENTIDADES:")
    for e in run.entities:
        print(f"  [{e.origin.value}] {e.type.value}: {e.value}  (evidências: {len(e.evidence_ids)})")
    if not run.entities:
        print("  (nenhuma)")
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    settings = get_settings()
    registry = load_builtin_providers()
    rows = []
    for p in registry.create_all(settings):
        rows.append({
            "name": p.name,
            "type": p.provider_type.value,
            "tier": p.effective_tier,
            "tag": p.source_tag,
            "enabled": p.enabled,
            "status": "CONNECTED" if p.is_configured() else "NOT CONFIGURED",
            "credentials": p.masked_credentials(),
            "supports": sorted(t.value for t in p.supported_identifiers),
        })
    if args.json:
        _dump(rows)
        return 0
    for r in rows:
        state = r["status"] if r["enabled"] else "DISABLED"
        print(f"{r['name']:<32} {state:<15} tier {r['tier']}  [{r['tag']}]  {', '.join(r['supports'])}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="osintizada", description="OSINT Investigation Orchestrator")
    parser.add_argument("--version", action="version", version=f"osintizada {__version__}")
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

    p = sub.add_parser("providers", help="Lista providers e status de configuração")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_providers)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
