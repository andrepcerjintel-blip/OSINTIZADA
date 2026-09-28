"""Pipeline de extração: executa todos os extractors sobre um texto.

  * ordem por prioridade; spans "consumidos" (URL, email, CPF...) não são
    reaproveitados por extractors genéricos (domínio, telefone, menção);
  * deduplica por (tipo, valor), contando ocorrências;
  * converte extrações em ``ProviderResult`` com contexto preservado em ``raw``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from osintizada.core.enums import DataClassification, RelationType
from osintizada.core.models import ProviderResult
from osintizada.extractors.base import BaseExtractor, Extraction
from osintizada.extractors.contact import EmailExtractor, PhoneExtractor
from osintizada.extractors.crypto import CryptoExtractor, HashExtractor
from osintizada.extractors.documents import CNPJExtractor, CPFExtractor
from osintizada.extractors.network import ASNExtractor, DomainExtractor, IPExtractor, URLExtractor
from osintizada.extractors.social import MentionExtractor, SocialProfileExtractor, TelegramExtractor


def default_extractors() -> list[BaseExtractor]:
    return [
        URLExtractor(), TelegramExtractor(), SocialProfileExtractor(), EmailExtractor(), CryptoExtractor(),
        CPFExtractor(), CNPJExtractor(), IPExtractor(), ASNExtractor(), DomainExtractor(), PhoneExtractor(),
        HashExtractor(), MentionExtractor(),
    ]


def _overlaps(start: int, end: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(start < e and s < end for s, e in spans)


class ExtractionPipeline:
    def __init__(self, extractors: Iterable[BaseExtractor] | None = None) -> None:
        self.extractors = sorted(extractors or default_extractors(), key=lambda e: e.priority)

    def extract(self, text: str, exclude_values: Iterable[str] = ()) -> list[Extraction]:
        if not text:
            return []
        excluded = {v.lower() for v in exclude_values if v}
        consumed: list[tuple[int, int]] = []
        found: dict[tuple, Extraction] = {}
        for extractor in self.extractors:
            new_spans: list[tuple[int, int]] = []
            for item in extractor.extract(text):
                if extractor.respect_consumed and _overlaps(item.start, item.end, consumed):
                    continue
                if extractor.consumes_span:
                    new_spans.append((item.start, item.end))
                if item.value.lower() in excluded:
                    continue
                existing = found.get(item.key)
                if existing is None:
                    item.metadata["occurrences"] = 1
                    found[item.key] = item
                else:
                    existing.metadata["occurrences"] += 1
                    if item.confidence > existing.confidence:
                        existing.confidence = item.confidence
            consumed.extend(new_spans)
        return sorted(found.values(), key=lambda e: (e.start, e.type.value))


def extractions_to_results(
    extractions: Iterable[Extraction],
    *,
    source_url: str | None,
    source_name: str,
    observed_at: datetime | None = None,
    is_historical: bool = False,
    confidence_factor: float = 0.6,
    classification: DataClassification = DataClassification.PUBLIC,
    relation: RelationType = RelationType.ASSOCIATED_WITH,
    reason: str = "Co-ocorrência no mesmo conteúdo (hipótese; não indica identidade)",
) -> list[ProviderResult]:
    """Converte extrações em resultados de provider, preservando o contexto original."""
    results = []
    for ex in extractions:
        results.append(ProviderResult(
            type=ex.type,
            value=ex.value,
            source_url=source_url,
            source_name=source_name,
            observed_at=observed_at,
            is_historical=is_historical,
            confidence=round(min(1.0, ex.confidence * confidence_factor), 2),
            classification=classification,
            relation_to_query=relation,
            relation_reason=reason,
            raw={"extractor": ex.extractor, "matched": ex.raw, "context": ex.context, **ex.metadata},
        ))
    return results
