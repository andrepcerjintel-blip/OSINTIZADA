"""AIService — análises de IA sobre um Case, com proveniência completa e validação contra os dados.

"CODE PRODUCES EVIDENCE. AI INTERPRETS EVIDENCE."

* Entrada montada a partir de entidades/evidências JÁ persistidas (com ids); o recorte usado fica em
  ``input_refs.coverage``. Conteúdo grande é dividido (batching/chunking) — nunca truncado em silêncio.
* Saída validada por esquema (no provider) e conferida contra a entrada (ids inventados descartados).
* Cada análise vira uma ``ai_annotations`` (classificação DERIVED) com provider, modelo, tarefa, versão
  do prompt, timestamp, ids de evidência de entrada, ``output_hash`` e uso/custo.
* Extração: só valores presentes LITERALMENTE no texto da evidência e aprovados pelo validador
  determinístico (tipos estruturados) viram entidades DERIVED ligadas à evidência ORIGINAL por
  ``DERIVED_FROM`` — marcadas AI_SUGGESTED, com confiança baixa (não pivotam nem correlacionam até
  revisão humana). Nenhuma evidência nova, nenhuma fonte fictícia "Llama".
* Relevância/triagem só PRIORIZA: nenhuma evidência é descartada ou alterada. Consultas sugeridas não
  são executadas (o investigador as envia pelo planejador e pelos orçamentos normais).
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from osintizada.ai.models import AIMode, AIResult, AIResultStatus, AITask, most_restrictive
from osintizada.ai.router import AIRouter, canonical_hash
from osintizada.core.enums import AuditEvent, EntityOrigin, EntityType, IdentifierType, RelationType
from osintizada.db import Database
from osintizada.db.tables import new_uuid
from osintizada.repositories import (
    AIAnnotationRepository,
    AuditRepository,
    CaseRepository,
    EntityRepository,
    EvidenceRepository,
    RelationshipRepository,
    SearchRepository,
    row_to_dict,
)

log = logging.getLogger("osintizada.ai.service")

OPERATIONS: dict[str, AITask] = {
    "summary": AITask.SUMMARIZE,
    "pivots": AITask.GENERATE_QUERIES,
    "extract": AITask.EXTRACT_ENTITIES,
    "relevance": AITask.ASSESS_RELEVANCE,
    "classify": AITask.CLASSIFY_CONTENT,
    "translate": AITask.TRANSLATE,
}
TEXT_TASKS = (AITask.CLASSIFY_CONTENT, AITask.TRANSLATE, AITask.EXTRACT_ENTITIES)
_TEXT_KEYS = ("title", "snippet", "description", "text", "message", "bio", "about", "display_name")
_PROMPT_OVERHEAD = 3_000     # caracteres reservados para instruções/JSON ao montar lotes

# Tipo sugerido pela IA → (tipo de entidade, tipos que o validador determinístico precisa confirmar).
# None = tipo semântico (nome, organização, local): só a checagem literal no texto se aplica.
AI_ENTITY_TYPES: dict[str, tuple[EntityType, set[IdentifierType] | None]] = {
    "EMAIL": (EntityType.EMAIL, {IdentifierType.EMAIL}),
    "PHONE": (EntityType.PHONE, {IdentifierType.PHONE}),
    "DOMAIN": (EntityType.DOMAIN, {IdentifierType.DOMAIN, IdentifierType.SUBDOMAIN, IdentifierType.HOSTNAME}),
    "URL": (EntityType.URL, {IdentifierType.URL}),
    "IP": (EntityType.IP, {IdentifierType.IPV4, IdentifierType.IPV6}),
    "CRYPTO_ADDRESS": (EntityType.CRYPTO_ADDRESS, {IdentifierType.BTC_ADDRESS, IdentifierType.EVM_ADDRESS}),
    "USERNAME": (EntityType.USERNAME, {IdentifierType.USERNAME, IdentifierType.ALIAS, IdentifierType.TELEGRAM_USERNAME}),
    "ALIAS": (EntityType.USERNAME, {IdentifierType.USERNAME, IdentifierType.ALIAS, IdentifierType.TELEGRAM_USERNAME}),
    "PERSON": (EntityType.PERSON, None),
    "ORGANIZATION": (EntityType.ORGANIZATION, None),
    "LOCATION": (EntityType.LOCATION, None),
    "INDICATOR": (EntityType.KEYWORD, None),
}
AI_DERIVED_CONFIDENCE = 0.3   # abaixo do mínimo de pivô: entidade sugerida não dispara coleta sozinha


def _clip(value: Any, size: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= size else text[:size] + "…"


def _evidence_text(raw: dict) -> str:
    parts = [str(raw[k]) for k in _TEXT_KEYS if isinstance(raw.get(k), str | int | float) and str(raw.get(k)).strip()]
    return " | ".join(parts)


class AIService:
    def __init__(self, db: Database, router: AIRouter, max_items: int = 200) -> None:
        self.db = db
        self.router = router
        self.max_items = max_items

    # --- limites de lote (do provider local, que é o padrão) --------------------------------------

    @property
    def _batch_items(self) -> int:
        return min((p.max_batch_items for p in self.router.local), default=20)

    @property
    def _char_budget(self) -> int:
        return max(2_000, min((p.max_input_chars for p in self.router.local), default=16_000) - _PROMPT_OVERHEAD)

    def _batches(self, items: list[dict], text_key: str = "text") -> list[list[dict]]:
        """Lotes de até ``max_batch_items`` itens e ``max_input_chars``; item grande vira pedaços ``id::n``."""
        budget, pieces = self._char_budget, []
        for item in items:
            text = str(item.get(text_key) or "")
            if len(text) <= budget // 2:
                pieces.append(item)
                continue
            step = budget // 2
            for n, start in enumerate(range(0, len(text), step)):
                pieces.append({**item, "id": f"{item['id']}::{n}", text_key: text[start:start + step]})
        batches, current, size = [], [], 0
        for piece in pieces:
            piece_size = len(str(piece))
            if current and (len(current) >= self._batch_items or size + piece_size > budget):
                batches.append(current)
                current, size = [], 0
            current.append(piece)
            size += piece_size
        if current:
            batches.append(current)
        return batches

    # --- contexto do Case ----------------------------------------------------------------------

    def _context(self, case_id: str) -> dict:
        with self.db.session() as s:
            case = CaseRepository(s).get(case_id)
            if case is None:
                raise LookupError(f"Case {case_id} não encontrado")
            case_meta = case.meta or {}
            seeds = [i.normalized_value for i in CaseRepository(s).inputs(case_id)]
            entities = EntityRepository(s).list(case_id)
            evidence = EvidenceRepository(s).list(case_id)
            executed = {(e.query or "").strip().lower() for e in SearchRepository(s).list(case_id)}
            counts = Counter(ev.entity_id for ev in evidence)
            ent_by_id = {e.id: e for e in entities}
            ent_items = sorted(
                ({"id": e.id, "type": e.type, "value": e.canonical_value, "depth": e.depth, "origin": e.origin,
                  "evidence_count": counts.get(e.id, 0)} for e in entities
                 if not ((e.meta or {}).get("ai") or {}).get("review") == "REJECTED"),
                key=lambda x: (x["depth"], -x["evidence_count"]))
            ev_items, private = [], bool(case_meta.get("private"))
            for ev in sorted(evidence, key=lambda x: -x.confidence):
                ent = ent_by_id.get(ev.entity_id)
                private = private or bool((ev.meta or {}).get("private")) or bool(ent and (ent.meta or {}).get("private"))
                ev_items.append({
                    "id": ev.id, "entity_id": ev.entity_id,
                    "entity": f"{ent.type} {ent.canonical_value}" if ent else None,
                    "entity_depth": ent.depth if ent else 0,
                    "provider": ev.provider, "source": ev.source_name or ev.provider, "url": ev.source_url,
                    "observed_at": ev.observed_at.isoformat() if ev.observed_at else None,
                    "text": _evidence_text(ev.raw_data or {})})
            return {"name": case.name, "objective": case.description, "seeds": seeds, "entities": ent_items,
                    "evidence": ev_items, "ai_mode": case_meta.get("ai_mode"), "private": private,
                    "known": {(e.type, e.canonical_value.lower()) for e in entities}, "executed_queries": executed}

    @staticmethod
    def _meta(ctx: dict, items: list[dict]) -> dict:
        """Metadados para o PrivacyGate (não vão ao modelo)."""
        return {"sources": sorted({i.get("provider") for i in items if i.get("provider")}), "private": ctx["private"]}

    # --- execução de uma operação ----------------------------------------------------------------

    async def run_operation(self, case_id: str, operation: str, job_id: str | None = None, **params) -> dict:
        if operation not in OPERATIONS:
            raise ValueError(f"operação de IA desconhecida: {operation} (use {', '.join(OPERATIONS)})")
        ctx = self._context(case_id)
        annotation_id = new_uuid()
        with self.db.session() as s:
            AuditRepository(s).log(case_id, AuditEvent.AI_ANALYSIS_STARTED, "AIService",
                                   f"Análise de IA '{operation}' iniciada",
                                   {"annotation_id": annotation_id, "task": OPERATIONS[operation].value,
                                    "job_id": job_id, "case_ai_mode": ctx["ai_mode"]})
        handler = getattr(self, f"_op_{operation}")
        results, input_refs, output = await handler(ctx, annotation_id=annotation_id, case_id=case_id, **params)
        mode = most_restrictive(self.router.mode, ctx["ai_mode"])
        summary = self._combine(OPERATIONS[operation], results, output, mode)
        with self.db.session() as s:
            row = AIAnnotationRepository(s).add(case_id=case_id, operation=operation, result=summary,
                                                input_refs=input_refs, output=output, job_id=job_id,
                                                annotation_id=annotation_id)
            details = {"annotation_id": row.id, "provider": summary.get("provider"), "model": summary.get("model"),
                       "task": summary["task"], "mode": summary["mode"], "job_id": job_id, "status": summary["status"]}
            audit = AuditRepository(s)
            ok = summary["status"] in ("OK", "PARTIAL", "NO_INPUT")
            audit.log(case_id, AuditEvent.AI_ANALYSIS_COMPLETED if ok else AuditEvent.AI_ANALYSIS_FAILED, "AIService",
                      f"Análise de IA '{operation}': {summary['status']}", details)
            if summary.get("fallback_used"):
                audit.log(case_id, AuditEvent.AI_FALLBACK_USED, "AIService",
                          f"Análise '{operation}' usou provider alternativo", details)
            annotation = row_to_dict(row)
        log.info("análise de IA registrada", extra={"case_id": case_id, "operation": operation,
                                                    "status": summary["status"], "provider": summary.get("provider")})
        return annotation

    @staticmethod
    def _combine(task: AITask, results: list[AIResult], output: dict | None, mode: AIMode) -> dict:
        """Proveniência agregada de uma operação (1+ chamadas)."""
        ok = [r for r in results if r.status == AIResultStatus.OK]
        if not results:
            status, reason = "NO_INPUT", ("nenhum item elegível para esta análise (ex.: evidências sem texto "
                                          "coletado); nenhum modelo foi chamado")
        elif len(ok) == len(results):
            status, reason = "OK", ok[0].reason
        elif ok:
            status, reason = "PARTIAL", f"{len(results) - len(ok)} de {len(results)} lotes falharam"
        else:
            status, reason = results[-1].status.value, results[-1].reason
        primary = Counter((r.provider, r.provider_kind, r.model) for r in ok).most_common(1)
        provider, kind, model = primary[0][0] if primary else (None, None, None)
        tokens_in = sum(r.usage.input_tokens or 0 for r in ok if r.usage)
        tokens_out = sum(r.usage.output_tokens or 0 for r in ok if r.usage)
        costs = [r.usage.cost_external_usd for r in ok if r.usage]
        mode = results[0].mode if results else mode
        return {
            "task": task.value, "status": status, "mode": mode.value if hasattr(mode, "value") else mode,
            "provider": provider, "provider_kind": kind, "model": model,
            "prompt_version": results[0].prompt_version if results else "-",
            "output_hash": canonical_hash(output) if output is not None and status in ("OK", "PARTIAL") else None,
            "usage": {"input_tokens": tokens_in or None, "output_tokens": tokens_out or None,
                      "cost_external_usd": (round(sum(costs), 6) if costs and all(c is not None for c in costs)
                                            else None),
                      "calls": len(results), "cached_calls": sum(1 for r in ok if r.cached)},
            "latency_ms": round(sum(r.latency_ms or 0 for r in ok), 1) or None,
            "fallback_used": any(r.fallback_used for r in ok),
            "reason": reason,
            "attempts": [a.model_dump(mode="json") for r in results for a in r.attempts][:200],
        }

    async def _run(self, ctx: dict, task: AITask, payload: dict, items: list[dict] | None = None,
                   route_key: str | None = None) -> AIResult:
        return await self.router.run(task, payload, case_mode=ctx["ai_mode"], route_key=route_key,
                                     meta=self._meta(ctx, items or ctx["evidence"]))

    # --- operações ---------------------------------------------------------------------------

    async def _op_summary(self, ctx: dict, **_) -> tuple[list[AIResult], dict, dict | None]:
        """Map-reduce: resumos parciais (rota ``summarize``) → síntese final (rota ``final_report``)."""
        ents, evs = ctx["entities"][: self.max_items], ctx["evidence"][: self.max_items]
        compact = [{k: v for k, v in e.items() if k not in ("entity_depth",)} | {"text": _clip(e["text"], 600)}
                   for e in evs]
        allowed = {x["id"] for x in ents} | {x["id"] for x in evs}
        chunks = self._batches(compact) if compact else [[]]
        results: list[AIResult] = []
        if len(chunks) == 1:
            final = await self._run(ctx, AITask.SUMMARIZE, {"data": {"case": ctx["name"], "seeds": ctx["seeds"],
                                                                     "entities": ents, "evidence": chunks[0]}},
                                    evs, route_key="final_report")
            results.append(final)
        else:
            partials = []
            for n, chunk in enumerate(chunks, 1):
                part = await self._run(ctx, AITask.SUMMARIZE, {"data": {"case": ctx["name"], "part": f"{n}/{len(chunks)}",
                                                                        "evidence": chunk}}, chunk)
                results.append(part)
                if part.output:
                    partials.append({"part": n, "summary": part.output["summary"],
                                     "key_points": part.output.get("key_points", []),
                                     "cited_ids": [i for i in part.output.get("cited_ids", []) if i in allowed]})
            final = await self._run(ctx, AITask.SUMMARIZE, {"data": {"case": ctx["name"], "seeds": ctx["seeds"],
                                                                     "entities": ents[:60], "partial_summaries": partials}},
                                    evs, route_key="final_report")
            results.append(final)
        output = None
        if final.output is not None:
            cited = final.output.get("cited_ids", [])
            output = {**final.output, "cited_ids": [i for i in cited if i in allowed],
                      "validation": {"dropped_ids": sorted(set(cited) - allowed)},
                      "map_reduce": {"chunks": len(chunks)}}
        refs = {"entity_ids": [x["id"] for x in ents], "evidence_ids": [x["id"] for x in evs],
                "coverage": {"entities": {"used": len(ents), "total": len(ctx["entities"])},
                             "evidence": {"used": len(evs), "total": len(ctx["evidence"])}}}
        return results, refs, output

    async def _op_pivots(self, ctx: dict, max_queries: int = 10, **_):
        ents = [{k: v for k, v in e.items() if k in ("id", "type", "value", "depth")} for e in ctx["entities"]]
        ents = ents[: self.max_items]
        result = await self._run(ctx, AITask.GENERATE_QUERIES,
                                 {"data": {"seeds": ctx["seeds"], "entities": ents}, "max_queries": max_queries}, [])
        output = None
        if result.output is not None:
            allowed = {x["id"] for x in ents}
            seen, queries, dropped = set(), [], []
            for q in result.output.get("queries", []):
                key = q["query"].strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                dropped += [i for i in q.get("based_on", []) if i not in allowed]
                queries.append({**q, "based_on": [i for i in q.get("based_on", []) if i in allowed],
                                "already_executed": key in ctx["executed_queries"], "executed": False,
                                "ai_suggested": True, "ai_reviewed": False})
            output = {"queries": queries, "validation": {"dropped_ids": sorted(set(dropped))},
                      "note": "Sugestões: nenhuma consulta foi executada. Para usar, envie-a como nova investigação "
                              "(passa pelo planejador e pelos orçamentos)."}
        refs = {"entity_ids": [x["id"] for x in ents],
                "coverage": {"entities": {"used": len(ents), "total": len(ctx["entities"])}}}
        return [result], refs, output

    def _text_items(self, ctx: dict, evidence_ids: list[str] | None = None) -> list[dict]:
        items = [e for e in ctx["evidence"] if e["text"] and (not evidence_ids or e["id"] in evidence_ids)]
        return items[: self.max_items]

    async def _op_extract(self, ctx: dict, annotation_id: str, case_id: str, evidence_ids=None, **_):
        items = self._text_items(ctx, evidence_ids)
        by_id = {x["id"]: x for x in items}
        results, candidates = [], []
        for batch in self._batches([{"id": x["id"], "text": x["text"]} for x in items]):
            res = await self._run(ctx, AITask.EXTRACT_ENTITIES, {"data": batch}, [by_id[b["id"].split("::")[0]]
                                                                             for b in batch])
            results.append(res)
            if res.output:
                texts = {b["id"]: b["text"].lower() for b in batch}
                for cand in res.output.get("entities", []):
                    candidates.append((cand, texts, res))
        kept, discarded, created = [], [], []
        from osintizada.core.identifiers import IdentifierEngine

        engine = IdentifierEngine()
        with self.db.session() as s:
            ents, rels = EntityRepository(s), RelationshipRepository(s)
            for cand, texts, res in candidates:
                value = cand["value"].strip()
                src = cand.get("source_id")
                if src not in texts:
                    src = next((i for i, t in texts.items() if value.lower() in t), None)
                if src is None or value.lower() not in texts[src]:
                    discarded.append({**cand, "reason": "valor não aparece literalmente no texto de origem"})
                    continue
                mapping = AI_ENTITY_TYPES.get(cand["type"].upper())
                if mapping is None:
                    discarded.append({**cand, "reason": f"tipo não suportado: {cand['type']}"})
                    continue
                entity_type, must_match = mapping
                if must_match is not None:
                    detected = {c.type for c in engine.detect(value).candidates if c.confidence >= 0.5}
                    if not detected & must_match:
                        discarded.append({**cand, "reason": "rejeitado pelo validador determinístico"})
                        continue
                evidence = by_id[src.split("::")[0]]
                item = {**cand, "type": entity_type.value, "source_id": evidence["id"], "verified_in_source": True,
                        "ai_confidence": cand.get("confidence"), "ai_suggested": True, "ai_reviewed": False}
                if (entity_type.value, value.lower()) in ctx["known"]:
                    kept.append({**item, "already_known": True})   # coletores determinísticos já tinham achado
                    continue
                row, is_new = ents.upsert(
                    case_id, entity_type, value, origin=EntityOrigin.DERIVED,
                    confidence=AI_DERIVED_CONFIDENCE, depth=evidence["entity_depth"] + 1,
                    metadata={"classification": "DERIVED", "ai": {
                        "suggested": True, "reviewed": False, "review": None, "annotation_id": annotation_id,
                        "provider": res.provider, "model": res.model, "prompt_version": res.prompt_version,
                        "ai_confidence": cand.get("confidence"), "source_evidence_id": evidence["id"],
                        "context": _clip(cand.get("context") or "", 300)}})
                ctx["known"].add((entity_type.value, value.lower()))
                if is_new:
                    rels.upsert(case_id, row.id, evidence["entity_id"], RelationType.DERIVED_FROM,
                                reason=(f"Extraído por IA ({res.provider}/{res.model}) do texto da evidência "
                                        f"{evidence['id'][:8]}; valor conferido literalmente — sugestão pendente de "
                                        "revisão"),
                                evidence_ids=[evidence["id"]], confidence=AI_DERIVED_CONFIDENCE,
                                metadata={"ai_suggested": True, "annotation_id": annotation_id})
                    created.append(row.id)
                kept.append({**item, "already_known": not is_new, "entity_id": row.id})
        output = {"candidates": kept, "discarded": discarded, "created_entity_ids": created,
                  "note": "Entidades DERIVED ligadas à evidência original (DERIVED_FROM), marcadas AI_SUGGESTED. "
                          "Não pivotam nem entram na correlação até revisão humana."} if results else None
        refs = {"evidence_ids": [x["id"] for x in items],
                "coverage": {"evidence_with_text": {"used": len(items),
                                                    "total": sum(1 for x in ctx["evidence"] if x["text"])}}}
        return results, refs, output

    async def _op_relevance(self, ctx: dict, **_):
        """Triagem: filtros determinísticos → relevância local em lote → alto interesse → ambíguos (nuvem opcional)."""
        cfg = self.router.cfg
        seen, stage1 = set(), []
        for e in ctx["evidence"]:
            key = (e["url"] or e["text"]).strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            stage1.append(e)
        stage1 = stage1[: self.max_items]
        by_id = {x["id"]: x for x in stage1}
        results, scored = [], {}
        for batch in self._batches([{"id": x["id"], "entity": x["entity"], "source": x["source"], "url": x["url"],
                                     "text": _clip(x["text"], 800)} for x in stage1]):
            res = await self._run(ctx, AITask.ASSESS_RELEVANCE, {"data": batch, "targets": ctx["seeds"],
                                                                 "objective": ctx["objective"]},
                                  [by_id[b["id"].split("::")[0]] for b in batch])
            results.append(res)
            for it in (res.output or {}).get("items", []):
                base = it["id"].split("::")[0]
                if base in by_id and (base not in scored or it["score"] > scored[base]["score"]):
                    scored[base] = {**it, "id": base, "provider": res.provider}
        low, high = cfg.ambiguous_band
        ambiguous = [i for i in scored.values() if low <= i["score"] <= high]
        reassessed = 0
        if ambiguous and cfg.routing.get("complex_analysis") == "cloud" and \
                self.router.mode != AIMode.LOCAL_ONLY and ctx["ai_mode"] not in ("LOCAL_ONLY",):
            batch = [{"id": i["id"], "text": _clip(by_id[i["id"]]["text"], 800), "url": by_id[i["id"]]["url"]}
                     for i in ambiguous]
            res = await self._run(ctx, AITask.ASSESS_RELEVANCE, {"data": batch, "targets": ctx["seeds"],
                                                                 "objective": ctx["objective"]},
                                  [by_id[i["id"]] for i in ambiguous], route_key="complex_analysis")
            results.append(res)
            for it in (res.output or {}).get("items", []):
                if it["id"] in scored:
                    scored[it["id"]] = {**it, "provider": res.provider, "reassessed": True}
                    reassessed += 1
        items = sorted(({**i, "evidence_id": i["id"], "high_interest": i["score"] >= cfg.relevance_threshold,
                         "ai_suggested": True, "ai_reviewed": False} for i in scored.values()),
                       key=lambda i: -i["score"])
        output = {"items": items, "stages": {
            "input": len(ctx["evidence"]), "after_deterministic_filters": len(stage1), "assessed": len(scored),
            "high_interest": sum(1 for i in items if i["high_interest"]), "ambiguous": len(ambiguous),
            "reassessed_complex": reassessed},
            "note": "Somente priorização: nenhuma evidência foi descartada ou teve a confiança alterada."} \
            if results else None
        refs = {"evidence_ids": [x["id"] for x in stage1],
                "coverage": {"evidence": {"used": len(stage1), "total": len(ctx["evidence"])}}}
        return results, refs, output

    async def _op_classify(self, ctx: dict, labels: list[str] | None = None, **_):
        labels = labels or self.router.cfg.classification_labels
        items = self._text_items(ctx)
        by_id = {x["id"]: x for x in items}
        results, classified = [], {}
        for batch in self._batches([{"id": x["id"], "text": _clip(x["text"], 1200)} for x in items]):
            res = await self._run(ctx, AITask.CLASSIFY_CONTENT, {"data": batch, "labels": labels},
                                  [by_id[b["id"].split("::")[0]] for b in batch])
            results.append(res)
            for it in (res.output or {}).get("items", []):
                base = it["id"].split("::")[0]
                if base in by_id and it["label"] in labels and base not in classified:
                    classified[base] = {**it, "id": base, "evidence_id": base}
        output = {"labels": labels, "items": list(classified.values()),
                  "counts": dict(Counter(i["label"] for i in classified.values()))} if results else None
        refs = {"evidence_ids": [x["id"] for x in items],
                "coverage": {"evidence_with_text": {"used": len(items), "total": sum(1 for x in ctx["evidence"] if x["text"])}}}
        return results, refs, output

    async def _op_translate(self, ctx: dict, target_language: str = "pt", evidence_ids=None, **_):
        items = self._text_items(ctx, evidence_ids)
        by_id = {x["id"]: x for x in items}
        results, parts = [], {}
        for batch in self._batches([{"id": x["id"], "text": x["text"]} for x in items]):
            res = await self._run(ctx, AITask.TRANSLATE, {"data": batch, "target_language": target_language},
                                  [by_id[b["id"].split("::")[0]] for b in batch])
            results.append(res)
            for it in (res.output or {}).get("items", []):
                base, _, n = it["id"].partition("::")
                if base in by_id:
                    parts.setdefault(base, []).append((int(n or 0), it, res))
        translations = []
        for base, pieces in parts.items():
            pieces.sort(key=lambda p: p[0])
            first = pieces[0]
            translations.append({
                "evidence_id": base, "original": by_id[base]["text"],
                "translation": " ".join(p[1]["translation"] for p in pieces),
                "source_language": first[1].get("source_language", ""), "target_language": target_language,
                "provider": first[2].provider, "model": first[2].model, "classification": "DERIVED"})
        output = {"items": translations,
                  "note": "Tradução DERIVED: o texto original permanece na evidência e é repetido aqui."} \
            if results else None
        refs = {"evidence_ids": [x["id"] for x in items]}
        return results, refs, output

    # --- revisão humana ---------------------------------------------------------------------------

    def review_entity(self, case_id: str, entity_id: str, accepted: bool, notes: str | None = None) -> dict:
        """AI_REVIEWED: aceita (passa a valer como entidade DERIVED normal) ou rejeita (fica só no histórico)."""
        with self.db.session() as s:
            ents = EntityRepository(s)
            row = ents.get(entity_id)
            if row is None or row.case_id != case_id:
                raise LookupError("Entidade não encontrada")
            meta = dict(row.meta or {})
            ai = dict(meta.get("ai") or {})
            if not ai.get("suggested"):
                raise ValueError("Entidade não foi sugerida por IA")
            ai.update({"reviewed": True, "review": "ACCEPTED" if accepted else "REJECTED", "review_notes": notes})
            meta["ai"] = ai
            row.meta = meta
            row.confidence = 0.6 if accepted else 0.0   # revisada por humano (não pela confiança da IA)
            AuditRepository(s).log(case_id, AuditEvent.AI_REVIEWED, "Investigador",
                                   f"Sugestão de IA {'aceita' if accepted else 'rejeitada'}: {row.type} {row.canonical_value}",
                                   {"entity_id": entity_id, "annotation_id": ai.get("annotation_id")})
            return row_to_dict(row)

    def review_annotation(self, case_id: str, annotation_id: str, accepted: bool, notes: str | None = None) -> dict:
        with self.db.session() as s:
            repo = AIAnnotationRepository(s)
            row = repo.get(annotation_id)
            if row is None or row.case_id != case_id:
                raise LookupError("Análise não encontrada")
            repo.set_review(annotation_id, "ACCEPTED" if accepted else "REJECTED", notes)
            AuditRepository(s).log(case_id, AuditEvent.AI_REVIEWED, "Investigador",
                                   f"Análise de IA {'aceita' if accepted else 'rejeitada'}", {"annotation_id": annotation_id})
            return row_to_dict(row)

    # --- tarefas avulsas sobre texto (não persistidas) --------------------------------------------

    async def run_text_task(self, task: AITask | str, text: str, *, labels: list[str] | None = None,
                            target_language: str = "pt", preferred: str | None = None) -> dict:
        task = AITask(task)
        if task not in TEXT_TASKS:
            raise ValueError(f"tarefa avulsa não suportada: {task} (use {', '.join(t.value for t in TEXT_TASKS)})")
        if task == AITask.CLASSIFY_CONTENT and not labels:
            raise ValueError("CLASSIFY_CONTENT exige 'labels'")
        payload: dict[str, Any] = {"data": [{"id": "t1", "text": text}], "labels": labels or [],
                                   "target_language": target_language}
        result = await self.router.run(task, payload, preferred=preferred, meta={"sources": [], "private": False})
        data = result.model_dump(mode="json")
        out = data.get("output")
        if out and task == AITask.EXTRACT_ENTITIES:
            low = text.lower()
            out["entities"] = [e for e in out["entities"] if e["value"].strip().lower() in low]
        if out and task == AITask.CLASSIFY_CONTENT:
            out["items"] = [i for i in out["items"] if i["label"] in (labels or [])]
        return data
