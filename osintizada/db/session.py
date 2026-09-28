"""Conexão com o banco.

``DATABASE_URL`` (env) > ``database.url`` (config) > SQLite local em ``data/``.
PostgreSQL: ``postgresql+psycopg://usuario:senha@host/banco`` (driver instalado à parte).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from osintizada.db.tables import Base

DEFAULT_SQLITE_URL = "sqlite:///data/osintizada.db"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_database_url(url: str | None = None) -> str:
    if url:
        return url
    if env := os.environ.get("DATABASE_URL"):
        return env
    from osintizada.config import get_settings

    return get_settings().database.url or DEFAULT_SQLITE_URL


class Database:
    def __init__(self, url: str | None = None, echo: bool = False) -> None:
        self.url = resolve_database_url(url)
        kwargs: dict = {"echo": echo, "future": True}
        if self.url.startswith("sqlite"):
            # timeout = busy_timeout: escritas concorrentes (heartbeat × coleta) esperam em vez de falhar.
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if self.url in ("sqlite://", "sqlite:///:memory:"):
                kwargs["poolclass"] = StaticPool  # uma conexão compartilhada: necessária para :memory:
            else:
                path = self.url.split("sqlite:///", 1)[-1]
                if path:
                    Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(self.url, **kwargs)
        if self.url.startswith("sqlite"):
            in_memory = self.url in ("sqlite://", "sqlite:///:memory:")
            event.listen(self.engine, "connect", _sqlite_pragmas if in_memory else _sqlite_file_pragmas)
        self._factory = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        """Cria o schema diretamente (testes/dev). Produção: ``osintizada db upgrade`` (Alembic)."""
        Base.metadata.create_all(self.engine)

    def upgrade(self, revision: str = "head") -> None:
        from alembic import command
        from alembic.config import Config

        ini = PROJECT_ROOT / "alembic.ini"
        cfg = Config(str(ini)) if ini.is_file() else Config()  # pacote instalado não traz o .ini
        cfg.set_main_option("script_location", str(Path(__file__).resolve().parent / "migrations"))
        cfg.set_main_option("sqlalchemy.url", self.url)
        command.upgrade(cfg, revision)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def _sqlite_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - trivial
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _sqlite_file_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - trivial
    """SQLite em arquivo: WAL permite leitores concorrentes (API/SSE) enquanto o worker escreve."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()
