"""Validadores determinísticos (checksums e formatos).

Somente código puro, sem rede. Usados pelo Identifier Engine para ajustar a
confiança das hipóteses de classificação.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re

_DIGITS = re.compile(r"\D+")

# Rótulo DNS: 1-63 chars, alfanumérico e hífen, sem hífen nas pontas.
_DNS_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_TLD = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})$")

def only_digits(value: str) -> str:
    return _DIGITS.sub("", value)


def is_valid_cpf(value: str) -> bool:
    digits = only_digits(value)
    if len(digits) != 11 or digits == digits[0] * 11:
        return False
    for size in (9, 10):
        total = sum(int(d) * w for d, w in zip(digits[:size], range(size + 1, 1, -1), strict=True))
        check = (total * 10) % 11 % 10
        if check != int(digits[size]):
            return False
    return True


def is_valid_cnpj(value: str) -> bool:
    digits = only_digits(value)
    if len(digits) != 14 or digits == digits[0] * 14:
        return False
    weights_1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    weights_2 = [6] + weights_1
    for size, weights in ((12, weights_1), (13, weights_2)):
        total = sum(int(d) * w for d, w in zip(digits[:size], weights, strict=True))
        rest = total % 11
        check = 0 if rest < 2 else 11 - rest
        if check != int(digits[size]):
            return False
    return True


def is_valid_hostname(value: str) -> bool:
    host = value.rstrip(".").lower()
    if not host or len(host) > 253 or "." not in host:
        return False
    labels = host.split(".")
    try:
        labels = [lbl.encode("idna").decode("ascii") if not lbl.isascii() else lbl for lbl in labels]
    except UnicodeError:
        return False
    if not all(_DNS_LABEL.match(lbl) for lbl in labels):
        return False
    return bool(_TLD.match(labels[-1]))


_DNS_NAME_LABEL = re.compile(r"^(?!-)[a-z0-9_-]{1,63}(?<!-)$")


def is_valid_dns_name(value: str) -> bool:
    """Nome DNS genérico: como hostname, mas aceita '_' (ex.: _spf.google.com, _dmarc.x.com)."""
    name = value.rstrip(".").lower()
    if not name or len(name) > 253 or "." not in name:
        return False
    labels = name.split(".")
    return all(_DNS_NAME_LABEL.match(lbl) for lbl in labels) and bool(_TLD.match(labels[-1]))


def registrable_domain(host: str) -> str:
    """Domínio registrável (eTLD+1) segundo a Public Suffix List.

    Mantido por compatibilidade; se o host não tiver domínio registrável (ex.: é
    ele próprio um sufixo público), devolve o host normalizado.
    """
    from osintizada.core.domains import get_domain_parser

    parts = get_domain_parser().parse_domain(host)
    return parts.registrable_domain or parts.hostname


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


# --- Bitcoin -----------------------------------------------------------------

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _b58decode(value: str) -> bytes | None:
    num = 0
    for ch in value:
        idx = _B58_ALPHABET.find(ch)
        if idx < 0:
            return None
        num = num * 58 + idx
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(value) - len(value.lstrip("1"))
    return b"\x00" * pad + raw


def is_valid_btc_base58(value: str) -> bool:
    if not (25 <= len(value) <= 35) or value[0] not in "13":
        return False
    data = _b58decode(value)
    if data is None or len(data) != 25:
        return False
    payload, checksum = data[:-4], data[-4:]
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] == checksum


def _bech32_polymod(values: list[int]) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def is_valid_btc_bech32(value: str) -> bool:
    if value.lower() != value and value.upper() != value:
        return False
    addr = value.lower()
    if not addr.startswith("bc1") or not (14 <= len(addr) <= 74):
        return False
    hrp, data_part = addr[:2], addr[3:]
    if any(c not in _BECH32_CHARSET for c in data_part) or len(data_part) < 6:
        return False
    data = [_BECH32_CHARSET.find(c) for c in data_part]
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    # bech32 (v0) => 1 ; bech32m (v1+) => 0x2BC830A3
    return _bech32_polymod(expanded + data) in (1, 0x2BC830A3)


def is_valid_btc_address(value: str) -> bool:
    return is_valid_btc_base58(value) or is_valid_btc_bech32(value)
