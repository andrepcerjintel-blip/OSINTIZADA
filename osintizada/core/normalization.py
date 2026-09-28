"""Normalização de identificadores.

Regras gerais:
  * o valor original é sempre preservado em ``NormalizedIdentifier.original``;
  * ``value`` é a forma canônica usada para deduplicação e fingerprint;
  * ``variants`` existem APENAS para pesquisa e nunca substituem o valor.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from collections.abc import Callable

from osintizada.core.enums import IdentifierType as T
from osintizada.core.models import NormalizedIdentifier
from osintizada.core.urls import canonical_url, parse_social_url
from osintizada.core.validators import (
    is_valid_cnpj,
    is_valid_cpf,
    only_digits,
    registrable_domain,
)

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def format_cpf(digits: str) -> str:
    return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"


def format_cnpj(digits: str) -> str:
    return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"


# --- normalizadores por tipo -------------------------------------------------


def _email(raw: str) -> NormalizedIdentifier:
    value = raw.strip().lower()
    local, _, domain = value.partition("@")
    meta: dict = {"local_part": local, "domain": domain}
    variants = [value]
    if domain in ("gmail.com", "googlemail.com"):
        canonical_local = local.split("+", 1)[0].replace(".", "")
        meta["provider_canonical"] = f"{canonical_local}@gmail.com"
        variants.append(meta["provider_canonical"])
    elif "+" in local:
        meta["provider_canonical"] = f"{local.split('+', 1)[0]}@{domain}"
        variants.append(meta["provider_canonical"])
    return NormalizedIdentifier(original=raw, type=T.EMAIL, value=value, variants=_dedup(variants), metadata=meta)


def _phone(raw: str) -> NormalizedIdentifier:
    digits = only_digits(raw)
    explicit_intl = raw.strip().startswith("+")
    meta: dict = {"digits": digits}

    national = None
    if explicit_intl and digits.startswith("55"):
        national = digits[2:]
    elif not explicit_intl:
        stripped = digits.lstrip("0")
        if len(stripped) in (12, 13) and stripped.startswith("55"):
            national = stripped[2:]
        elif len(stripped) in (10, 11):
            national = stripped

    if national and len(national) in (10, 11):
        ddd, number = national[:2], national[2:]
        e164 = f"+55{national}"
        local_fmt = f"{number[:-4]}-{number[-4:]}"
        meta.update(country_code="55", ddd=ddd, e164=e164, is_mobile=len(number) == 9 and number[0] == "9")
        variants = [national, e164, e164[1:], f"{ddd} {local_fmt}", f"({ddd}) {local_fmt}", f"+55 {ddd} {local_fmt}"]
        return NormalizedIdentifier(original=raw, type=T.PHONE, value=e164, variants=_dedup(variants), metadata=meta)

    if explicit_intl:
        e164 = f"+{digits}"
        meta["e164"] = e164
        return NormalizedIdentifier(original=raw, type=T.PHONE, value=e164, variants=_dedup([e164, digits]), metadata=meta)

    meta["note"] = "País indeterminado; valor mantido apenas com dígitos"
    return NormalizedIdentifier(original=raw, type=T.PHONE, value=digits, variants=[digits], metadata=meta)


def _cpf(raw: str) -> NormalizedIdentifier:
    digits = only_digits(raw).zfill(11)
    formatted = format_cpf(digits)
    meta = {"formatted": formatted, "digits": digits, "checksum_valid": is_valid_cpf(digits)}
    return NormalizedIdentifier(original=raw, type=T.CPF, value=digits, variants=[digits, formatted], metadata=meta)


def _cnpj(raw: str) -> NormalizedIdentifier:
    digits = only_digits(raw).zfill(14)
    formatted = format_cnpj(digits)
    meta = {
        "formatted": formatted,
        "digits": digits,
        "checksum_valid": is_valid_cnpj(digits),
        "cnpj_root": digits[:8],  # raiz: identifica a empresa (matriz + filiais)
        "is_headquarters": digits[8:12] == "0001",
    }
    return NormalizedIdentifier(original=raw, type=T.CNPJ, value=digits, variants=[digits, formatted], metadata=meta)


def normalize_host(raw: str) -> str:
    host = _SCHEME.sub("", raw.strip()).split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.rsplit("@", 1)[-1]  # remove userinfo
    if not host.startswith("["):
        host = host.split(":", 1)[0]
    return host.rstrip(".").lower()


def _host(type_: T) -> Callable[[str], NormalizedIdentifier]:
    def inner(raw: str) -> NormalizedIdentifier:
        host = normalize_host(raw)
        meta: dict = {"registrable_domain": registrable_domain(host)}
        variants = [host]
        try:
            puny = host.encode("idna").decode("ascii")
            if puny != host:
                meta["punycode"] = puny
                variants.append(puny)
        except UnicodeError:
            pass
        if host.startswith("www."):
            variants.append(host[4:])
        return NormalizedIdentifier(original=raw, type=type_, value=host, variants=_dedup(variants), metadata=meta)

    return inner


def _url(type_: T) -> Callable[[str], NormalizedIdentifier]:
    def inner(raw: str) -> NormalizedIdentifier:
        return _url_impl(raw, type_)

    return inner


def _url_impl(raw: str, type_: T) -> NormalizedIdentifier:
    original = raw.strip()
    canonical = canonical_url(original)
    host = normalize_host(original)
    meta: dict = {"original_url": original, "host": host, "registrable_domain": registrable_domain(host)}
    social = parse_social_url(original)
    if social:
        meta.update(platform=social.platform, handle=social.handle, handle_type=social.identifier_type.value)
    return NormalizedIdentifier(original=raw, type=type_, value=canonical, variants=_dedup([original, canonical]),
                                metadata=meta)


def _ip(raw: str) -> NormalizedIdentifier:
    ip = ipaddress.ip_address(raw.strip())
    meta = {"version": ip.version, "is_private": ip.is_private, "is_global": ip.is_global,
            "is_reserved": ip.is_reserved}
    variants = [str(ip)]
    if ip.version == 6:
        variants.append(ip.exploded)
    return NormalizedIdentifier(original=raw, type=T.IPV4 if ip.version == 4 else T.IPV6, value=str(ip),
                                variants=_dedup(variants), metadata=meta)


def _cidr(raw: str) -> NormalizedIdentifier:
    net = ipaddress.ip_network(raw.strip(), strict=False)
    meta = {"num_addresses": net.num_addresses, "version": net.version}
    return NormalizedIdentifier(original=raw, type=T.CIDR, value=str(net), variants=[str(net)], metadata=meta)


def _asn(raw: str) -> NormalizedIdentifier:
    number = only_digits(raw)
    value = f"AS{number}"
    return NormalizedIdentifier(original=raw, type=T.ASN, value=value, variants=[value, number, f"ASN{number}"],
                                metadata={"number": int(number)})


def _handle(type_: T, case_insensitive: bool = False) -> Callable[[str], NormalizedIdentifier]:
    def inner(raw: str) -> NormalizedIdentifier:
        handle = raw.strip().lstrip("@")
        value = handle.lower() if case_insensitive else handle
        variants = [handle, handle.lower(), f"@{handle}"]
        # variantes de separador somente para busca (ex.: dark_wolf -> darkwolf)
        compact = re.sub(r"[._-]", "", handle)
        if compact != handle and len(compact) >= 3:
            variants.append(compact)
        if type_ == T.TELEGRAM_USERNAME:
            variants.append(f"t.me/{handle}")
        meta = {"case_preserved": handle, "mutable": True}
        return NormalizedIdentifier(original=raw, type=type_, value=value, variants=_dedup(variants), metadata=meta)

    return inner


def _telegram_id(raw: str) -> NormalizedIdentifier:
    value = raw.strip()
    meta: dict = {"immutable": True}
    variants = [value]
    if value.startswith("-100"):
        meta["peer_kind"] = "channel_or_supergroup"
        meta["bare_id"] = value[4:]
        variants.append(value[4:])
    elif value.startswith("-"):
        meta["peer_kind"] = "basic_group"
    else:
        meta["peer_kind"] = "user_or_unknown"
    return NormalizedIdentifier(original=raw, type=T.TELEGRAM_ID, value=value, variants=variants, metadata=meta)


def _lower_hex(type_: T) -> Callable[[str], NormalizedIdentifier]:
    def inner(raw: str) -> NormalizedIdentifier:
        value = raw.strip().lower()
        return NormalizedIdentifier(original=raw, type=type_, value=value, variants=_dedup([value, raw.strip()]))

    return inner


def _btc(raw: str) -> NormalizedIdentifier:
    value = raw.strip()
    if value.lower().startswith("bc1"):
        value = value.lower()
    kind = "bech32" if value.startswith("bc1") else ("p2sh" if value.startswith("3") else "p2pkh")
    return NormalizedIdentifier(original=raw, type=T.BTC_ADDRESS, value=value, variants=[value],
                                metadata={"chain": "bitcoin", "address_kind": kind})


def _name(type_: T) -> Callable[[str], NormalizedIdentifier]:
    def inner(raw: str) -> NormalizedIdentifier:
        value = " ".join(raw.split())
        plain = strip_accents(value)
        tokens = value.split()
        variants = [value, plain, plain.lower()]
        meta: dict = {"tokens": tokens, "accentless": plain}
        if len(tokens) >= 3:
            meta["first_last"] = f"{tokens[0]} {tokens[-1]}"
            variants.append(meta["first_last"])
        return NormalizedIdentifier(original=raw, type=type_, value=value, variants=_dedup(variants), metadata=meta)

    return inner


def _plain(type_: T) -> Callable[[str], NormalizedIdentifier]:
    def inner(raw: str) -> NormalizedIdentifier:
        value = " ".join(raw.split())
        return NormalizedIdentifier(original=raw, type=type_, value=value, variants=[value])

    return inner


_NORMALIZERS: dict[T, Callable[[str], NormalizedIdentifier]] = {
    T.EMAIL: _email,
    T.PHONE: _phone,
    T.CPF: _cpf,
    T.CNPJ: _cnpj,
    T.DOMAIN: _host(T.DOMAIN),
    T.SUBDOMAIN: _host(T.SUBDOMAIN),
    T.HOSTNAME: _host(T.HOSTNAME),
    T.ONION: _host(T.ONION),
    T.URL: _url(T.URL),
    T.TELEGRAM_LINK: _url(T.TELEGRAM_LINK),
    T.IPV4: _ip,
    T.IPV6: _ip,
    T.CIDR: _cidr,
    T.ASN: _asn,
    T.USERNAME: _handle(T.USERNAME),
    T.ALIAS: _handle(T.ALIAS),
    T.TELEGRAM_USERNAME: _handle(T.TELEGRAM_USERNAME, case_insensitive=True),
    T.TWITTER_USERNAME: _handle(T.TWITTER_USERNAME, case_insensitive=True),
    T.INSTAGRAM_USERNAME: _handle(T.INSTAGRAM_USERNAME, case_insensitive=True),
    T.TIKTOK_USERNAME: _handle(T.TIKTOK_USERNAME, case_insensitive=True),
    T.GITHUB_USERNAME: _handle(T.GITHUB_USERNAME, case_insensitive=True),
    T.REDDIT_USERNAME: _handle(T.REDDIT_USERNAME, case_insensitive=True),
    T.YOUTUBE_CHANNEL: _plain(T.YOUTUBE_CHANNEL),
    T.FACEBOOK_PROFILE: _plain(T.FACEBOOK_PROFILE),
    T.SOCIAL_PROFILE: _plain(T.SOCIAL_PROFILE),
    T.TELEGRAM_ID: _telegram_id,
    T.EVM_ADDRESS: _lower_hex(T.EVM_ADDRESS),
    T.TX_HASH: _lower_hex(T.TX_HASH),
    T.MD5: _lower_hex(T.MD5),
    T.SHA1: _lower_hex(T.SHA1),
    T.SHA256: _lower_hex(T.SHA256),
    T.SHA512: _lower_hex(T.SHA512),
    T.BTC_ADDRESS: _btc,
    T.FULL_NAME: _name(T.FULL_NAME),
    T.POSSIBLE_NAME: _name(T.POSSIBLE_NAME),
    T.KEYWORD: _plain(T.KEYWORD),
}


def normalize(raw: str, identifier_type: T) -> NormalizedIdentifier:
    """Normaliza ``raw`` assumindo o tipo informado.

    Falhas de parsing (ex.: IP inválido forçado manualmente) não são
    silenciadas: propagam ``ValueError`` para o chamador decidir.
    """
    normalizer = _NORMALIZERS.get(identifier_type)
    if normalizer is None:
        raise ValueError(f"Sem normalizador para {identifier_type}")
    return normalizer(raw)
