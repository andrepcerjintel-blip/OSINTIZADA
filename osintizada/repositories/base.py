"""Base dos repositories: encapsulam o acesso ao banco (sem regra investigativa)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Session


class Repository:
    def __init__(self, session: Session) -> None:
        self.session = session


def row_to_dict(row: Any) -> dict[str, Any]:
    """Serializa uma linha ORM (``meta`` → ``metadata``; datas em ISO 8601)."""
    data: dict[str, Any] = {}
    for attr in inspect(row).mapper.column_attrs:
        name = attr.columns[0].name  # nome da coluna ("metadata"), não do atributo ("meta")
        value = getattr(row, attr.key)
        data[name] = value.isoformat() if isinstance(value, datetime) else value
    return data
