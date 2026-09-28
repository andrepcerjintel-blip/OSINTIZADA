"""Modelos de domínio (Pydantic v2).

Estes modelos formam os contratos entre Identifier Engine, providers,
Evidence Engine e (futuramente) Pivot/Correlation Engine e persistência.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from osintizada.core.enums import (
    DataClassification,
    EntityOrigin,
    EntityType,
    IdentifierType,
    ProviderStatus,
    RelationType,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


def entity_fingerprint(entity_type: EntityType | str, value: str) -> str:
    """Impressão digital estável de uma entidade (tipo + valor normalizado)."""
    return f"{EntityType(entity_type).value}:{value}"


def social_account_value(platform: str, handle: str) -> str:
    """Valor canônico de SOCIAL_ACCOUNT: ``plataforma:handle`` (handle em lowercase, exceto YouTube)."""
    handle = handle.lstrip("@")
    return f"{platform}:{handle if platform == 'youtube' else handle.lower()}"


def entity_id_for(entity_type: EntityType | str, value: str) -> str:
    return hashlib.sha1(entity_fingerprint(entity_type, value).encode()).hexdigest()[:20]


class _Model(BaseModel):
    model_config = ConfigDict(use_enum_values=False, populate_by_name=True)


# --- Identificadores ---------------------------------------------------------


class IdentifierCandidate(_Model):
    type: IdentifierType
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    value: str | None = None  # valor extraído, quando difere do input (ex.: handle de URL)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DetectionResult(_Model):
    input: str
    candidates: list[IdentifierCandidate] = Field(default_factory=list)

    @property
    def primary(self) -> IdentifierCandidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def is_ambiguous(self) -> bool:
        return len(self.candidates) > 1 and self.candidates[1].confidence >= 0.5


class NormalizedIdentifier(_Model):
    original: str
    type: IdentifierType
    value: str  # forma canônica
    variants: list[str] = Field(default_factory=list)  # apenas para pesquisa
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def entity_type(self) -> EntityType:
        from osintizada.core.enums import IDENTIFIER_TO_ENTITY

        return IDENTIFIER_TO_ENTITY[self.type]

    @property
    def entity_value(self) -> str:
        """Valor usado na entidade (contas sociais ganham prefixo de plataforma)."""
        from osintizada.core.enums import IDENTIFIER_PLATFORM

        platform = IDENTIFIER_PLATFORM.get(self.type)
        return social_account_value(platform, self.value) if platform else self.value


# --- Entidades e relações ----------------------------------------------------


class Entity(_Model):
    id: str = ""
    type: EntityType
    value: str
    display_value: str | None = None
    origin: EntityOrigin = EntityOrigin.DISCOVERED
    classification: DataClassification = DataClassification.PUBLIC
    identifier_type: IdentifierType | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    depth: int = 0
    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)

    def model_post_init(self, __context: Any) -> None:
        if not self.id:
            self.id = entity_id_for(self.type, self.value)

    @property
    def fingerprint(self) -> str:
        return entity_fingerprint(self.type, self.value)

    @property
    def is_seed(self) -> bool:
        return self.origin == EntityOrigin.SEED


class Relationship(_Model):
    """Relação entre entidades. Nunca existe sem motivo e evidência."""

    id: str = Field(default_factory=new_id)
    source_id: str
    target_id: str
    type: RelationType
    reason: str = Field(min_length=3)
    evidence_ids: list[str] = Field(min_length=1)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    classification: DataClassification = DataClassification.PUBLIC
    created_at: datetime = Field(default_factory=utcnow)


# --- Providers ---------------------------------------------------------------


class ProviderResult(_Model):
    """Item individual devolvido por um provider."""

    type: EntityType
    value: str
    source_url: str | None = None
    source_name: str | None = None
    title: str | None = None
    snippet: str | None = None
    collected_at: datetime = Field(default_factory=utcnow)
    observed_at: datetime | None = None  # timestamp do conteúdo (≠ coleta)
    is_historical: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)
    # Confiança do *provider* no dado (≠ confiança de identidade).
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    classification: DataClassification = DataClassification.PUBLIC
    relation_to_query: RelationType | None = None
    relation_reason: str | None = None


class ProviderResponse(_Model):
    provider: str
    query: str
    identifier_type: IdentifierType | None = None
    status: ProviderStatus
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    results: list[ProviderResult] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)

    @property
    def duration_ms(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds() * 1000


# --- Evidência ---------------------------------------------------------------


class Evidence(_Model):
    evidence_id: str = Field(default_factory=new_id)
    case_id: str | None = None
    provider: str
    source: str | None = None
    source_url: str | None = None
    canonical_url: str | None = None
    query: str
    collected_at: datetime
    observed_at: datetime | None = None
    is_historical: bool = False
    raw_data: dict[str, Any] = Field(default_factory=dict)
    normalized_value: str
    entity_type: EntityType
    entity_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    classification: DataClassification
    content_hash: str
    fingerprint: str
    # Outros providers que devolveram a mesma evidência (não contam como
    # evidências independentes).
    duplicate_sightings: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("normalized_value")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("normalized_value não pode ser vazio")
        return v
