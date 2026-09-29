"""Identidade do produto: RINO.

O projeto se chamava OSINTIZADA. O nome visível agora é **RINO**; alguns identificadores internos
continuam com o nome legado por compatibilidade (pacote Python ``osintizada``, prefixo de chaves
Redis, nome da fila, prefixo das métricas, banco SQLite padrão) — ver docs/ARCHITECTURE.md.

Variáveis de ambiente: o prefixo oficial é ``RINO_``; o prefixo legado ``OSINTIZADA_`` continua
aceito (``RINO_`` tem precedência).
"""

from __future__ import annotations

import base64
import os
from functools import lru_cache
from pathlib import Path

PRODUCT_NAME = "RINO"
TAGLINE = "Plataforma de Investigação OSINT"
FULL_TITLE = f"{PRODUCT_NAME} — {TAGLINE}"
LEGACY_NAME = "OSINTIZADA"

ENV_PREFIX = "RINO_"
LEGACY_ENV_PREFIX = "OSINTIZADA_"

# Paleta oficial (derivada da logo): grafite/preto + azul elétrico.
COLORS = {
    "ink": "#0d1117",       # preto grafite
    "graphite": "#1f2630",
    "steel": "#5b6573",     # cinza do rinoceronte
    "accent": "#1e90ff",    # azul elétrico
    "accent_glow": "#3fa9ff",
    "paper": "#f5f7fa",
}

ASSETS_DIR = Path(__file__).parent / "assets"
LOGO_HEADER = ASSETS_DIR / "logo-rino-256.png"   # derivado (redimensionado) da logo oficial
LOGO_ICON = ASSETS_DIR / "logo-rino-64.png"
# Logo oficial em resolução original: assets/logo-rino.png (raiz do repositório).


def env(name: str, default: str | None = None) -> str | None:
    """Lê ``RINO_<name>``; se ausente, ``OSINTIZADA_<name>`` (legado)."""
    value = os.environ.get(ENV_PREFIX + name)
    if value is None:
        value = os.environ.get(LEGACY_ENV_PREFIX + name)
    return default if value is None else value


def env_names(name: str) -> str:
    """Texto para mensagens: nome oficial (e legado aceito)."""
    return f"{ENV_PREFIX}{name}"


def user_agent(version: str) -> str:
    major_minor = ".".join(version.split(".")[:2])
    return f"{PRODUCT_NAME}/{major_minor} (+investigation research tool)"


@lru_cache(maxsize=4)
def logo_bytes(path: Path = LOGO_HEADER) -> bytes:
    return path.read_bytes()


def logo_data_uri(path: Path = LOGO_HEADER) -> str:
    """Logo embutida (relatórios HTML autocontidos, sem depender de rede)."""
    return "data:image/png;base64," + base64.b64encode(logo_bytes(path)).decode("ascii")
