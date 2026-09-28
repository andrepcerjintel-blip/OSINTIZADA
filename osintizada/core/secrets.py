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


def load_dotenv(path: str | os.PathLike[str] = ".env") -> list[str]:
    """Carrega ``CHAVE=valor`` de um arquivo .env local para o ambiente.

    Variáveis já definidas no ambiente têm precedência e nunca são sobrescritas.
    Retorna apenas os NOMES carregados (nunca os valores).
    """
    loaded: list[str] = []
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return loaded
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


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
