"""Tipos da camada de IA: tarefas, modos, estados, saídas validadas e resultado com proveniência."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

AI_DISCLAIMER = ("Gerado por IA a partir das evidências indicadas. É interpretação (DERIVED), não evidência: "
                 "não confirma identidade nem substitui a análise do investigador.")


class AITask(StrEnum):
    CLASSIFY_CONTENT = "CLASSIFY_CONTENT"
    EXTRACT_ENTITIES = "EXTRACT_ENTITIES"
    SUMMARIZE = "SUMMARIZE"
    TRANSLATE = "TRANSLATE"
    GENERATE_QUERIES = "GENERATE_QUERIES"
    ASSESS_RELEVANCE = "ASSESS_RELEVANCE"


# Chave de roteamento (ai.routing) de cada tarefa. "final_report"/"complex_analysis" são rotas que o
# serviço pode pedir explicitamente (ex.: síntese final de um Case grande).
ROUTE_KEYS: dict[AITask, str] = {
    AITask.CLASSIFY_CONTENT: "classify",
    AITask.EXTRACT_ENTITIES: "extract",
    AITask.SUMMARIZE: "summarize",
    AITask.TRANSLATE: "translate",
    AITask.GENERATE_QUERIES: "query_generation",
    AITask.ASSESS_RELEVANCE: "relevance",
}


class AIMode(StrEnum):
    LOCAL_ONLY = "LOCAL_ONLY"
    HYBRID = "HYBRID"
    CLOUD = "CLOUD"


MODE_RANK = {AIMode.LOCAL_ONLY: 0, AIMode.HYBRID: 1, AIMode.CLOUD: 2}


def most_restrictive(*modes: AIMode | str | None) -> AIMode:
    values = [AIMode(str(m).upper()) for m in modes if m]
    return min(values, key=MODE_RANK.__getitem__) if values else AIMode.LOCAL_ONLY


class ProviderKind(StrEnum):
    LOCAL = "LOCAL"
    CLOUD = "CLOUD"


class AIProviderStatus(StrEnum):
    READY = "READY"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    UNAVAILABLE = "UNAVAILABLE"
    DEGRADED = "DEGRADED"      # responde, mas as últimas chamadas falharam


class AIResultStatus(StrEnum):
    OK = "OK"
    AI_UNAVAILABLE = "AI_UNAVAILABLE"        # nenhum provider permitido e disponível
    POLICY_BLOCKED = "POLICY_BLOCKED"        # modo/PrivacyGate impediram (ex.: LOCAL_ONLY sem modelo local)
    CONTENT_TOO_LARGE = "CONTENT_TOO_LARGE"  # item individual maior que o limite de todos os providers
    AI_PARSE_ERROR = "AI_PARSE_ERROR"        # JSON inválido mesmo após reparo, ou fora do esquema
    REFUSED = "REFUSED"
    ERROR = "ERROR"


class AIHealth(BaseModel):
    provider: str
    kind: ProviderKind
    enabled: bool = True
    status: AIProviderStatus
    reachable: bool | None = None
    runtime: str | None = None
    model: str | None = None
    message: str | None = None
    missing: list[str] = Field(default_factory=list)   # NOMES de variáveis ausentes (nunca valores)
    latency_ms: float | None = None


class Usage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_external_usd: float | None = None   # só quando o preço é conhecido; local = 0 (custo externo)
    note: str | None = None


class Completion(BaseModel):
    text: str
    usage: Usage = Field(default_factory=Usage)


# --- saídas validadas por tarefa (sempre em lote: ``items`` com o id do item de entrada) ------------


class ClassifiedItem(BaseModel):
    id: str
    label: str
    confidence: float = Field(ge=0, le=1)
    reason: str = ""


class ClassificationBatch(BaseModel):
    items: list[ClassifiedItem] = Field(default_factory=list)


class ExtractedEntity(BaseModel):
    type: str = Field(min_length=2, max_length=40)
    value: str = Field(min_length=1, max_length=500)
    confidence: float = Field(default=0.5, ge=0, le=1)      # AI_CONFIDENCE (≠ confiança de correlação)
    context: str = Field(default="", max_length=500)
    source_id: str | None = None


class Extraction(BaseModel):
    entities: list[ExtractedEntity] = Field(default_factory=list)


class Summary(BaseModel):
    summary: str = Field(min_length=1)
    key_points: list[str] = Field(default_factory=list)
    cited_ids: list[str] = Field(default_factory=list)


class TranslatedItem(BaseModel):
    id: str
    source_language: str = ""
    translation: str


class TranslationBatch(BaseModel):
    items: list[TranslatedItem] = Field(default_factory=list)


class QuerySuggestion(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    reason: str = ""
    based_on: list[str] = Field(default_factory=list)


class QuerySuggestions(BaseModel):
    queries: list[QuerySuggestion] = Field(default_factory=list)


class RelevanceItem(BaseModel):
    id: str
    relevant: bool
    score: float = Field(ge=0, le=1)
    reason: str = ""


class RelevanceBatch(BaseModel):
    items: list[RelevanceItem] = Field(default_factory=list)


OUTPUT_MODELS: dict[AITask, type[BaseModel]] = {
    AITask.CLASSIFY_CONTENT: ClassificationBatch,
    AITask.EXTRACT_ENTITIES: Extraction,
    AITask.SUMMARIZE: Summary,
    AITask.TRANSLATE: TranslationBatch,
    AITask.GENERATE_QUERIES: QuerySuggestions,
    AITask.ASSESS_RELEVANCE: RelevanceBatch,
}


class AIAttempt(BaseModel):
    provider: str
    kind: ProviderKind
    status: str          # OK | CACHE_HIT | SKIPPED_<motivo> | <erro>
    detail: str | None = None
    latency_ms: float | None = None


class AIResult(BaseModel):
    task: AITask
    status: AIResultStatus
    mode: AIMode
    route: str | None = None             # local | cloud (rota configurada para a tarefa)
    provider: str | None = None
    provider_kind: ProviderKind | None = None
    model: str | None = None
    output: dict[str, Any] | None = None
    output_hash: str | None = None       # sha256 da saída validada (JSON canônico)
    usage: Usage | None = None
    latency_ms: float | None = None
    prompt_version: str
    cached: bool = False
    fallback_used: bool = False
    reason: str | None = None
    attempts: list[AIAttempt] = Field(default_factory=list)
    classification: str = "DERIVED"
    ai_generated: bool = True
    disclaimer: str = AI_DISCLAIMER
