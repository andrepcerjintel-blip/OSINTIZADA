"""Base dos Entity Extractors.

Um extractor recebe texto livre (snippet, página, mensagem, documento) e
devolve ``Extraction``s com posição, contexto e confiança. Extractors são
determinísticos (regex + validação); extração semântica (nomes, locais)
ficará a cargo da camada de IA, sempre marcada como DERIVED.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from osintizada.core.enums import EntityType, IdentifierType

CONTEXT_CHARS = 60


class Extraction(BaseModel):
    type: EntityType
    identifier_type: IdentifierType | None = None
    value: str  # valor normalizado
    raw: str  # trecho exatamente como apareceu
    start: int
    end: int
    context: str
    confidence: float = Field(ge=0.0, le=1.0)
    extractor: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> tuple[EntityType, str]:
        return (self.type, self.value)


def context_window(text: str, start: int, end: int, size: int = CONTEXT_CHARS) -> str:
    left = max(0, start - size)
    right = min(len(text), end + size)
    snippet = " ".join(text[left:right].split())
    return ("…" if left > 0 else "") + snippet + ("…" if right < len(text) else "")


class BaseExtractor(ABC):
    name: ClassVar[str]
    produces: ClassVar[frozenset[EntityType]]
    # Ordem de execução: menor primeiro. Spans "consumidos" por extractors
    # anteriores são ignorados por extractors com ``respect_consumed``.
    priority: ClassVar[int] = 50
    consumes_span: ClassVar[bool] = False
    respect_consumed: ClassVar[bool] = False

    @abstractmethod
    def extract(self, text: str) -> Iterable[Extraction]: ...

    def make(self, text: str, start: int, end: int, type_: EntityType, value: str, confidence: float,
             identifier_type: IdentifierType | None = None, **metadata: Any) -> Extraction:
        return Extraction(
            type=type_, identifier_type=identifier_type, value=value, raw=text[start:end], start=start, end=end,
            context=context_window(text, start, end), confidence=round(confidence, 2), extractor=self.name,
            metadata=metadata,
        )
