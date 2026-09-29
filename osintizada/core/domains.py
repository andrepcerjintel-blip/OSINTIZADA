"""DomainParser baseado na Public Suffix List (PSL).

Fonte principal: a PSL oficial (https://publicsuffix.org), distribuída com o
pacote ``publicsuffixlist`` e atualizável sem mudar código:

  * ``RINO_PSL_FILE`` (legado: ``OSINTIZADA_PSL_FILE``; ou ``domains.psl_file`` na configuração) aponta
    para uma cópia local mais recente de ``public_suffix_list.dat``.

Inclui as seções ICANN e PRIVATE da PSL (ex.: ``github.io``), o que é o
comportamento correto para OSINT: ``usuario.github.io`` pertence a um
usuário, não ao GitHub.
"""

from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass
from functools import lru_cache

from publicsuffixlist import PublicSuffixList

from osintizada.branding import env as branding_env


@dataclass(frozen=True)
class DomainParts:
    hostname: str
    subdomain: str | None
    registrable_domain: str | None
    suffix: str | None
    is_known_suffix: bool  # o sufixo existe na PSL (TLD real)
    is_public_suffix: bool  # o próprio hostname é um sufixo público (ex.: "com.br")

    def as_dict(self) -> dict:
        return asdict(self)


def _clean(hostname: str) -> str:
    return hostname.strip().rstrip(".").lower()


class DomainParser:
    def __init__(self, psl_file: str | None = None) -> None:
        self.source = psl_file or "bundled"
        if psl_file:
            with open(psl_file, encoding="utf-8") as fh:
                self._psl = PublicSuffixList(fh)
        else:
            self._psl = PublicSuffixList()

    def parse_domain(self, hostname: str) -> DomainParts:
        host = _clean(hostname)
        try:
            ipaddress.ip_address(host)
            return DomainParts(host, None, None, None, False, False)
        except ValueError:
            pass
        if not host:
            return DomainParts(host, None, None, None, False, False)
        known_suffix = self._psl.publicsuffix(host, accept_unknown=False)
        suffix = known_suffix or self._psl.publicsuffix(host)
        registrable = self._psl.privatesuffix(host) if suffix else None
        subdomain = None
        if registrable and host != registrable and host.endswith("." + registrable):
            subdomain = host[: -(len(registrable) + 1)]
        return DomainParts(
            hostname=host,
            subdomain=subdomain,
            registrable_domain=registrable,
            suffix=suffix,
            is_known_suffix=known_suffix is not None,
            is_public_suffix=suffix is not None and host == suffix,
        )

    def registrable_domain(self, hostname: str) -> str | None:
        return self.parse_domain(hostname).registrable_domain

    def is_known_suffix(self, hostname: str) -> bool:
        return self.parse_domain(hostname).is_known_suffix


@lru_cache(maxsize=1)
def get_domain_parser() -> DomainParser:
    return DomainParser(branding_env("PSL_FILE") or None)


def parse_domain(hostname: str) -> dict:
    """Interface funcional: ``{"hostname", "subdomain", "registrable_domain", "suffix", ...}``."""
    return get_domain_parser().parse_domain(hostname).as_dict()


def host_entity_type(hostname: str):
    """DOMAIN se o host é o próprio domínio registrável; senão SUBDOMAIN."""
    from osintizada.core.enums import EntityType

    parts = get_domain_parser().parse_domain(hostname)
    return EntityType.DOMAIN if parts.registrable_domain == parts.hostname else EntityType.SUBDOMAIN
