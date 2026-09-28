"""Configuração central do OSINTIZADA.

Ordem de precedência:
  1. valores padrão definidos aqui;
  2. arquivo YAML (``OSINTIZADA_CONFIG`` ou ``config/osintizada.yaml``);
  3. overrides explícitos passados em código.

Secrets NUNCA ficam neste arquivo nem no YAML: são lidos de variáveis de
ambiente via ``osintizada.core.secrets``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from osintizada.core.enums import QueryCategory, SearchMode

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "osintizada.yaml"


class SearchSettings(BaseModel):
    max_depth: int = 2
    max_queries: int = 100
    max_entities: int = 500
    max_results_per_provider: int = 50
    concurrency: int = 8
    provider_timeout_seconds: float = 30.0
    depth_priority_penalty: int = 10


class ModeProfile(BaseModel):
    max_depth: int
    max_queries: int
    max_queries_per_identifier: int
    concurrency: int = 8
    max_provider_tier: int = 5
    timeout_seconds: float = 30.0
    query_categories: list[QueryCategory] = Field(default_factory=list)  # vazio = todas
    persist_case: bool = False
    enable_pivots: bool = False


class ProviderSettings(BaseModel):
    enabled: bool = True
    timeout_seconds: float | None = None
    rate_limit_per_minute: int | None = None
    max_concurrency: int | None = None
    tier: int | None = None  # override do tier declarado pelo provider
    cache_ttl_seconds: int | None = None


class QueryBudget(BaseModel):
    costs: dict[str, int] = Field(
        default_factory=lambda: {"local": 0, "web": 1, "api": 2, "browser": 3, "tor": 4, "paid": 5}
    )
    max_cost_per_search: int = 200


class ResilienceSettings(BaseModel):
    max_retries: int = 2                     # re-tentativas para falhas transitórias (5xx, conexão)
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    breaker_failure_threshold: int = 5       # falhas consecutivas para abrir o circuito
    breaker_recovery_seconds: float = 120.0
    default_cooldown_seconds: float = 60.0   # pausa após 429 sem Retry-After
    default_cache_ttl_seconds: int = 3600
    max_response_bytes: int = 5 * 1024 * 1024


class NetworkSettings(BaseModel):
    user_agent: str = "OSINTIZADA/0.2 (+investigation research tool)"


class TorSettings(BaseModel):
    mode: str = "TOR_DISABLED"  # TOR_DISABLED | TOR_PASSIVE | TOR_DEEP_SEARCH
    socks_proxy: str = "socks5h://127.0.0.1:9050"


def _default_modes() -> dict[SearchMode, ModeProfile]:
    C = QueryCategory
    return {
        SearchMode.QUICK: ModeProfile(
            max_depth=0, max_queries=15, max_queries_per_identifier=8, concurrency=6, max_provider_tier=3,
            timeout_seconds=15, query_categories=[C.EXACT, C.SOCIAL, C.TELEGRAM, C.CODE],
        ),
        SearchMode.DEEP: ModeProfile(
            max_depth=1, max_queries=60, max_queries_per_identifier=30, concurrency=8, max_provider_tier=4,
            timeout_seconds=30, enable_pivots=True,
        ),
        SearchMode.INVESTIGATION: ModeProfile(
            max_depth=2, max_queries=100, max_queries_per_identifier=40, concurrency=8, max_provider_tier=4,
            timeout_seconds=45, persist_case=True, enable_pivots=True,
        ),
        SearchMode.DEEP_SWEEP: ModeProfile(
            max_depth=3, max_queries=250, max_queries_per_identifier=80, concurrency=12, max_provider_tier=5,
            timeout_seconds=60, persist_case=True, enable_pivots=True,
        ),
        SearchMode.RAW: ModeProfile(
            max_depth=0, max_queries=1, max_queries_per_identifier=1, concurrency=4, max_provider_tier=3,
            timeout_seconds=30, query_categories=[C.RAW],
        ),
    }


class Settings(BaseModel):
    search: SearchSettings = Field(default_factory=SearchSettings)
    modes: dict[SearchMode, ModeProfile] = Field(default_factory=_default_modes)
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)
    query_budget: QueryBudget = Field(default_factory=QueryBudget)
    tor: TorSettings = Field(default_factory=TorSettings)
    resilience: ResilienceSettings = Field(default_factory=ResilienceSettings)
    network: NetworkSettings = Field(default_factory=NetworkSettings)

    def mode(self, mode: SearchMode) -> ModeProfile:
        return self.modes[mode]

    def provider(self, name: str) -> ProviderSettings:
        return self.providers.get(name, ProviderSettings())


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_settings(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Settings:
    config_path = Path(path or os.environ.get("OSINTIZADA_CONFIG", DEFAULT_CONFIG_PATH))
    data: dict[str, Any] = Settings().model_dump(mode="json")
    if config_path.is_file():
        with config_path.open(encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Configuração inválida em {config_path}: esperado mapeamento YAML")
        data = _deep_merge(data, loaded)
    if overrides:
        data = _deep_merge(data, overrides)
    return Settings.model_validate(data)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
