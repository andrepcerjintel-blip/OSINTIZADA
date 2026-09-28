"""Extractors de documentos brasileiros: CPF e CNPJ (com validação de DV)."""

from __future__ import annotations

import re
from collections.abc import Iterable

from osintizada.core.enums import EntityType, IdentifierType
from osintizada.core.validators import is_valid_cnpj, is_valid_cpf, only_digits
from osintizada.extractors.base import BaseExtractor, Extraction

_CPF_FMT = re.compile(r"(?<![\d.])(\d{3}\.\d{3}\.\d{3}-\d{2})(?![\d-])")
_CNPJ_FMT = re.compile(r"(?<![\d.])(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})(?![\d-])")
_DIGITS = re.compile(r"(?<![\d./-])(\d{11}|\d{14})(?![\d./-])")
_CPF_CTX = re.compile(r"(?i)\bcpf\b[^\d]{0,15}$")
_CNPJ_CTX = re.compile(r"(?i)\bcnpj\b[^\d]{0,15}$")


class CPFExtractor(BaseExtractor):
    name = "cpf"
    produces = frozenset({EntityType.CPF})
    priority = 15
    consumes_span = True

    def extract(self, text: str) -> Iterable[Extraction]:
        for m in _CPF_FMT.finditer(text):
            if is_valid_cpf(m.group(1)):
                yield self.make(text, m.start(1), m.end(1), EntityType.CPF, only_digits(m.group(1)), 0.95,
                                IdentifierType.CPF, formatted=m.group(1))
        for m in _DIGITS.finditer(text):
            digits = m.group(1)
            # CPF sem formatação só com a palavra "CPF" logo antes: 11 dígitos soltos são ambíguos
            # (telefone, protocolos) mesmo com DV válido.
            if len(digits) == 11 and is_valid_cpf(digits) and _CPF_CTX.search(text[max(0, m.start() - 20):m.start()]):
                yield self.make(text, m.start(1), m.end(1), EntityType.CPF, digits, 0.85, IdentifierType.CPF,
                                context_keyword=True)


class CNPJExtractor(BaseExtractor):
    name = "cnpj"
    produces = frozenset({EntityType.CNPJ})
    priority = 15
    consumes_span = True

    def extract(self, text: str) -> Iterable[Extraction]:
        for m in _CNPJ_FMT.finditer(text):
            if is_valid_cnpj(m.group(1)):
                yield self.make(text, m.start(1), m.end(1), EntityType.CNPJ, only_digits(m.group(1)), 0.97,
                                IdentifierType.CNPJ, formatted=m.group(1))
        for m in _DIGITS.finditer(text):
            digits = m.group(1)
            if len(digits) == 14 and is_valid_cnpj(digits):
                has_ctx = bool(_CNPJ_CTX.search(text[max(0, m.start() - 20):m.start()]))
                yield self.make(text, m.start(1), m.end(1), EntityType.CNPJ, digits, 0.9 if has_ctx else 0.6,
                                IdentifierType.CNPJ, context_keyword=has_ctx)
