"""Schema relacional do RINO (SQLAlchemy 2.0).

Portável entre SQLite (desenvolvimento/testes) e PostgreSQL (produção):
somente tipos genéricos (String, Text, JSON, DateTime com timezone).
Regras de negócio NÃO ficam aqui — ver ``osintizada.repositories`` e
``osintizada.investigation``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


def _id() -> Mapped[str]:
    return mapped_column(String(32), primary_key=True, default=new_uuid)


def _case_fk() -> Mapped[str]:
    return mapped_column(String(32), ForeignKey("cases.id", ondelete="CASCADE"), index=True)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow)


class CaseRow(Base):
    __tablename__ = "cases"

    id: Mapped[str] = _id()
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", index=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class InvestigationRow(Base):
    """Uma execução (rodada completa de pivôs) dentro de um Case."""

    __tablename__ = "investigations"

    id: Mapped[str] = _id()
    case_id: Mapped[str] = _case_fk()
    mode: Mapped[str] = mapped_column(String(20))
    max_depth: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")
    started_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    request: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EntityRow(Base):
    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint("case_id", "fingerprint", name="uq_entity_case_fingerprint"),
        Index("ix_entities_case_type", "case_id", "type"),
    )

    id: Mapped[str] = _id()
    case_id: Mapped[str] = _case_fk()
    type: Mapped[str] = mapped_column(String(40))
    canonical_value: Mapped[str] = mapped_column(Text)
    display_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    origin: Mapped[str] = mapped_column(String(20))  # SEED | DISCOVERED | DERIVED | MANUAL
    classification: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class CaseInputRow(Base):
    """SEED: identificador fornecido pelo investigador (nunca confundido com descoberta)."""

    __tablename__ = "case_inputs"

    id: Mapped[str] = _id()
    case_id: Mapped[str] = _case_fk()
    investigation_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("investigations.id"), nullable=True)
    raw_input: Mapped[str] = mapped_column(Text)
    identifier_type: Mapped[str] = mapped_column(String(40))
    normalized_value: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("entities.id"), nullable=True)
    source_type: Mapped[str] = mapped_column(String(20), default="USER_INPUT")
    detection: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _created()


class SearchExecutionRow(Base):
    __tablename__ = "search_executions"

    id: Mapped[str] = _id()
    case_id: Mapped[str] = _case_fk()
    investigation_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("investigations.id"), nullable=True)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    query: Mapped[str] = mapped_column(Text)
    identifier_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    identifier_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class EvidenceRow(Base):
    __tablename__ = "evidence"
    __table_args__ = (UniqueConstraint("case_id", "fingerprint", name="uq_evidence_case_fingerprint"),)

    id: Mapped[str] = _id()
    case_id: Mapped[str] = _case_fk()
    entity_id: Mapped[str] = mapped_column(String(32), ForeignKey("entities.id", ondelete="CASCADE"), index=True)
    search_execution_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("search_executions.id"), nullable=True)
    provider: Mapped[str] = mapped_column(String(80))
    source_type: Mapped[str] = mapped_column(String(20))
    source_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    query: Mapped[str] = mapped_column(Text)
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    normalized_value: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    temporality: Mapped[str] = mapped_column(String(20), default="CURRENT_DATA")
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class RelationshipRow(Base):
    __tablename__ = "relationships"
    __table_args__ = (UniqueConstraint("case_id", "fingerprint", name="uq_relationship_case_fingerprint"),)

    id: Mapped[str] = _id()
    case_id: Mapped[str] = _case_fk()
    source_entity_id: Mapped[str] = mapped_column(String(32), ForeignKey("entities.id", ondelete="CASCADE"),
                                                  index=True)
    target_entity_id: Mapped[str] = mapped_column(String(32), ForeignKey("entities.id", ondelete="CASCADE"),
                                                  index=True)
    relationship_type: Mapped[str] = mapped_column(String(40))
    confidence: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class RelationshipEvidenceRow(Base):
    """Associação N:N — toda relação aponta para ≥ 1 evidência."""

    __tablename__ = "relationship_evidence"

    relationship_id: Mapped[str] = mapped_column(String(32), ForeignKey("relationships.id", ondelete="CASCADE"),
                                                 primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String(32), ForeignKey("evidence.id", ondelete="CASCADE"),
                                             primary_key=True)


class PivotRow(Base):
    __tablename__ = "pivots"

    id: Mapped[str] = _id()
    case_id: Mapped[str] = _case_fk()
    investigation_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("investigations.id"), nullable=True)
    source_entity_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("entities.id"), nullable=True)
    target_entity_id: Mapped[str] = mapped_column(String(32), ForeignKey("entities.id"))
    depth: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    provider_origin: Mapped[str | None] = mapped_column(String(80), nullable=True)
    confidence: Mapped[float] = mapped_column(Float)
    priority: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = _created()


class CorrelationRow(Base):
    __tablename__ = "correlations"
    __table_args__ = (UniqueConstraint("case_id", "entity_a_id", "entity_b_id", name="uq_correlation_pair"),)

    id: Mapped[str] = _id()
    case_id: Mapped[str] = _case_fk()
    entity_a_id: Mapped[str] = mapped_column(String(32), ForeignKey("entities.id", ondelete="CASCADE"))
    entity_b_id: Mapped[str] = mapped_column(String(32), ForeignKey("entities.id", ondelete="CASCADE"))
    score: Mapped[int] = mapped_column(Integer)
    level: Mapped[str] = mapped_column(String(20))
    positive_signals: Mapped[list[Any]] = mapped_column(JSON, default=list)
    negative_signals: Mapped[list[Any]] = mapped_column(JSON, default=list)
    evidence_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    relationship_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("relationships.id"), nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ConflictRow(Base):
    __tablename__ = "conflicts"
    __table_args__ = (UniqueConstraint("case_id", "entity_id", "attribute", name="uq_conflict_attribute"),)

    id: Mapped[str] = _id()
    case_id: Mapped[str] = _case_fk()
    entity_id: Mapped[str] = mapped_column(String(32), ForeignKey("entities.id", ondelete="CASCADE"))
    attribute: Mapped[str] = mapped_column(String(80))
    observations: Mapped[list[Any]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="CONFLICTING_EVIDENCE")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AuditLogRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    case_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("cases.id", ondelete="CASCADE"),
                                                nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    component: Mapped[str] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class JobRow(Base):
    """Execução (≠ Case). Um Case pode ter vários Jobs. O banco é a fonte da verdade; a fila só transporta o id."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_heartbeat", "status", "heartbeat_at"),)

    id: Mapped[str] = _id()
    case_id: Mapped[str] = _case_fk()
    job_type: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    created_at: Mapped[datetime] = _created()
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    execution_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    stage: Mapped[str | None] = mapped_column(String(60), nullable=True)
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    request: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    investigation_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("investigations.id"), nullable=True)
    retry_of: Mapped[str | None] = mapped_column(String(32), ForeignKey("jobs.id"), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class JobAttemptRow(Base):
    """Histórico de tentativas: nada é sobrescrito."""

    __tablename__ = "job_attempts"
    __table_args__ = (UniqueConstraint("job_id", "attempt", name="uq_job_attempt"),)

    id: Mapped[str] = _id()
    job_id: Mapped[str] = mapped_column(String(32), ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    execution_token: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class JobEventRow(Base):
    """Eventos persistentes de progresso (base do SSE)."""

    __tablename__ = "job_events"
    __table_args__ = (Index("ix_job_events_job_id_id", "job_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(32), ForeignKey("jobs.id", ondelete="CASCADE"))
    case_id: Mapped[str] = mapped_column(String(32), ForeignKey("cases.id", ondelete="CASCADE"))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    event_type: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AIAnnotationRow(Base):
    """Interpretação gerada por IA sobre dados do Case — NUNCA evidência.

    Guarda o que foi pedido (operação/tarefa), quem respondeu (provider/modelo/modo), com qual prompt
    (``prompt_version``) e A PARTIR DE QUAIS dados (``input_refs``: ids de evidência/entidade), além das
    tentativas do roteador. Nada aqui altera entidades, evidências ou relações.
    """

    __tablename__ = "ai_annotations"

    id: Mapped[str] = _id()
    case_id: Mapped[str] = _case_fk()
    job_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = _created()
    operation: Mapped[str] = mapped_column(String(40), index=True)
    task: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30))
    mode: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    provider_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(40))
    input_refs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attempts: Mapped[list[Any]] = mapped_column(JSON, default=list)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)   # sha256 da saída validada
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)            # tokens / custo externo estimado
    classification: Mapped[str] = mapped_column(String(20), default="DERIVED")
    # Revisão humana: {"decision": "ACCEPTED"|"REJECTED", "notes": ..., "at": ...}; vazio = AI_SUGGESTED.
    review: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
