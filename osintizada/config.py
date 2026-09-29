"""Configuração central do RINO.

Ordem de precedência:
  1. valores padrão definidos aqui;
  2. arquivo YAML (``RINO_CONFIG`` ou ``config/rino.yaml``; legado: ``OSINTIZADA_CONFIG`` e
     ``config/osintizada.yaml`` continuam aceitos);
  3. overrides explícitos passados em código.

Secrets NUNCA ficam neste arquivo nem no YAML: são lidos de variáveis de
ambiente via ``osintizada.core.secrets``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from osintizada.branding import env as branding_env
from osintizada.core.enums import QueryCategory, SearchMode

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "rino.yaml"
LEGACY_CONFIG_PATH = DEFAULT_CONFIG_PATH.with_name("osintizada.yaml")  # instalações anteriores ao RINO


class SearchSettings(BaseModel):
    max_depth: int = 2
    max_queries: int = 100
    max_entities: int = 500
    max_results_per_provider: int = 50
    concurrency: int = 8
    global_concurrency: int = 10  # teto global de chamadas simultâneas (todas as rodadas/providers)
    hard_max_depth: int = 5       # nenhuma requisição pode ultrapassar esta profundidade
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
    # Orçamentos da investigação inteira (todas as profundidades). Esgotar ≠ erro: BUDGET_EXHAUSTED.
    max_entities: int = 250
    max_pivots: int = 40
    max_provider_calls: int = 300
    max_runtime_seconds: float = 600


class ProviderSettings(BaseModel):
    """Overrides por provider. Aceita nomes curtos: ``concurrency``, ``cache_ttl``."""

    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = True
    timeout_seconds: float | None = None
    rate_limit_per_minute: float | None = None  # 0 desativa o limite padrão do provider
    requests_per_second: float | None = None
    max_concurrency: int | None = Field(default=None, validation_alias=AliasChoices("max_concurrency", "concurrency"))
    tier: int | None = None  # override do tier declarado pelo provider
    cache_ttl_seconds: int | None = Field(default=None, validation_alias=AliasChoices("cache_ttl_seconds", "cache_ttl"))
    max_results: int | None = None


class QueryBudget(BaseModel):
    costs: dict[str, int] = Field(
        default_factory=lambda: {"local": 0, "dns": 0, "web": 1, "api": 1, "archive": 1, "browser": 3, "tor": 4,
                                 "paid": 5}
    )
    max_cost_per_search: int = 200


class ResilienceSettings(BaseModel):
    max_retries: int = 2                     # re-tentativas para falhas transitórias (5xx, conexão)
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    breaker_failure_threshold: int = 5       # falhas consecutivas para abrir o circuito
    breaker_recovery_seconds: float = 120.0
    default_cooldown_seconds: float = 60.0   # pausa após 429 sem Retry-After
    rate_limit_retries: int = 1              # re-tentativas após 429 (com Retry-After/backoff)
    max_rate_limit_wait_seconds: float = 15  # espera maior que isso → não re-tenta, registra RATE_LIMITED
    default_cache_ttl_seconds: int = 3600
    max_response_bytes: int = 5 * 1024 * 1024
    # Com Redis: rate limit e cooldown de 429 compartilhados entre todos os processos (cota é do provider,
    # não do worker). Sem Redis, ou False: limite por processo.
    shared_rate_limit: bool = True


class DatabaseSettings(BaseModel):
    # DATABASE_URL (variável de ambiente) tem precedência. Nunca coloque senha aqui.
    url: str = "sqlite:///data/osintizada.db"


class NetworkSettings(BaseModel):
    user_agent: str = "RINO/0.5 (+investigation research tool)"
    # Portas permitidas para URLs não confiáveis (providers podem declarar outras).
    allowed_ports: list[int] = Field(default_factory=lambda: [80, 443])
    # Redes adicionais proibidas (além de todo endereço não-global da stdlib).
    blocked_networks: list[str] = Field(default_factory=list)
    # "pin": conecta no IP validado (inclusive via proxy HTTP CONNECT);
    # "deny": com proxy que resolve DNS (socks5h), recusa URLs arbitrárias.
    proxy_policy: str = "pin"


class RedisSettings(BaseModel):
    # REDIS_URL (env) tem precedência. Senha/TLS vão na URL (rediss://:senha@host:6380/0) — nunca no YAML.
    url: str | None = None
    # REDIS_CACHE_URL (env, opcional): Redis/DB separado para o cache (ex.: maxmemory-policy allkeys-lru),
    # enquanto fila e locks ficam num Redis com noeviction. Ausente → usa REDIS_URL.
    cache_url: str | None = None
    socket_timeout_seconds: float = 5.0


class CacheSettings(BaseModel):
    backend: str = "memory"  # memory | redis
    prefix: str = "osintizada"
    max_memory_entries: int = 10_000


class JobSettings(BaseModel):
    # Quem executa os Jobs: "redis" (fila RQ + processos `rino worker`), "embedded" (o próprio
    # `rino serve` executa, fila no banco — uma máquina, sem Redis) ou "auto" (redis se REDIS_URL
    # estiver configurada; senão embedded). Variável RINO_EXECUTOR tem precedência.
    executor: str = "auto"
    queue_name: str = "osintizada"
    heartbeat_interval_seconds: float = 15.0   # JOB_HEARTBEAT_INTERVAL
    stale_after_seconds: float = 90.0          # heartbeat mais antigo que isso → INTERRUPTED
    max_attempts: int = 3                      # tentativas do JOB (≠ retry de provider)
    retry_backoff_seconds: float = 30.0        # atraso antes de reexecutar um job que falhou
    pending_dispatch_after_seconds: float = 10.0  # PENDING mais antigo que isso é reenviado à fila
    queued_redispatch_after_seconds: float = 300.0  # QUEUED sem mensagem na fila é reenviado
    # TTL do lock do Case. None → igual a stale_after_seconds: o lock de um worker morto expira no mesmo
    # momento em que o job é considerado abandonado (o lock nunca é liberado por quem não é dono).
    case_lock_ttl_seconds: float | None = None
    case_lock_wait_seconds: float = 5.0
    reconcile_interval_seconds: float = 30.0
    worker_heartbeat_interval_seconds: float = 10.0
    reuse_executions_max_age_hours: float = 24.0  # SearchExecution SUCCESS reaproveitada dentro do Case
    export_dir: str = "data/exports"
    artifact_dir: str = "data/artifacts"   # avatares/imagens (armazenamento por hash)

    @property
    def effective_case_lock_ttl(self) -> float:
        ttl = self.case_lock_ttl_seconds or self.stale_after_seconds
        # Precisa sobreviver a pelo menos 2 heartbeats (a renovação acontece a cada heartbeat).
        return max(ttl, self.heartbeat_interval_seconds * 2 + 1)


class ImageSettings(BaseModel):
    max_bytes: int = 5 * 1024 * 1024
    max_pixels: int = 40_000_000
    # Distância de Hamming (64 bits). Calibrado em testes (recompressão/resize ≤ 4; imagens distintas ≥ 20).
    phash_very_similar: int = 6
    phash_similar: int = 12
    dhash_confirm: int = 12       # dHash precisa concordar para "very_similar"
    generic_reuse_threshold: int = 3  # imagem usada por ≥ N contas no case → LOW_IDENTITY_VALUE
    known_generic_sha256: list[str] = Field(default_factory=list)  # avatares padrão conhecidos


class TorSettings(BaseModel):
    mode: str = "TOR_DISABLED"  # TOR_DISABLED | TOR_PASSIVE | TOR_DEEP_SEARCH
    socks_proxy: str = "socks5h://127.0.0.1:9050"


def _default_modes() -> dict[SearchMode, ModeProfile]:
    C = QueryCategory
    return {
        SearchMode.QUICK: ModeProfile(
            max_depth=1, max_queries=15, max_queries_per_identifier=8, concurrency=6, max_provider_tier=3,
            timeout_seconds=15, query_categories=[C.EXACT, C.SOCIAL, C.TELEGRAM, C.CODE], enable_pivots=True,
            max_entities=100, max_pivots=10, max_provider_calls=60, max_runtime_seconds=120,
        ),
        SearchMode.DEEP: ModeProfile(
            max_depth=2, max_queries=60, max_queries_per_identifier=30, concurrency=8, max_provider_tier=5,
            timeout_seconds=30, enable_pivots=True,
            max_entities=250, max_pivots=40, max_provider_calls=300, max_runtime_seconds=600,
        ),
        SearchMode.INVESTIGATION: ModeProfile(
            max_depth=2, max_queries=100, max_queries_per_identifier=40, concurrency=8, max_provider_tier=5,
            timeout_seconds=45, persist_case=True, enable_pivots=True,
            max_entities=500, max_pivots=60, max_provider_calls=500, max_runtime_seconds=900,
        ),
        SearchMode.DEEP_SWEEP: ModeProfile(
            max_depth=3, max_queries=250, max_queries_per_identifier=80, concurrency=12, max_provider_tier=5,
            timeout_seconds=60, persist_case=True, enable_pivots=True,
            max_entities=1000, max_pivots=150, max_provider_calls=1500, max_runtime_seconds=1800,
        ),
        SearchMode.RAW: ModeProfile(
            max_depth=0, max_queries=1, max_queries_per_identifier=1, concurrency=4, max_provider_tier=3,
            timeout_seconds=30, query_categories=[C.RAW], max_entities=100, max_pivots=0,
            max_provider_calls=10, max_runtime_seconds=60,
        ),
    }


def _default_pivot_priorities() -> dict[str, int]:
    # 3 = HIGH, 2 = MEDIUM, 1 = LOW, 0 = nunca pivotar
    return {
        "EMAIL": 3, "PHONE": 3, "DOMAIN": 3, "IP": 3, "CPF": 3, "CNPJ": 3, "CRYPTO_ADDRESS": 3,
        "USERNAME": 2, "SOCIAL_ACCOUNT": 2, "SUBDOMAIN": 2, "ASN": 2, "TELEGRAM_USER": 2,
        "TELEGRAM_CHANNEL": 2, "TELEGRAM_GROUP": 2, "NETWORK": 1, "URL": 1, "ORGANIZATION": 1,
        "PERSON": 1, "HASH": 1, "CRYPTO_TRANSACTION": 1, "LOCATION": 0, "KEYWORD": 0, "DOCUMENT": 0,
    }


class PivotSettings(BaseModel):
    priorities: dict[str, int] = Field(default_factory=_default_pivot_priorities)
    min_confidence: float = 0.5  # entidades abaixo disso não geram pivô (hipóteses fracas)
    skip_non_public_ips: bool = True
    # Domínios de grandes plataformas/infra: gerariam investigação sobre a plataforma, não sobre o alvo.
    blocklist_domains: list[str] = Field(default_factory=lambda: [
        "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "icloud.com",
        "proton.me", "protonmail.com", "google.com", "github.com", "t.me", "telegram.me", "twitter.com", "x.com",
        "instagram.com", "facebook.com", "youtube.com", "tiktok.com", "reddit.com", "linkedin.com",
        "cloudflare.com", "cloudflare.net", "amazonaws.com", "awsdns.com", "azure.com", "akamai.net",
        "akamaiedge.net", "googleusercontent.com", "iana-servers.net", "web.archive.org", "archive.org",
    ])


class CorrelationSettings(BaseModel):
    weights: dict[str, int] = Field(default_factory=lambda: {
        "same_telegram_id": 60, "same_phone": 50, "same_email": 45, "same_username_rare": 25,
        "same_username_common": 10, "same_domain": 15, "same_name": 5, "same_location": 5,
        "same_exact_avatar": 20, "very_similar_avatar": 12, "similar_avatar": 6,
        "conflicting_country": -15, "conflicting_location": -10,
    })
    # Multiplicador aplicado a sinais de avatar com LOW_IDENTITY_VALUE (logos, memes, avatar padrão…).
    low_identity_avatar_factor: float = 0.25
    strong_signals: list[str] = Field(default_factory=lambda: ["same_telegram_id", "same_phone", "same_email"])
    same_as_threshold: int = 80      # exige também ao menos um sinal forte
    possible_threshold: int = 35
    weak_threshold: int = 10
    conflict_attributes: list[str] = Field(default_factory=lambda: [
        "country", "location", "city", "display_name"])


class Settings(BaseModel):
    search: SearchSettings = Field(default_factory=SearchSettings)
    modes: dict[SearchMode, ModeProfile] = Field(default_factory=_default_modes)
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)
    query_budget: QueryBudget = Field(default_factory=QueryBudget)
    tor: TorSettings = Field(default_factory=TorSettings)
    resilience: ResilienceSettings = Field(default_factory=ResilienceSettings)
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    pivots: PivotSettings = Field(default_factory=PivotSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    jobs: JobSettings = Field(default_factory=JobSettings)
    images: ImageSettings = Field(default_factory=ImageSettings)
    correlation: CorrelationSettings = Field(default_factory=CorrelationSettings)

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
    config_path = Path(path or branding_env("CONFIG", DEFAULT_CONFIG_PATH))
    if path is None and not config_path.is_file() and LEGACY_CONFIG_PATH.is_file():
        config_path = LEGACY_CONFIG_PATH
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
