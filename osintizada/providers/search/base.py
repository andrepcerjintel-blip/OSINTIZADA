"""Base para providers de mecanismos de busca.

Um SearchEngineProvider executa consultas textuais (geradas pelo QueryPlanner
ou RAW SEARCH) e converte cada resultado em:
  * um resultado URL (a página), com título/snippet/rank preservados em ``raw``;
  * entidades extraídas de título+snippet (co-ocorrência, baixa confiança).

Cada mecanismo declara os operadores que suporta; consultas com operadores
não suportados não são enviadas (SKIPPED), em vez de serem silenciosamente
alteradas.
"""

from __future__ import annotations

import html
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from osintizada.core.enums import EntityType, IdentifierType, RelationType, SourceTier
from osintizada.core.models import NormalizedIdentifier, ProviderResult
from osintizada.core.query_builder import SUPPORTED_OPERATORS, detect_operators, quote
from osintizada.core.urls import canonical_url
from osintizada.extractors import ExtractionPipeline, extractions_to_results
from osintizada.net.http_client import SafeHTTPClient
from osintizada.providers.base import APIProvider, SkippedError

_TAGS = re.compile(r"<[^>]+>")


def clean_snippet(text: str | None) -> str:
    """Remove marcação HTML de destaque (<strong>, <b>) e entidades."""
    return " ".join(html.unescape(_TAGS.sub("", text or "")).split())


def parse_date(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class SearchHit(BaseModel):
    url: str
    title: str = ""
    snippet: str = ""
    rank: int
    engine: str
    published_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class SearchEngineProvider(APIProvider, ABC):
    tier = SourceTier.TIER_3
    source_tag = "WEB"
    cost_category = "web"
    consumes_planned_queries = True
    # Consultas textuais valem para qualquer tipo de identificador.
    supported_identifiers = frozenset(IdentifierType)
    supported_operators: ClassVar[frozenset[str]] = frozenset(SUPPORTED_OPERATORS)
    max_results_per_query: ClassVar[int] = 10
    default_cache_ttl_seconds = 6 * 3600

    _pipeline = ExtractionPipeline()

    def unsupported_operators(self, query: str) -> list[str]:
        return sorted(set(detect_operators(query)) - self.supported_operators)

    def supports_query(self, query: str) -> bool:
        return not self.unsupported_operators(query)

    @abstractmethod
    async def execute_query(self, client: SafeHTTPClient, query: str) -> list[SearchHit]:
        """Executa a consulta no mecanismo e devolve os hits na ordem do ranking."""

    async def _search(self, identifier: NormalizedIdentifier, query: str | None) -> list[ProviderResult]:
        text = query or quote(identifier.value)
        if unsupported := self.unsupported_operators(text):
            raise SkippedError(f"Operadores não suportados por {self.name}: {', '.join(unsupported)}")
        async with self.http_client() as client:
            hits = await self.execute_query(client, text)
        return self.hits_to_results(hits, identifier, text)

    def hits_to_results(self, hits: list[SearchHit], identifier: NormalizedIdentifier,
                        query: str) -> list[ProviderResult]:
        needles = {v.lower() for v in [identifier.value, *identifier.variants] if len(v) >= 3}
        results: list[ProviderResult] = []
        for hit in hits:
            haystack = f"{hit.title}\n{hit.snippet}"
            mentioned = any(n in haystack.lower() for n in needles)
            raw = {"engine": hit.engine, "rank": hit.rank, "query": query, "title": hit.title,
                   "snippet": hit.snippet, "identifier_in_snippet": mentioned, "api_item": hit.raw}
            if hit.published_at:  # data de publicação informada pela fonte (≠ data da coleta)
                raw["published_at"] = hit.published_at.isoformat()
            results.append(ProviderResult(
                type=EntityType.URL,
                value=canonical_url(hit.url),
                source_url=hit.url,
                source_name=self.display_name or self.name,
                title=hit.title or None,
                snippet=hit.snippet or None,
                observed_at=hit.published_at,
                raw=raw,
                # Confiança de que a página é relevante: maior se o identificador aparece no snippet.
                confidence=0.7 if mentioned else 0.4,
                classification=self.classification,
                relation_to_query=RelationType.MENTIONED_IN,
                relation_reason=(
                    f"Página retornada por {hit.engine} (posição {hit.rank}) para a consulta {query}"
                    + ("; identificador presente no snippet" if mentioned else "; identificador não visível no snippet")
                ),
            ))
            extractions = self._pipeline.extract(haystack, exclude_values=needles)
            results.extend(extractions_to_results(
                extractions, source_url=hit.url, source_name=self.display_name or self.name,
                observed_at=hit.published_at, classification=self.classification,
                reason=f"Co-ocorre no snippet de {hit.url} (hipótese; não indica identidade)",
            ))
        return results
