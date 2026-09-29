"""Enumerações centrais do RINO.

Todos os vocabulários controlados (tipos de identificador, entidades, relações,
status de provider, classificação de dados) ficam aqui para evitar strings
soltas espalhadas pelo código.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """Enum serializável como string pura (JSON-friendly)."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class IdentifierType(StrEnum):
    # Identidade
    FULL_NAME = "full_name"
    POSSIBLE_NAME = "possible_name"
    USERNAME = "username"
    ALIAS = "alias"
    EMAIL = "email"
    PHONE = "phone"
    CPF = "cpf"
    CNPJ = "cnpj"
    # Internet
    DOMAIN = "domain"
    SUBDOMAIN = "subdomain"
    URL = "url"
    HOSTNAME = "hostname"
    IPV4 = "ipv4"
    IPV6 = "ipv6"
    CIDR = "cidr"
    ASN = "asn"
    ONION = "onion"
    # Redes
    TELEGRAM_USERNAME = "telegram_username"
    TELEGRAM_ID = "telegram_id"
    TELEGRAM_LINK = "telegram_link"
    TWITTER_USERNAME = "twitter_username"
    INSTAGRAM_USERNAME = "instagram_username"
    TIKTOK_USERNAME = "tiktok_username"
    GITHUB_USERNAME = "github_username"
    REDDIT_USERNAME = "reddit_username"
    YOUTUBE_CHANNEL = "youtube_channel"
    FACEBOOK_PROFILE = "facebook_profile"
    SOCIAL_PROFILE = "social_profile"
    # Cripto
    BTC_ADDRESS = "btc_address"
    EVM_ADDRESS = "evm_address"
    TX_HASH = "tx_hash"
    # Arquivos / hashes
    MD5 = "md5"
    SHA1 = "sha1"
    SHA256 = "sha256"
    SHA512 = "sha512"
    # Fallback
    KEYWORD = "keyword"


class EntityType(StrEnum):
    PERSON = "PERSON"
    ORGANIZATION = "ORGANIZATION"
    USERNAME = "USERNAME"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    CPF = "CPF"
    CNPJ = "CNPJ"
    DOMAIN = "DOMAIN"
    SUBDOMAIN = "SUBDOMAIN"
    URL = "URL"
    IP = "IP"
    ASN = "ASN"
    NETWORK = "NETWORK"
    SOCIAL_ACCOUNT = "SOCIAL_ACCOUNT"
    TELEGRAM_USER = "TELEGRAM_USER"
    TELEGRAM_CHANNEL = "TELEGRAM_CHANNEL"
    TELEGRAM_GROUP = "TELEGRAM_GROUP"
    CRYPTO_ADDRESS = "CRYPTO_ADDRESS"
    CRYPTO_TRANSACTION = "CRYPTO_TRANSACTION"
    DOCUMENT = "DOCUMENT"
    LOCATION = "LOCATION"
    IMAGE = "IMAGE"
    FILE = "FILE"
    HASH = "HASH"
    KEYWORD = "KEYWORD"


class RelationType(StrEnum):
    USES = "USES"
    OWNS = "OWNS"
    REGISTERED_TO = "REGISTERED_TO"
    ASSOCIATED_WITH = "ASSOCIATED_WITH"
    MENTIONED_IN = "MENTIONED_IN"
    MEMBER_OF = "MEMBER_OF"
    ADMIN_OF = "ADMIN_OF"
    POSTED = "POSTED"
    FORWARDED_FROM = "FORWARDED_FROM"
    LINKS_TO = "LINKS_TO"
    RESOLVES_TO = "RESOLVES_TO"
    HOSTED_ON = "HOSTED_ON"
    PART_OF = "PART_OF"
    SAME_AS = "SAME_AS"
    POSSIBLY_SAME_AS = "POSSIBLY_SAME_AS"
    CONTACT_OF = "CONTACT_OF"
    USES_EMAIL = "USES_EMAIL"
    USES_PHONE = "USES_PHONE"
    USES_USERNAME = "USES_USERNAME"
    DERIVED_FROM = "DERIVED_FROM"


class ProviderStatus(StrEnum):
    """Status de execução de um provider.

    NO_RESULTS, FAILED e SKIPPED são semanticamente distintos e nunca devem
    ser misturados:
      * NO_RESULTS – o provider respondeu e não encontrou nada;
      * FAILED     – o provider não conseguiu concluir;
      * SKIPPED    – o provider não foi executado.
    """

    SUCCESS = "SUCCESS"
    NO_RESULTS = "NO_RESULTS"
    FAILED = "FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    TIMEOUT = "TIMEOUT"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class ProviderType(StrEnum):
    API = "api"
    HTTP = "http"
    BROWSER = "browser"
    TOR = "tor"
    LOCAL = "local"
    PAID = "paid"


class SourceAccess(StrEnum):
    PUBLIC_SOURCE = "PUBLIC_SOURCE"
    AUTHENTICATED_SOURCE = "AUTHENTICATED_SOURCE"
    PAID_SOURCE = "PAID_SOURCE"
    RESTRICTED_SOURCE = "RESTRICTED_SOURCE"


class DataClassification(StrEnum):
    PUBLIC = "PUBLIC"
    AUTHENTICATED = "AUTHENTICATED"
    PAID = "PAID"
    RESTRICTED = "RESTRICTED"
    MANUAL = "MANUAL"
    DERIVED = "DERIVED"


class EntityOrigin(StrEnum):
    """Distingue input do investigador de descoberta OSINT."""

    SEED = "SEED"
    DISCOVERED = "DISCOVERED"
    DERIVED = "DERIVED"
    MANUAL = "MANUAL"


class SourceTier(int, Enum):
    TIER_1 = 1  # fontes oficiais / APIs estruturadas
    TIER_2 = 2  # serviços OSINT especializados
    TIER_3 = 3  # search engines
    TIER_4 = 4  # browser scraping permitido
    TIER_5 = 5  # archives / Tor


class SearchMode(StrEnum):
    QUICK = "quick"
    DEEP = "deep"
    INVESTIGATION = "investigation"
    DEEP_SWEEP = "deep_sweep"
    RAW = "raw"


class QueryCategory(StrEnum):
    EXACT = "exact"
    CONTEXT = "context"
    SOCIAL = "social"
    TELEGRAM = "telegram"
    CODE = "code"
    PASTE = "paste"
    DOCUMENTS = "documents"
    GOVERNMENT = "government"
    INFRASTRUCTURE = "infrastructure"
    ARCHIVE = "archive"
    CRYPTO = "crypto"
    RAW = "raw"


# Mapeamento identificador -> tipo de entidade.
IDENTIFIER_TO_ENTITY: dict[IdentifierType, EntityType] = {
    IdentifierType.FULL_NAME: EntityType.PERSON,
    IdentifierType.POSSIBLE_NAME: EntityType.PERSON,
    IdentifierType.USERNAME: EntityType.USERNAME,
    IdentifierType.ALIAS: EntityType.USERNAME,
    IdentifierType.EMAIL: EntityType.EMAIL,
    IdentifierType.PHONE: EntityType.PHONE,
    IdentifierType.CPF: EntityType.CPF,
    IdentifierType.CNPJ: EntityType.CNPJ,
    IdentifierType.DOMAIN: EntityType.DOMAIN,
    IdentifierType.SUBDOMAIN: EntityType.SUBDOMAIN,
    IdentifierType.URL: EntityType.URL,
    IdentifierType.HOSTNAME: EntityType.SUBDOMAIN,
    IdentifierType.IPV4: EntityType.IP,
    IdentifierType.IPV6: EntityType.IP,
    IdentifierType.CIDR: EntityType.NETWORK,
    IdentifierType.ASN: EntityType.ASN,
    IdentifierType.ONION: EntityType.DOMAIN,
    IdentifierType.TELEGRAM_USERNAME: EntityType.TELEGRAM_USER,
    IdentifierType.TELEGRAM_ID: EntityType.TELEGRAM_USER,
    IdentifierType.TELEGRAM_LINK: EntityType.URL,
    IdentifierType.TWITTER_USERNAME: EntityType.SOCIAL_ACCOUNT,
    IdentifierType.INSTAGRAM_USERNAME: EntityType.SOCIAL_ACCOUNT,
    IdentifierType.TIKTOK_USERNAME: EntityType.SOCIAL_ACCOUNT,
    IdentifierType.GITHUB_USERNAME: EntityType.SOCIAL_ACCOUNT,
    IdentifierType.REDDIT_USERNAME: EntityType.SOCIAL_ACCOUNT,
    IdentifierType.YOUTUBE_CHANNEL: EntityType.SOCIAL_ACCOUNT,
    IdentifierType.FACEBOOK_PROFILE: EntityType.SOCIAL_ACCOUNT,
    IdentifierType.SOCIAL_PROFILE: EntityType.SOCIAL_ACCOUNT,
    IdentifierType.BTC_ADDRESS: EntityType.CRYPTO_ADDRESS,
    IdentifierType.EVM_ADDRESS: EntityType.CRYPTO_ADDRESS,
    IdentifierType.TX_HASH: EntityType.CRYPTO_TRANSACTION,
    IdentifierType.MD5: EntityType.HASH,
    IdentifierType.SHA1: EntityType.HASH,
    IdentifierType.SHA256: EntityType.HASH,
    IdentifierType.SHA512: EntityType.HASH,
    IdentifierType.KEYWORD: EntityType.KEYWORD,
}


# Plataforma associada a identificadores de redes sociais. Entidades
# SOCIAL_ACCOUNT usam o valor canônico "<plataforma>:<handle>".
IDENTIFIER_PLATFORM: dict[IdentifierType, str] = {
    IdentifierType.TWITTER_USERNAME: "twitter",
    IdentifierType.INSTAGRAM_USERNAME: "instagram",
    IdentifierType.TIKTOK_USERNAME: "tiktok",
    IdentifierType.GITHUB_USERNAME: "github",
    IdentifierType.REDDIT_USERNAME: "reddit",
    IdentifierType.YOUTUBE_CHANNEL: "youtube",
    IdentifierType.FACEBOOK_PROFILE: "facebook",
}


# --- Persistência / investigação ------------------------------------------------


class CaseStatus(StrEnum):
    OPEN = "OPEN"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"


class SearchExecutionStatus(StrEnum):
    """Status persistido de cada consulta. EMPTY (respondeu sem resultados) ≠ FAILED."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    EMPTY = "EMPTY"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


PROVIDER_TO_EXECUTION_STATUS: dict[ProviderStatus, SearchExecutionStatus] = {
    ProviderStatus.SUCCESS: SearchExecutionStatus.SUCCESS,
    ProviderStatus.NO_RESULTS: SearchExecutionStatus.EMPTY,
    ProviderStatus.FAILED: SearchExecutionStatus.FAILED,
    ProviderStatus.RATE_LIMITED: SearchExecutionStatus.RATE_LIMITED,
    ProviderStatus.AUTH_REQUIRED: SearchExecutionStatus.AUTH_REQUIRED,
    ProviderStatus.NOT_CONFIGURED: SearchExecutionStatus.NOT_CONFIGURED,
    ProviderStatus.TIMEOUT: SearchExecutionStatus.TIMEOUT,
    ProviderStatus.SKIPPED: SearchExecutionStatus.SKIPPED,
    ProviderStatus.CANCELLED: SearchExecutionStatus.CANCELLED,
}


class SourceType(StrEnum):
    """Natureza da fonte de uma evidência."""

    PUBLIC = "PUBLIC"
    AUTHENTICATED = "AUTHENTICATED"
    PAID = "PAID"
    RESTRICTED = "RESTRICTED"
    ARCHIVE = "ARCHIVE"
    TOR = "TOR"
    MANUAL = "MANUAL"
    DERIVED = "DERIVED"
    USER_INPUT = "USER_INPUT"


class DataTemporality(StrEnum):
    CURRENT_DATA = "CURRENT_DATA"
    HISTORICAL_DATA = "HISTORICAL_DATA"


class AuditEvent(StrEnum):
    CASE_CREATED = "CASE_CREATED"
    CASE_UPDATED = "CASE_UPDATED"
    CASE_STARTED = "CASE_STARTED"
    CASE_FINISHED = "CASE_FINISHED"
    CASE_FAILED = "CASE_FAILED"
    SEED_REGISTERED = "SEED_REGISTERED"
    SEARCH_STARTED = "SEARCH_STARTED"
    SEARCH_FINISHED = "SEARCH_FINISHED"
    PROVIDER_CALLED = "PROVIDER_CALLED"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    ENTITY_CREATED = "ENTITY_CREATED"
    ENTITY_MERGED = "ENTITY_MERGED"
    EVIDENCE_CREATED = "EVIDENCE_CREATED"
    RELATIONSHIP_CREATED = "RELATIONSHIP_CREATED"
    PIVOT_CREATED = "PIVOT_CREATED"
    PIVOT_SKIPPED = "PIVOT_SKIPPED"
    CORRELATION_CREATED = "CORRELATION_CREATED"
    CONFLICT_DETECTED = "CONFLICT_DETECTED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    JOB_CREATED = "JOB_CREATED"
    JOB_QUEUED = "JOB_QUEUED"
    JOB_STARTED = "JOB_STARTED"
    JOB_COMPLETED = "JOB_COMPLETED"
    JOB_FAILED = "JOB_FAILED"
    JOB_CANCELLED = "JOB_CANCELLED"
    JOB_INTERRUPTED = "JOB_INTERRUPTED"
    JOB_RETRY_SCHEDULED = "JOB_RETRY_SCHEDULED"
    JOB_RECOVERED = "JOB_RECOVERED"
    EXPORT_CREATED = "EXPORT_CREATED"
    AI_ANALYSIS_STARTED = "AI_ANALYSIS_STARTED"
    AI_ANALYSIS_COMPLETED = "AI_ANALYSIS_COMPLETED"
    AI_ANALYSIS_FAILED = "AI_ANALYSIS_FAILED"
    AI_FALLBACK_USED = "AI_FALLBACK_USED"
    AI_REVIEWED = "AI_REVIEWED"


class PivotStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    EXECUTED = "EXECUTED"
    SKIPPED_VISITED = "SKIPPED_VISITED"
    SKIPPED_DEPTH = "SKIPPED_DEPTH"
    SKIPPED_BUDGET = "SKIPPED_BUDGET"
    SKIPPED_LOW_CONFIDENCE = "SKIPPED_LOW_CONFIDENCE"
    SKIPPED_LOW_PRIORITY = "SKIPPED_LOW_PRIORITY"
    SKIPPED_BLOCKED = "SKIPPED_BLOCKED"


class CorrelationLevel(StrEnum):
    HIGH_CONFIDENCE = "HIGH_CONFIDENCE"  # gera SAME_AS (exige sinal forte)
    POSSIBLE = "POSSIBLE"                # gera POSSIBLY_SAME_AS
    WEAK = "WEAK"                        # registrado, sem relação
    UNRELATED = "UNRELATED"


CLASSIFICATION_TO_SOURCE_TYPE: dict[DataClassification, SourceType] = {
    DataClassification.PUBLIC: SourceType.PUBLIC,
    DataClassification.AUTHENTICATED: SourceType.AUTHENTICATED,
    DataClassification.PAID: SourceType.PAID,
    DataClassification.RESTRICTED: SourceType.RESTRICTED,
    DataClassification.MANUAL: SourceType.MANUAL,
    DataClassification.DERIVED: SourceType.DERIVED,
}


# --- Jobs (execução) ------------------------------------------------------------------


class JobStatus(StrEnum):
    PENDING = "PENDING"          # gravado no banco, ainda não confirmado na fila
    QUEUED = "QUEUED"            # publicação na fila confirmada
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"        # aguardando nova tentativa (next_attempt_at)
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"            # terminal: excedeu tentativas ou erro não recuperável (dead letter)
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"  # heartbeat expirou (worker morreu); reconciliador decide o próximo passo


ACTIVE_JOB_STATUSES = frozenset({JobStatus.PENDING, JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.RETRYING})
TERMINAL_JOB_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})
CLAIMABLE_JOB_STATUSES = frozenset({JobStatus.PENDING, JobStatus.QUEUED, JobStatus.RETRYING, JobStatus.INTERRUPTED})


class JobType(StrEnum):
    INVESTIGATION = "INVESTIGATION"
    EXPORT = "EXPORT"
    AI_ANALYSIS = "AI_ANALYSIS"


class JobEventType(StrEnum):
    JOB_CREATED = "JOB_CREATED"
    JOB_QUEUED = "JOB_QUEUED"
    JOB_STARTED = "JOB_STARTED"
    JOB_PROGRESS = "JOB_PROGRESS"
    JOB_CHECKPOINT = "JOB_CHECKPOINT"
    PROVIDER_STARTED = "PROVIDER_STARTED"
    PROVIDER_FINISHED = "PROVIDER_FINISHED"
    ENTITY_CREATED = "ENTITY_CREATED"
    PIVOT_CREATED = "PIVOT_CREATED"
    JOB_COMPLETED = "JOB_COMPLETED"
    JOB_FAILED = "JOB_FAILED"
    JOB_RETRY_SCHEDULED = "JOB_RETRY_SCHEDULED"
    JOB_CANCELLED = "JOB_CANCELLED"
    JOB_INTERRUPTED = "JOB_INTERRUPTED"
