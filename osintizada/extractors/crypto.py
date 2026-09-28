"""Extractors de cripto (BTC, EVM) e hashes."""

from __future__ import annotations

import re
from collections.abc import Iterable

from osintizada.core.enums import EntityType, IdentifierType
from osintizada.core.validators import is_valid_btc_address
from osintizada.extractors.base import BaseExtractor, Extraction

_BTC = re.compile(r"(?<![A-Za-z0-9])([13][1-9A-HJ-NP-Za-km-z]{24,33}|bc1[02-9ac-hj-np-z]{11,71})(?![A-Za-z0-9])")
_EVM_ADDR = re.compile(r"(?<![0-9A-Za-z])(0x[0-9a-fA-F]{40})(?![0-9A-Za-z])")
_EVM_TX = re.compile(r"(?<![0-9A-Za-z])(0x[0-9a-fA-F]{64})(?![0-9A-Za-z])")
_HEX = re.compile(r"(?<![0-9A-Za-z])([0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64})(?![0-9A-Za-z])")
_HASH_TYPES = {32: IdentifierType.MD5, 40: IdentifierType.SHA1, 64: IdentifierType.SHA256}


class CryptoExtractor(BaseExtractor):
    name = "crypto"
    produces = frozenset({EntityType.CRYPTO_ADDRESS, EntityType.CRYPTO_TRANSACTION})
    priority = 12
    consumes_span = True

    def extract(self, text: str) -> Iterable[Extraction]:
        for m in _BTC.finditer(text):
            addr = m.group(1)
            if is_valid_btc_address(addr):
                value = addr.lower() if addr.lower().startswith("bc1") else addr
                yield self.make(text, m.start(1), m.end(1), EntityType.CRYPTO_ADDRESS, value, 0.95,
                                IdentifierType.BTC_ADDRESS, chain="bitcoin")
        for m in _EVM_ADDR.finditer(text):
            yield self.make(text, m.start(1), m.end(1), EntityType.CRYPTO_ADDRESS, m.group(1).lower(), 0.9,
                            IdentifierType.EVM_ADDRESS, chain="evm")
        for m in _EVM_TX.finditer(text):
            yield self.make(text, m.start(1), m.end(1), EntityType.CRYPTO_TRANSACTION, m.group(1).lower(), 0.85,
                            IdentifierType.TX_HASH, chain="evm")


class HashExtractor(BaseExtractor):
    name = "hash"
    produces = frozenset({EntityType.HASH})
    priority = 45
    respect_consumed = True

    def extract(self, text: str) -> Iterable[Extraction]:
        for m in _HEX.finditer(text):
            token = m.group(1)
            if token.isdigit() or token.isalpha():
                continue  # só dígitos/letras: improvável ser hash
            id_type = _HASH_TYPES[len(token)]
            yield self.make(text, m.start(1), m.end(1), EntityType.HASH, token.lower(), 0.6, id_type,
                            algorithm=id_type.value)
