"""Extractors de internet: URL, domínio, IP e ASN."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable

from osintizada.core.domains import get_domain_parser
from osintizada.core.enums import EntityType, IdentifierType
from osintizada.core.urls import canonical_url
from osintizada.core.validators import is_valid_hostname
from osintizada.extractors.base import BaseExtractor, Extraction

_URL = re.compile(r"\b(?:https?://|www\.)[^\s<>\"'`{}|\\^]+", re.I)
_TRAILING = ".,;:!?)]}'\"»”’…"


def clean_url(raw: str) -> str:
    url = raw.rstrip(_TRAILING)
    # Parêntese de fechamento só é mantido se balanceado (ex.: URLs da Wikipedia).
    while url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    return url


class URLExtractor(BaseExtractor):
    name = "url"
    produces = frozenset({EntityType.URL})
    priority = 5
    consumes_span = True

    def extract(self, text: str) -> Iterable[Extraction]:
        for m in _URL.finditer(text):
            url = clean_url(m.group(0))
            full = url if "://" in url else "http://" + url
            host = re.sub(r"^[a-z]+://", "", full, flags=re.I).split("/", 1)[0].split(":", 1)[0].lower()
            if not (is_valid_hostname(host) or _is_ip(host)):
                continue
            yield self.make(text, m.start(), m.start() + len(url), EntityType.URL, canonical_url(full), 0.95,
                            IdentifierType.URL, original_url=url, host=host)


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


_HOST = re.compile(r"(?<![\w@.-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?![\w-]|\.[a-z0-9])", re.I)


class DomainExtractor(BaseExtractor):
    """Hostnames soltos no texto (fora de URLs/emails) com TLD conhecido."""

    name = "domain"
    produces = frozenset({EntityType.DOMAIN, EntityType.SUBDOMAIN})
    priority = 30
    respect_consumed = True

    def extract(self, text: str) -> Iterable[Extraction]:
        for m in _HOST.finditer(text):
            host = m.group(1).lower()
            parts = get_domain_parser().parse_domain(host)
            # Só sufixos reais da PSL: descarta "relatorio.pdf", "arquivo.txt", "e.g".
            if not parts.is_known_suffix or parts.registrable_domain is None or not is_valid_hostname(host):
                continue
            root = parts.registrable_domain
            bare = host[4:] if host.startswith("www.") else host
            is_root = bare == root
            yield self.make(text, m.start(1), m.end(1), EntityType.DOMAIN if is_root else EntityType.SUBDOMAIN,
                            bare, 0.7, IdentifierType.DOMAIN if is_root else IdentifierType.SUBDOMAIN,
                            registrable_domain=root)


_IPV4 = re.compile(r"(?<![\d.])((?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3})"
                   r"(?:/(\d{1,2}))?(?![\d.]|\.\d)")
_IPV6_CANDIDATE = re.compile(r"(?<![\w:])((?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4})(?:/(\d{1,3}))?(?![\w:])", re.I)
_VERSION_CONTEXT = re.compile(r"(?i)(vers[aã]o|version|v\.?|release|build)\s*$")


class IPExtractor(BaseExtractor):
    name = "ip"
    produces = frozenset({EntityType.IP, EntityType.NETWORK})
    priority = 20
    consumes_span = True

    def extract(self, text: str) -> Iterable[Extraction]:
        for m in _IPV4.finditer(text):
            if _VERSION_CONTEXT.search(text[max(0, m.start() - 12):m.start()]):
                continue  # "versão 1.2.3.4"
            yield from self._emit(text, m, IdentifierType.IPV4)
        for m in _IPV6_CANDIDATE.finditer(text):
            if m.group(1).count(":") < 2 or not any(c.isdigit() for c in m.group(1)):
                continue
            yield from self._emit(text, m, IdentifierType.IPV6)

    def _emit(self, text: str, m: re.Match[str], id_type: IdentifierType) -> Iterable[Extraction]:
        addr_text, prefix = m.group(1), m.group(2)
        try:
            if prefix is not None:
                net = ipaddress.ip_network(f"{addr_text}/{prefix}", strict=False)
                yield self.make(text, m.start(), m.end(), EntityType.NETWORK, str(net), 0.85, IdentifierType.CIDR)
                return
            addr = ipaddress.ip_address(addr_text)
        except ValueError:
            return
        yield self.make(text, m.start(1), m.end(1), EntityType.IP, str(addr), 0.85 if addr.is_global else 0.6,
                        id_type, is_global=addr.is_global)


_ASN = re.compile(r"\bASN?\s?(\d{1,10})\b")


class ASNExtractor(BaseExtractor):
    name = "asn"
    produces = frozenset({EntityType.ASN})
    priority = 25

    def extract(self, text: str) -> Iterable[Extraction]:
        for m in _ASN.finditer(text):  # case-sensitive: evita "as 2020" em inglês
            number = int(m.group(1))
            if number == 0 or number > 4_294_967_295:
                continue
            yield self.make(text, m.start(), m.end(), EntityType.ASN, f"AS{number}", 0.8, IdentifierType.ASN)
