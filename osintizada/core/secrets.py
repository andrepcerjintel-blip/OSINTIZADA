"""Gestão mínima de secrets e sanitização de logs.

Secrets vêm exclusivamente de variáveis de ambiente (ou ``.env`` fora do Git,
carregado pelo ambiente). Nunca devem ser logados, exibidos por completo ou
enviados a modelos de IA.
"""

from __future__ import annotations

import logging
import os
import re

_REDACTED = "****"


def get_secret(env_var: str) -> str | None:
    value = os.environ.get(env_var)
    return value.strip() if value and value.strip() else None


def mask_secret(value: str | None) -> str:
    """Exibe apenas prefixo curto e 4 últimos caracteres: ``sk-****37ad``."""
    if not value:
        return "NOT CONFIGURED"
    if len(value) <= 8:
        return _REDACTED
    prefix = value.split("-", 1)[0] + "-" if "-" in value[:6] else value[:2]
    return f"{prefix}{_REDACTED}{value[-4:]}"


_SECRET_PATTERNS = [
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|authorization|bearer)\s*[=:]\s*(?:bearer\s+)?)(\S+)"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]{8,})"),
    re.compile(r"((?:[?&])(?:key|apikey|api_key|token|access_token)=)([^&\s]+)"),
]


def sanitize(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + _REDACTED, text)
    return text


class SecretRedactingFilter(logging.Filter):
    """Filtro de logging que remove secrets de mensagens."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = sanitize(str(record.getMessage()))
        record.args = ()
        return True
