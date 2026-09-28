"""Enumerações centrais do OSINTIZADA.

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
