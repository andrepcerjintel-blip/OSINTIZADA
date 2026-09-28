"""Canonicalização de valores de entidade antes da persistência.

O mesmo dado encontrado por dois providers precisa virar a MESMA entidade.
Funções individuais (``normalize_email`` etc.) reutilizam os normalizadores
do Identifier Engine.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re

from osintizada.core.enums import EntityType as E
from osintizada.core.enums import IdentifierType
from osintizada.core.models import social_account_value
from osintizada.core.normalization import normalize, normalize_host
from osintizada.core.urls import canonical_url
from osintizada.core.validators import only_digits


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_phone(value: str) -> str:
    return normalize(value, IdentifierType.PHONE).value


def normalize_domain(value: str) -> str:
    return normalize_host(value)


def normalize_ip(value: str) -> str:
    return str(ipaddress.ip_address(value.strip().strip("[]")))


def normalize_username(value: str) -> str:
    return value.strip().lstrip("@").lower()


def normalize_url(value: str) -> str:
    return canonical_url(value)


def _collapse(value: str) -> str:
    return " ".join(value.split())


def canonicalize(entity_type: E | str, value: str) -> str:
    """Forma canônica usada no fingerprint. Levanta ``ValueError`` para valor inválido."""
    etype = E(entity_type)
    raw = value.strip()
    if not raw:
        raise ValueError("valor vazio")
    if etype == E.EMAIL:
        return normalize_email(raw)
    if etype == E.PHONE:
        return normalize_phone(raw)
    if etype in (E.DOMAIN, E.SUBDOMAIN):
        return normalize_domain(raw)
    if etype == E.IP:
        return normalize_ip(raw)
    if etype == E.NETWORK:
        return str(ipaddress.ip_network(raw, strict=False))
    if etype == E.ASN:
        digits = only_digits(raw)
        if not digits:
            raise ValueError(f"ASN inválido: {raw}")
        return f"AS{int(digits)}"
    if etype == E.URL:
        return normalize_url(raw)
    if etype in (E.USERNAME, E.TELEGRAM_USER, E.TELEGRAM_CHANNEL, E.TELEGRAM_GROUP):
        return normalize_username(raw)
    if etype == E.SOCIAL_ACCOUNT:
        platform, sep, handle = raw.partition(":")
        return social_account_value(platform.lower(), handle) if sep else normalize_username(raw)
    if etype in (E.CPF, E.CNPJ):
        return only_digits(raw)
    if etype == E.CRYPTO_ADDRESS:
        return raw.lower() if raw.lower().startswith(("0x", "bc1")) else raw
    if etype in (E.CRYPTO_TRANSACTION, E.HASH):
        return raw.lower()
    if etype in (E.ORGANIZATION, E.PERSON, E.LOCATION, E.KEYWORD):
        return _collapse(raw).casefold()
    return _collapse(raw)


def entity_fingerprint_hash(entity_type: E | str, canonical_value: str) -> str:
    """Fingerprint determinístico: sha256(TIPO:valor_canônico)."""
    return hashlib.sha256(f"{E(entity_type).value}:{canonical_value}".encode()).hexdigest()


_HANDLE = re.compile(r"^[A-Za-z0-9_.-]+$")


def handle_of(entity_type: E | str, canonical_value: str) -> str | None:
    """Handle comparável entre plataformas (para correlação de usernames)."""
    etype = E(entity_type)
    if etype == E.SOCIAL_ACCOUNT:
        handle = canonical_value.partition(":")[2]
    elif etype in (E.USERNAME, E.TELEGRAM_USER):
        handle = canonical_value
    else:
        return None
    handle = handle.lower().lstrip("@")
    return handle if handle and _HANDLE.match(handle) and not handle.isdigit() else None
