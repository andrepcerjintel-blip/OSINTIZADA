"""Extractors de contato: email e telefone."""

from __future__ import annotations

import re
from collections.abc import Iterable

from osintizada.core.enums import EntityType, IdentifierType
from osintizada.core.identifiers import BR_DDDS
from osintizada.core.normalization import normalize
from osintizada.core.validators import is_valid_hostname, only_digits
from osintizada.extractors.base import BaseExtractor, Extraction

# TLDs que na prática são extensões de arquivo (ex.: logo@2x.png).
_FILE_TLDS = frozenset({"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "ico", "bmp", "tif", "tiff"})

_EMAIL = re.compile(r"(?<![\w.+-])([A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,63})(?![\w-])")
_OBFUSCATED = re.compile(
    r"(?<![\w.])([A-Za-z0-9][A-Za-z0-9._+-]{0,63})\s*[\[(\{]\s*(?:at|arroba)\s*[\])\}]\s*"
    r"([A-Za-z0-9-]+(?:\s*[\[(\{]\s*(?:dot|ponto)\s*[\])\}]\s*[A-Za-z0-9-]+)+)",
    re.I,
)
_OBF_DOT = re.compile(r"\s*[\[(\{]\s*(?:dot|ponto)\s*[\])\}]\s*", re.I)


class EmailExtractor(BaseExtractor):
    name = "email"
    produces = frozenset({EntityType.EMAIL})
    priority = 10
    consumes_span = True

    def extract(self, text: str) -> Iterable[Extraction]:
        for m in _EMAIL.finditer(text):
            email = m.group(1).rstrip(".")
            domain = email.rsplit("@", 1)[1].lower()
            if domain.rsplit(".", 1)[-1] in _FILE_TLDS or not is_valid_hostname(domain):
                continue
            yield self.make(text, m.start(1), m.start(1) + len(email), EntityType.EMAIL, email.lower(), 0.95,
                            IdentifierType.EMAIL)
        for m in _OBFUSCATED.finditer(text):
            domain = _OBF_DOT.sub(".", m.group(2)).lower()
            if not is_valid_hostname(domain):
                continue
            email = f"{m.group(1).lower()}@{domain}"
            yield self.make(text, m.start(), m.end(), EntityType.EMAIL, email, 0.75, IdentifierType.EMAIL,
                            obfuscated=True)


# Telefone brasileiro: +55 opcional, DDD com/sem parênteses, 8-9 dígitos.
_PHONE_BR = re.compile(
    r"(?<![\d+])(?:\+?55[\s.-]?)?\(?([1-9]\d)\)?[\s.-]?(9?\d{4})[\s.-]?(\d{4})(?![\d])"
)
# Internacional explícito: começa com + e tem 8-15 dígitos em grupos.
_PHONE_INTL = re.compile(r"(?<![\w+])\+(\d{1,3})(?:[\s.-]?\(?\d{1,4}\)?){2,5}(?![\d])")
_PHONE_CONTEXT = re.compile(r"(?i)\b(tel|telefone|fone|celular|cel|whats(?:app)?|zap|phone|contato|ligue)\b")


class PhoneExtractor(BaseExtractor):
    name = "phone"
    produces = frozenset({EntityType.PHONE})
    priority = 40
    respect_consumed = True  # não reaproveitar dígitos de CPF/CNPJ/IPs/URLs

    def extract(self, text: str) -> Iterable[Extraction]:
        seen_spans: list[tuple[int, int]] = []
        for m in _PHONE_BR.finditer(text):
            ddd, part1 = int(m.group(1)), m.group(2)
            raw = m.group(0)
            if ddd not in BR_DDDS:
                continue
            is_mobile = len(part1) == 5 and part1[0] == "9"
            is_landline = len(part1) == 4 and part1[0] in "2345"
            if not (is_mobile or is_landline):
                continue
            formatted = any(c in raw for c in "()-. ") or raw.startswith("+")
            has_context = bool(_PHONE_CONTEXT.search(text[max(0, m.start() - 30):m.start()]))
            if not formatted and not has_context:
                continue  # sequência de dígitos crua sem contexto: alto risco de falso positivo
            norm = normalize(raw, IdentifierType.PHONE)
            conf = 0.85 if has_context else 0.7
            seen_spans.append((m.start(), m.end()))
            yield self.make(text, m.start(), m.end(), EntityType.PHONE, norm.value, conf, IdentifierType.PHONE,
                            is_mobile=is_mobile, context_keyword=has_context)
        for m in _PHONE_INTL.finditer(text):
            if any(s <= m.start() < e for s, e in seen_spans):
                continue
            digits = only_digits(m.group(0))
            if not 8 <= len(digits) <= 15 or digits.startswith("55"):
                continue  # +55 já coberto pelo padrão brasileiro
            yield self.make(text, m.start(), m.end(), EntityType.PHONE, f"+{digits}", 0.7, IdentifierType.PHONE)
