"""Validação de inicialização (API e worker).

Obrigatório: banco acessível e migrações em dia. Redis é obrigatório para o WORKER e para a
fila; a API sobe sem Redis, mas reporta QUEUE_UNAVAILABLE/WORKER_UNAVAILABLE (sem fingir fila).
Providers sem credencial NUNCA impedem a subida: ficam NOT_CONFIGURED.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from osintizada.db import Database

log = logging.getLogger("osintizada.bootstrap")


class StartupError(RuntimeError):
    pass


def migration_status(db: Database) -> dict:
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from osintizada.db.session import PROJECT_ROOT

    cfg = Config()
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "osintizada" / "db" / "migrations"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    with db.engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    return {"current": current, "head": head, "up_to_date": current == head}


def database_status(db: Database) -> dict:
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "UNAVAILABLE", "error": type(exc).__name__}
    try:
        migrations = migration_status(db)
    except Exception as exc:  # noqa: BLE001
        return {"status": "ok", "migrations": {"error": type(exc).__name__}}
    return {"status": "ok" if migrations["up_to_date"] else "MIGRATIONS_PENDING", "migrations": migrations}


def validate_database(db: Database, auto_migrate: bool) -> None:
    if auto_migrate:
        db.upgrade()
    status = database_status(db)
    if status["status"] != "ok":
        raise StartupError(f"Banco indisponível ou migrações pendentes: {status}")


def validate_redis(redis) -> None:
    try:
        redis.ping()
    except Exception as exc:  # noqa: BLE001
        raise StartupError(f"Redis indisponível: {type(exc).__name__}") from exc
