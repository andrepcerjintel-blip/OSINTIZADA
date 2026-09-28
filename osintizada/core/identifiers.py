"""Identifier Engine: detecção de tipo de input com múltiplas hipóteses.

Nunca força classificação única: devolve todos os candidatos plausíveis,
ordenados por confiança, cada um com o motivo da hipótese.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata

from osintizada.core.domains import get_domain_parser
from osintizada.core.enums import IdentifierType as T
from osintizada.core.models import (
    DetectionResult,
    IdentifierCandidate,
    NormalizedIdentifier,
)
from osintizada.core.normalization import normalize
from osintizada.core.urls import parse_social_url
from osintizada.core.validators import (
    is_valid_btc_address,
    is_valid_cnpj,
    is_valid_cpf,
    is_valid_hostname,
    only_digits,
)

# DDDs válidos no Brasil.
BR_DDDS = frozenset(
    {11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 24, 27, 28, 31, 32, 33, 34, 35, 37, 38,
     41, 42, 43, 44, 45, 46, 47, 48, 49, 51, 53, 54, 55, 61, 62, 63, 64, 65, 66, 67, 68,
     69, 71, 73, 74, 75, 77, 79, 81, 82, 83, 84, 85, 86, 87, 88, 89, 91, 92, 93, 94, 95,
     96, 97, 98, 99}
)

_URL_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_BARE_URL = re.compile(r"^(?:www\.)?[a-z0-9.-]+\.[a-z]{2,63}/\S*$", re.I)
_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,63})$")
_ASN = re.compile(r"^AS[N]?\s?(\d{1,10})$", re.I)
_EVM_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_EVM_TX = re.compile(r"^0x[0-9a-fA-F]{64}$")
_HEX = re.compile(r"^[0-9a-fA-F]+$")
_ONION = re.compile(r"^(?:[a-z0-9-]+\.)*[a-z2-7]{56}\.onion$", re.I)
_CPF_FMT = re.compile(r"^\d{3}\.\d{3}\.\d{3}-\d{2}$")
_CNPJ_FMT = re.compile(r"^\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}$")
_NUMERIC_LIKE = re.compile(r"^[+\d\s().\-/]+$")
_AT_HANDLE = re.compile(r"^@([A-Za-z0-9_.]{1,32})$")
_USERNAME = re.compile(r"^[A-Za-z0-9_.\-]{2,64}$")
_TG_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_TG_ID = re.compile(r"^-?\d{5,16}$")


def _is_name_word(word: str) -> bool:
    cleaned = word.replace("'", "").replace("-", "")
    return bool(cleaned) and all(unicodedata.category(c).startswith("L") for c in cleaned)


class _Collector:
    def __init__(self) -> None:
        self._by_type: dict[T, IdentifierCandidate] = {}

    def add(self, type_: T, confidence: float, reason: str, value: str | None = None, **meta) -> None:
        current = self._by_type.get(type_)
        if current is None or confidence > current.confidence:
            self._by_type[type_] = IdentifierCandidate(
                type=type_, confidence=round(confidence, 2), reason=reason, value=value, metadata=meta
            )

    def result(self, raw: str) -> DetectionResult:
        ordered = sorted(self._by_type.values(), key=lambda c: c.confidence, reverse=True)
        return DetectionResult(input=raw, candidates=ordered)


class IdentifierEngine:
    """Detecta tipos de identificador e produz versões normalizadas."""

    def detect(self, raw: str) -> DetectionResult:
        value = raw.strip()
        out = _Collector()
        if not value:
            return out.result(raw)

        if self._detect_url(value, out):
            return out.result(raw)
        if self._detect_email(value, out):
            return out.result(raw)
        if self._detect_network(value, out):
            return out.result(raw)
        if self._detect_crypto_or_hash(value, out):
            return out.result(raw)
        if self._detect_host(value, out):
            return out.result(raw)
        if self._detect_numeric(value, out):
            return out.result(raw)
        if self._detect_handle(value, out):
            return out.result(raw)
        if self._detect_name(value, out):
            return out.result(raw)

        out.add(T.KEYWORD, 0.5, "Texto livre sem padrão estruturado reconhecido")
        return out.result(raw)

    def analyze(self, raw: str, min_confidence: float = 0.3) -> list[NormalizedIdentifier]:
        """Detecta e normaliza todos os candidatos acima de ``min_confidence``."""
        detection = self.detect(raw)
        normalized: list[NormalizedIdentifier] = []
        for cand in detection.candidates:
            if cand.confidence < min_confidence:
                continue
            item = normalize(cand.value or raw, cand.type)
            item.metadata.setdefault("detection_confidence", cand.confidence)
            item.metadata.setdefault("detection_reason", cand.reason)
            normalized.append(item)
        return normalized

    # --- detectores individuais ---------------------------------------------

    @staticmethod
    def _detect_url(value: str, out: _Collector) -> bool:
        if " " in value or not (_URL_SCHEME.match(value) or _BARE_URL.match(value)):
            return False
        out.add(T.URL, 0.97, "Possui esquema de URL ou formato host/caminho")
        host_part = re.sub(_URL_SCHEME, "", value).split("/", 1)[0].split(":", 1)[0].lower()
        if host_part.endswith(".onion"):
            out.add(T.ONION, 0.95, "Host termina em .onion", value=host_part)
        social = parse_social_url(value)
        if social is not None:
            out.add(social.identifier_type, 0.9, f"Perfil {social.platform} extraído da URL", value=social.handle,
                    platform=social.platform)
            if social.platform == "telegram":
                out.add(T.TELEGRAM_LINK, 0.95, "Link público do Telegram")
        return True

    @staticmethod
    def _detect_email(value: str, out: _Collector) -> bool:
        match = _EMAIL.match(value)
        if not match or not is_valid_hostname(match.group(1)):
            return False
        out.add(T.EMAIL, 0.98, "Formato local@domínio válido")
        return True

    @staticmethod
    def _detect_network(value: str, out: _Collector) -> bool:
        if m := _ASN.match(value):
            out.add(T.ASN, 0.97, "Prefixo AS seguido de número", value=m.group(1))
            return True
        if "/" in value:
            try:
                ipaddress.ip_network(value, strict=False)
                out.add(T.CIDR, 0.97, "Notação CIDR válida")
                return True
            except ValueError:
                return False
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            return False
        out.add(T.IPV4 if ip.version == 4 else T.IPV6, 0.99, f"Endereço IPv{ip.version} válido")
        return True

    @staticmethod
    def _detect_crypto_or_hash(value: str, out: _Collector) -> bool:
        if _EVM_ADDR.match(value):
            out.add(T.EVM_ADDRESS, 0.97, "0x + 40 hex (endereço EVM)")
            return True
        if _EVM_TX.match(value):
            out.add(T.TX_HASH, 0.95, "0x + 64 hex (hash de transação EVM)", chain="evm")
            return True
        if _HEX.match(value) and len(value) in (32, 40, 64, 128):
            if len(value) == 32:
                out.add(T.MD5, 0.9, "32 caracteres hexadecimais")
            elif len(value) == 40:
                out.add(T.SHA1, 0.9, "40 caracteres hexadecimais")
            elif len(value) == 64:
                out.add(T.SHA256, 0.85, "64 caracteres hexadecimais")
                out.add(T.TX_HASH, 0.35, "64 hex também é formato de txid Bitcoin", chain="btc")
            else:
                out.add(T.SHA512, 0.9, "128 caracteres hexadecimais")
            return True
        if is_valid_btc_address(value):
            out.add(T.BTC_ADDRESS, 0.98, "Endereço Bitcoin com checksum válido")
            return True
        return False

    @staticmethod
    def _detect_host(value: str, out: _Collector) -> bool:
        host = value.lower().rstrip("./")
        if _ONION.match(host):
            out.add(T.ONION, 0.97, "Endereço onion v3")
            return True
        if not is_valid_hostname(host) or host.replace(".", "").isdigit():
            return False
        parts = get_domain_parser().parse_domain(host)
        if parts.is_public_suffix or parts.registrable_domain is None:
            return False  # "com.br" sozinho é sufixo público, não domínio
        known = parts.is_known_suffix
        base = 0.93 if known else 0.55
        if parts.registrable_domain == host:
            out.add(T.DOMAIN, base, f"Domínio registrável sob o sufixo público '{parts.suffix}'",
                    suffix=parts.suffix)
        else:
            out.add(T.SUBDOMAIN, base - 0.03, "Hostname com rótulos abaixo do domínio registrável",
                    registrable_domain=parts.registrable_domain, suffix=parts.suffix)
            out.add(T.HOSTNAME, base - 0.3, "Pode ser hostname específico de serviço")
        if not known:
            out.add(T.USERNAME, 0.5, "Sufixo fora da Public Suffix List: pode ser username com ponto")
        return True

    @staticmethod
    def _detect_numeric(value: str, out: _Collector) -> bool:
        if not _NUMERIC_LIKE.match(value):
            return False
        digits = only_digits(value)
        if not digits:
            return False

        if _CPF_FMT.match(value):
            valid = is_valid_cpf(digits)
            out.add(T.CPF, 0.99 if valid else 0.6, "Formato de CPF" + ("" if valid else " (dígito verificador inválido)"),
                    checksum_valid=valid)
            return True
        if _CNPJ_FMT.match(value):
            valid = is_valid_cnpj(digits)
            out.add(T.CNPJ, 0.99 if valid else 0.6, "Formato de CNPJ" + ("" if valid else " (dígito verificador inválido)"),
                    checksum_valid=valid)
            return True

        if _TG_ID.match(value) and value.startswith("-100"):
            out.add(T.TELEGRAM_ID, 0.9, "Prefixo -100 de ID de canal/supergrupo Telegram")
            return True

        n = len(digits)
        if n == 14:
            valid = is_valid_cnpj(digits)
            out.add(T.CNPJ, 0.95 if valid else 0.3, "14 dígitos" + (" com DV de CNPJ válido" if valid else ""),
                    checksum_valid=valid)
        if n == 11:
            valid = is_valid_cpf(digits)
            out.add(T.CPF, 0.85 if valid else 0.25, "11 dígitos" + (" com DV de CPF válido" if valid else ""),
                    checksum_valid=valid)

        phone_conf = IdentifierEngine._phone_confidence(value, digits)
        if phone_conf > 0:
            out.add(T.PHONE, phone_conf, "Formato compatível com telefone")

        if _TG_ID.match(value) and not value.startswith("-") and 5 <= n <= 12:
            out.add(T.TELEGRAM_ID, 0.25, "Sequência numérica compatível com ID Telegram")
        out.add(T.KEYWORD, 0.2, "Sequência numérica")
        return True

    @staticmethod
    def _phone_confidence(value: str, digits: str) -> float:
        n = len(digits)
        if value.startswith("+"):
            return 0.92 if 8 <= n <= 15 else 0.0
        national = digits[2:] if n in (12, 13) and digits.startswith("55") else digits
        if len(national) in (10, 11) and int(national[:2]) in BR_DDDS:
            if len(national) == 11:
                return 0.85 if national[2] == "9" else 0.3
            return 0.75 if national[2] in "2345" else 0.4  # fixo
        if n in (8, 9):
            return 0.35  # número local sem DDD
        return 0.0

    @staticmethod
    def _detect_handle(value: str, out: _Collector) -> bool:
        if m := _AT_HANDLE.match(value):
            handle = m.group(1)
            out.add(T.USERNAME, 0.95, "Prefixo @ indica handle", value=handle)
            if _TG_USERNAME.match(handle):
                out.add(T.TELEGRAM_USERNAME, 0.55, "Compatível com regras de username do Telegram", value=handle)
            if re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
                out.add(T.TWITTER_USERNAME, 0.5, "Compatível com regras de handle do X/Twitter", value=handle)
            if re.fullmatch(r"[A-Za-z0-9_.]{1,30}", handle):
                out.add(T.INSTAGRAM_USERNAME, 0.45, "Compatível com regras do Instagram", value=handle)
            return True
        if not _USERNAME.match(value):
            return False
        has_digit = any(c.isdigit() for c in value)
        has_sep = any(c in "_.-" for c in value)
        out.add(T.USERNAME, 0.96 if (has_digit or has_sep) else 0.85, "Token único sem espaços, formato de username")
        out.add(T.ALIAS, 0.72, "Token único pode ser apelido/alias")
        if value.isalpha():
            out.add(T.POSSIBLE_NAME, 0.35, "Palavra alfabética pode ser nome ou sobrenome")
        else:
            out.add(T.POSSIBLE_NAME, 0.11, "Improvável como nome (contém dígitos/símbolos)")
        return True

    @staticmethod
    def _detect_name(value: str, out: _Collector) -> bool:
        words = value.split()
        if not (1 <= len(words) <= 8) or not all(_is_name_word(w) for w in words):
            return False
        if len(words) == 1:
            out.add(T.POSSIBLE_NAME, 0.6, "Palavra única alfabética (nome ou sobrenome)")
            out.add(T.ALIAS, 0.4, "Palavra única pode ser apelido")
            out.add(T.KEYWORD, 0.3, "Pode ser termo de busca genérico")
            return True
        particles = {"da", "de", "do", "das", "dos", "e", "di", "del", "van", "von", "la", "le"}
        capitalized = all(w[0].isupper() or w.lower() in particles for w in words)
        out.add(T.FULL_NAME, 0.88 if capitalized else 0.7, "Sequência de palavras alfabéticas (nome de pessoa/organização)")
        out.add(T.KEYWORD, 0.3, "Pode ser termo de busca genérico")
        return True
