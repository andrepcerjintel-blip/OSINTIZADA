"""Repositories: única camada que conversa com o banco."""

from osintizada.repositories.audit import AuditRepository
from osintizada.repositories.base import row_to_dict
from osintizada.repositories.cases import CaseRepository, InvestigationRepository
from osintizada.repositories.entities import EntityRepository
from osintizada.repositories.evidence import EvidenceRepository
from osintizada.repositories.jobs import JobRepository
from osintizada.repositories.pivots import (
    ConflictRepository,
    CorrelationRepository,
    PivotRepository,
)
from osintizada.repositories.relationships import RelationshipRepository
from osintizada.repositories.searches import SearchRepository

__all__ = [
    "AuditRepository", "CaseRepository", "ConflictRepository", "CorrelationRepository", "EntityRepository",
    "EvidenceRepository", "InvestigationRepository", "JobRepository", "PivotRepository", "RelationshipRepository",
    "SearchRepository", "row_to_dict",
]
