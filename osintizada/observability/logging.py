"""Logs estruturados.

Cada linha é um JSON com ``timestamp``, ``level``, ``logger``, ``message`` e os
campos de contexto (``case_id``, ``provider``, ``operation``, ``status``,
``duration_ms``...). O ``case_id`` é propagado por ``contextvars``, então logs
emitidos dentro de providers já saem associados ao Case em andamento.
Tudo passa por ``SecretRedactingFilter``.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from osintizada.core.secrets import SecretRedactingFilter, sanitize_payload

_case_id: ContextVar[str | None] = ContextVar("osintizada_case_id", default=None)
_job_id: ContextVar[str | None] = ContextVar("osintizada_job_id", default=None)
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


def current_case_id() -> str | None:
    return _case_id.get()


@contextmanager
def case_context(case_id: str | None) -> Iterator[None]:
    token = _case_id.set(case_id)
    try:
        yield
    finally:
        _case_id.reset(token)


@contextmanager
def job_context(job_id: str | None, case_id: str | None = None) -> Iterator[None]:
    job_token = _job_id.set(job_id)
    case_token = _case_id.set(case_id) if case_id else None
    try:
        yield
    finally:
        _job_id.reset(job_token)
        if case_token is not None:
            _case_id.reset(case_token)


class _CaseFilter(logging.Filter):
    """Injeta case_id/job_id do contexto em todo log (correlação de investigação)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "case_id", None) is None:
            record.case_id = _case_id.get()
        if getattr(record, "job_id", None) is None:
            record.job_id = _job_id.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and not key.startswith("_") and value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(sanitize_payload(payload), ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", json_output: bool = True, stream=None) -> None:
    root = logging.getLogger("osintizada")
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if json_output else logging.Formatter("%(levelname)s %(name)s %(message)s"))
    handler.addFilter(_CaseFilter())
    handler.addFilter(SecretRedactingFilter())
    root.addHandler(handler)
    root.propagate = False


def get_logger(name: str) -> logging.LoggerAdapter | logging.Logger:
    return logging.getLogger(name if name.startswith("osintizada") else f"osintizada.{name}")
