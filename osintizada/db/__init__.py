"""Persistência: schema SQLAlchemy, sessão e migrações Alembic."""

from osintizada.db.session import Database
from osintizada.db.tables import Base

__all__ = ["Base", "Database"]
