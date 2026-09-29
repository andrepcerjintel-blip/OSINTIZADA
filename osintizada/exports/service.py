"""ExportService — exportação de um Case (a lógica NÃO vive nas rotas).

* JSON: pacote completo com IDs preservados (reconstruível);
* CSV: um arquivo por tipo de dado dentro de um .zip (entities, evidence, relationships,
  timeline, searches, seeds) — nada de grafo "achatado" num CSV só;
* HTML: relatório investigativo; toda conclusão derivada (relações, correlações,
  conflitos) aponta para as evidências que a sustentam.
Exports grandes rodam como Job (``POST /cases/{id}/exports``).
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from osintizada import __version__
from osintizada.branding import COLORS, PRODUCT_NAME, TAGLINE, logo_data_uri
from osintizada.config import Settings, get_settings
from osintizada.core.enums import AuditEvent
from osintizada.db import Database
from osintizada.db.tables import utcnow
from osintizada.repositories import (
    AIAnnotationRepository,
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
from osintizada.timeline.service import TimelineService

LIMITATIONS = [
    "Correlações são hipóteses pontuadas por regras determinísticas; SAME_AS exige sinal forte e nunca decorre só de nome, username ou avatar.",
    "Dados HISTORICAL_DATA (arquivos, certificados expirados) não descrevem a situação atual.",
    "Providers NOT_CONFIGURED não foram consultados; ausência de resultado (EMPTY) ≠ falha (FAILED).",
    "Datas da timeline são datas do fato (observed_at); a data de coleta aparece separadamente.",
]


class ExportService:
    def __init__(self, db: Database, settings: Settings | None = None) -> None:
        self.db = db
        self.settings = settings or get_settings()

    # --- pacote -----------------------------------------------------------------------------------

    def bundle(self, case_id: str) -> dict[str, Any]:
        with self.db.session() as s:
            case = CaseRepository(s).get(case_id)
            if case is None:
                raise LookupError(f"Case {case_id} não encontrado")
            rels = RelationshipRepository(s)
            searches = [row_to_dict(x) for x in SearchRepository(s).list(case_id)]
            audit = AuditRepository(s).list(case_id)
            provider_status: dict[str, Counter] = {}
            for x in searches:
                provider_status.setdefault(x["provider"], Counter())[x["status"]] += 1
            data = {
                # "osintizada.case" = identificador legado da MESMA estrutura (exports anteriores ao RINO).
                "format": "rino.case", "format_version": 1, "format_aliases": ["osintizada.case"],
                "product": PRODUCT_NAME, "generator": f"{PRODUCT_NAME} {__version__}",
                "generated_at": utcnow().isoformat(),
                "case": row_to_dict(case),
                "seeds": [row_to_dict(i) for i in CaseRepository(s).inputs(case_id)],
                "investigations": [row_to_dict(i) for i in InvestigationRepository(s).list(case_id)],
                "entities": [row_to_dict(e) for e in EntityRepository(s).list(case_id)],
                "relationships": [row_to_dict(r) | {"evidence_ids": rels.evidence_ids(r.id)} for r in rels.list(case_id)],
                "evidence": [row_to_dict(e) for e in EvidenceRepository(s).list(case_id)],
                "searches": searches,
                "pivots": [row_to_dict(p) for p in PivotRepository(s).list(case_id)],
                "correlations": [row_to_dict(c) for c in CorrelationRepository(s).list(case_id)],
                "conflicts": [row_to_dict(c) for c in ConflictRepository(s).list(case_id)],
                "audit_summary": {
                    "total_events": len(audit),
                    "by_type": dict(Counter(a.event_type for a in audit)),
                    "first": audit[0].timestamp.isoformat() if audit else None,
                    "last": audit[-1].timestamp.isoformat() if audit else None,
                },
                "provider_status": {k: dict(v) for k, v in provider_status.items()},
                # Interpretações de IA: separadas das evidências e marcadas como tal.
                "ai_annotations": [row_to_dict(a) | {"ai_generated": True}
                                   for a in AIAnnotationRepository(s).list(case_id, limit=1000)],
                "limitations": LIMITATIONS,
            }
        data["timeline"] = [e.model_dump() for e in TimelineService(self.db).build(case_id)]
        return data

    # --- formatos ---------------------------------------------------------------------------------

    @staticmethod
    def to_json(bundle: dict) -> bytes:
        return json.dumps(bundle, ensure_ascii=False, indent=2, default=str).encode()

    @staticmethod
    def to_csv_zip(bundle: dict) -> bytes:
        tables = {
            "entities.csv": bundle["entities"], "evidence.csv": bundle["evidence"],
            "relationships.csv": bundle["relationships"], "timeline.csv": bundle["timeline"],
            "searches.csv": bundle["searches"], "seeds.csv": bundle["seeds"],
            "ai_annotations.csv": bundle.get("ai_annotations", []),
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, rows in tables.items():
                out = io.StringIO()
                columns = sorted({k for row in rows for k in row}) if rows else ["id"]
                writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow({k: json.dumps(v, ensure_ascii=False, default=str)
                                     if isinstance(v, (dict, list)) else _csv_safe(v) for k, v in row.items()})
                zf.writestr(name, out.getvalue())
        return buf.getvalue()

    @staticmethod
    def to_html(bundle: dict) -> str:
        e = html.escape
        entities = {x["id"]: x for x in bundle["entities"]}

        def ent(entity_id: str) -> str:
            x = entities.get(entity_id)
            return f'<a href="#ent-{e(entity_id)}">{e(x["type"])} {e(x["canonical_value"])}</a>' if x else e(entity_id)

        def evlinks(ids: list[str]) -> str:
            return ", ".join(f'<a href="#ev-{e(i)}">{e(i[:8])}</a>' for i in ids) or "—"

        case = bundle["case"]
        c = COLORS
        parts = [f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{PRODUCT_NAME} — Relatório — {e(case['name'])}</title>
<style>body{{font-family:system-ui,sans-serif;margin:0;color:#1f2328;background:#fff}}
main{{margin:0 auto;padding:1.5rem 2rem;max-width:1200px}}
header.brand{{background:{c['ink']};color:{c['paper']};border-bottom:3px solid {c['accent']};
padding:1rem 2rem;display:flex;align-items:center;gap:1rem}}
header.brand img{{width:64px;height:64px;border-radius:10px;background:#fff}}
header.brand .name{{font-size:1.6rem;font-weight:800;letter-spacing:.15em}}
header.brand .brand-tag{{color:#aeb7c2;font-size:.9rem}}
h1{{margin-top:.5rem}}h2{{border-bottom:2px solid {c['accent']};padding-bottom:.2rem}}
a{{color:#0b6fd6}}
table{{border-collapse:collapse;width:100%;margin:1rem 0;font-size:.9rem}}th,td{{border:1px solid #d0d7de;padding:.35rem;text-align:left;vertical-align:top}}
th{{background:{c['graphite']};color:{c['paper']}}}code{{font-size:.85em}}.tag{{padding:0 .3rem;border-radius:3px;background:#eef}}
@media print{{header.brand{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}}}</style></head><body>
<header class="brand"><img src="{logo_data_uri()}" alt="Logo {PRODUCT_NAME}">
<div><div class="name">{PRODUCT_NAME}</div><div class="brand-tag">{e(TAGLINE)}</div></div></header><main>
<h1>Relatório investigativo — {e(case['name'])}</h1>
<p>Gerado em {e(bundle['generated_at'])} por {e(bundle['generator'])}. Case <code>{e(case['id'])}</code>, status {e(case['status'])}.</p>"""]
        parts.append("<h2>Case Summary</h2><table>" + "".join(
            f"<tr><th>{e(k)}</th><td>{e(str(v))}</td></tr>" for k, v in (
                ("Entidades", len(bundle["entities"])), ("Evidências", len(bundle["evidence"])),
                ("Relações", len(bundle["relationships"])), ("Buscas", len(bundle["searches"])),
                ("Correlações", len(bundle["correlations"])), ("Conflitos", len(bundle["conflicts"])),
                ("Descrição", case.get("description") or "—"))) + "</table>")
        parts.append("<h2>Seeds</h2><table><tr><th>Input</th><th>Tipo</th><th>Origem</th><th>Registrado</th></tr>" + "".join(
            f"<tr><td>{e(x['raw_input'])}</td><td>{e(x['identifier_type'])}</td><td>{e(x['source_type'])}</td>"
            f"<td>{e(x['created_at'])}</td></tr>" for x in bundle["seeds"]) + "</table>")
        parts.append("<h2>Entities</h2><table><tr><th>Tipo</th><th>Valor</th><th>Origem</th><th>Depth</th><th>Confiança</th></tr>" + "".join(
            f'<tr id="ent-{e(x["id"])}"><td>{e(x["type"])}</td><td>{e(x["display_value"] or x["canonical_value"])}</td>'
            f'<td>{e(x["origin"])}</td><td>{x["depth"]}</td><td>{x["confidence"]:.2f}</td></tr>' for x in bundle["entities"]) + "</table>")
        parts.append("<h2>Relationships</h2><table><tr><th>Origem</th><th>Relação</th><th>Destino</th><th>Motivo</th><th>Evidências</th></tr>" + "".join(
            f"<tr><td>{ent(r['source_entity_id'])}</td><td><span class=tag>{e(r['relationship_type'])}</span></td>"
            f"<td>{ent(r['target_entity_id'])}</td><td>{e(r['reason'])}</td><td>{evlinks(r['evidence_ids'])}</td></tr>"
            for r in bundle["relationships"]) + "</table>")
        if bundle["correlations"]:
            parts.append("<h3>Correlações (hipóteses)</h3><table><tr><th>A</th><th>B</th><th>Score</th><th>Nível</th><th>Sinais</th><th>Evidências</th></tr>" + "".join(
                f"<tr><td>{ent(c['entity_a_id'])}</td><td>{ent(c['entity_b_id'])}</td><td>{c['score']}</td><td>{e(c['level'])}</td>"
                f"<td>{e('; '.join(str(sg.get('signal')) + ' ' + str(sg.get('weight')) for sg in c['positive_signals'] + c['negative_signals']))}</td>"
                f"<td>{evlinks(c['evidence_ids'])}</td></tr>" for c in bundle["correlations"]) + "</table>")
        if bundle["conflicts"]:
            parts.append("<h3>Evidências conflitantes</h3><table><tr><th>Entidade</th><th>Atributo</th><th>Valores observados</th></tr>" + "".join(
                f"<tr><td>{ent(c['entity_id'])}</td><td>{e(c['attribute'])}</td><td>"
                + "; ".join(f"{e(str(o.get('value')))} ({e(str(o.get('provider')))}, {evlinks([o.get('evidence_id', '')])})"
                            for o in c["observations"]) + "</td></tr>" for c in bundle["conflicts"]) + "</table>")
        parts.append("<h2>Timeline</h2><p>Datas do fato (observed_at); a data de coleta é mostrada separadamente.</p>"
                     "<table><tr><th>Data do fato</th><th>Base</th><th>Evento</th><th>Entidade</th><th>Temporalidade</th><th>Coletado em</th><th>Evidência</th></tr>" + "".join(
            f"<tr><td>{e(t['event_time'] or '—')}</td><td>{e(t['time_basis'])}</td><td>{e(t['event_type'])}</td>"
            f"<td>{ent(t['entity_id'])}</td><td>{e(t['temporality'])}</td><td>{e(t['collected_at'])}</td><td>{evlinks([t['evidence_id']])}</td></tr>"
            for t in bundle["timeline"]) + "</table>")
        parts.append("<h2>Evidence</h2><table><tr><th>ID</th><th>Entidade</th><th>Provider</th><th>Fonte</th><th>Tipo de fonte</th><th>Consulta</th><th>Coletado</th><th>Hash</th></tr>" + "".join(
            f'<tr id="ev-{e(x["id"])}"><td><code>{e(x["id"][:8])}</code></td><td>{ent(x["entity_id"])}</td><td>{e(x["provider"])}</td>'
            f'<td>{_link(x["source_url"], x["source_name"])}</td><td>{e(x["source_type"])} / {e(x["temporality"])}</td>'
            f'<td>{e(x["query"])}</td><td>{e(x["collected_at"])}</td><td><code>{e(x["content_hash"][:16])}</code></td></tr>'
            for x in bundle["evidence"]) + "</table>")
        ai_rows = bundle.get("ai_annotations") or []
        if ai_rows:
            parts.append("<h2>Análises por IA</h2><p><b>Interpretação gerada por IA, não evidência.</b> Não confirma "
                         "identidade; cada análise indica provider, modelo, modo de privacidade e as evidências usadas.</p>"
                         "<table><tr><th>Data</th><th>Análise</th><th>Status</th><th>Provider / modelo</th><th>Modo</th>"
                         "<th>Resultado</th><th>Evidências usadas</th></tr>" + "".join(
                f"<tr><td>{e(str(a['created_at']))}</td><td>{e(a['operation'])}</td><td>{e(a['status'])}</td>"
                f"<td>{e(str(a.get('provider') or '—'))} / {e(str(a.get('model') or '—'))}</td><td>{e(a['mode'])}</td>"
                f"<td>{_ai_html(a, e)}</td><td>{evlinks((a.get('input_refs') or {}).get('evidence_ids', [])[:20])}</td></tr>"
                for a in ai_rows) + "</table>")
        parts.append("<h2>Provider Status</h2><table><tr><th>Provider</th><th>Execuções por status</th></tr>" + "".join(
            f"<tr><td>{e(p)}</td><td>{e(', '.join(f'{k}: {v}' for k, v in sorted(st.items())))}</td></tr>"
            for p, st in sorted(bundle["provider_status"].items())) + "</table>")
        parts.append("<h2>Search History</h2><table><tr><th>Início</th><th>Provider</th><th>Consulta</th><th>Status</th><th>Resultados</th><th>Erro</th></tr>" + "".join(
            f"<tr><td>{e(str(x['started_at']))}</td><td>{e(x['provider'])}</td><td>{e(x['query'])}</td><td>{e(x['status'])}</td>"
            f"<td>{x['result_count']}</td><td>{e(x['error_code'] or '')}</td></tr>" for x in bundle["searches"]) + "</table>")
        parts.append("<h2>Limitations</h2><ul>" + "".join(f"<li>{e(t)}</li>" for t in bundle["limitations"]) + "</ul></main></body></html>")
        return "\n".join(parts)

    # --- arquivo (usado pelo Job de export) ------------------------------------------------------------

    def render(self, case_id: str, fmt: str) -> tuple[bytes, str, str]:
        bundle = self.bundle(case_id)
        if fmt == "json":
            return self.to_json(bundle), "json", "application/json"
        if fmt == "csv":
            return self.to_csv_zip(bundle), "zip", "application/zip"
        if fmt == "html":
            return self.to_html(bundle).encode(), "html", "text/html"
        raise ValueError(f"Formato não suportado: {fmt}")

    def export_to_file(self, case_id: str, fmt: str, job_id: str) -> dict:
        content, ext, mime = self.render(case_id, fmt)
        directory = Path(self.settings.jobs.export_dir) / case_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{job_id}.{ext}"
        path.write_bytes(content)
        info = {"format": fmt, "path": str(path), "filename": f"rino-{case_id[:8]}.{ext}", "mime": mime,
                "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        with self.db.session() as s:
            AuditRepository(s).log(case_id, AuditEvent.EXPORT_CREATED, "ExportService",
                                   f"Export {fmt.upper()} gerado ({len(content)} bytes)",
                                   {"job_id": job_id, "sha256": info["sha256"]})
        return info


def _csv_safe(value: Any) -> Any:
    """Evita injeção de fórmula ao abrir o CSV em planilhas."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def _link(url: str | None, label: str | None) -> str:
    text = html.escape(label or url or "—")
    if url and url.startswith(("http://", "https://")):
        return f'<a href="{html.escape(url, quote=True)}" rel="noopener noreferrer">{text}</a>'
    return text


def _ai_html(annotation: dict, e) -> str:
    """Resumo legível da saída de uma análise de IA (texto sempre escapado)."""
    out = annotation.get("output") or {}
    if annotation.get("status") not in ("OK", "PARTIAL"):
        return e(annotation.get("reason") or annotation.get("status") or "")
    op = annotation.get("operation")
    if "summary" in out:
        points = "".join(f"<li>{e(str(p))}</li>" for p in out.get("key_points", []))
        return f"{e(out['summary'])}<ul>{points}</ul>"
    if "queries" in out:
        return "<ul>" + "".join(f"<li><code>{e(q['query'])}</code> — {e(q.get('reason', ''))}</li>"
                                for q in out["queries"]) + "</ul>"
    if "candidates" in out:
        return "<ul>" + "".join(f"<li>{e(c['type'])}: {e(c['value'])} (IA {c.get('ai_confidence') or '?'})</li>"
                                for c in out["candidates"]) + "</ul>"
    if op == "relevance":
        stages = out.get("stages") or {}
        head = e(" → ".join(f"{k}: {v}" for k, v in stages.items()))
        return head + "<ul>" + "".join(f"<li>{e(i['id'][:8])}: {i['score']:.2f} — {e(i.get('reason', ''))}</li>"
                                       for i in out.get("items", [])[:20]) + "</ul>"
    if op == "classify":
        return e(", ".join(f"{k}: {v}" for k, v in (out.get("counts") or {}).items()))
    if op == "translate":
        return "<ul>" + "".join(f"<li>[{e(i.get('source_language', ''))}] {e(i['translation'][:300])}</li>"
                                for i in out.get("items", [])[:10]) + "</ul>"
    return e(json.dumps(out, ensure_ascii=False)[:500])
