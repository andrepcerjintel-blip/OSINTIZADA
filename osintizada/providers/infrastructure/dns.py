"""DNSProvider — resolução DNS pública (dnspython).

Domínio/subdomínio: A, AAAA, CNAME, MX, NS, TXT (SPF interpretado).
IP: PTR (DNS reverso) + mapeamento IP→ASN da Team Cymru (consulta DNS pública:
    https://www.team-cymru.com/ip-asn-mapping).
ASN: descrição do AS pela Team Cymru (AS<n>.asn.cymru.com).

O provider só registra o que o DNS responde; não infere propriedade.
"""

from __future__ import annotations

import ipaddress
import os
from typing import Any, Protocol

import dns.asyncresolver
import dns.exception
import dns.resolver
import dns.reversename

from osintizada.core.domains import host_entity_type
from osintizada.core.enums import EntityType, IdentifierType, RelationType, SourceTier
from osintizada.core.models import EntityRef, NormalizedIdentifier, ProviderResult
from osintizada.core.normalization import normalize_host
from osintizada.core.validators import is_valid_dns_name, is_valid_hostname
from osintizada.providers.base import APIProvider, ProviderTimeout, ProviderUnavailable, register_provider

DOMAIN_RECORD_TYPES = ("A", "AAAA", "CNAME", "MX", "NS", "TXT")


class NXDomain(Exception):
    """O nome não existe (resposta válida do DNS)."""


class DNSLookup(Protocol):
    async def query(self, name: str, rtype: str) -> list[tuple[str, int | None]]:
        """Retorna [(rdata_texto, ttl)]; [] se não houver registro; NXDomain se o nome não existir."""


class DnsPythonLookup:
    """Implementação real sobre dnspython (resolver do sistema ou OSINTIZADA_DNS_SERVERS)."""

    def __init__(self, timeout: float = 5.0, nameservers: list[str] | None = None) -> None:
        self.resolver = dns.asyncresolver.Resolver()
        servers = nameservers or [s.strip() for s in os.environ.get("OSINTIZADA_DNS_SERVERS", "").split(",") if s.strip()]
        if servers:
            self.resolver.nameservers = servers
        self.timeout = timeout

    @property
    def description(self) -> str:
        return ",".join(self.resolver.nameservers)

    async def query(self, name: str, rtype: str) -> list[tuple[str, int | None]]:
        try:
            answer = await self.resolver.resolve(name, rtype, lifetime=self.timeout)
        except dns.resolver.NXDOMAIN as exc:
            raise NXDomain(name) from exc
        except dns.resolver.NoAnswer:
            return []
        except dns.exception.Timeout as exc:
            raise ProviderTimeout(f"DNS {rtype} {name}: timeout") from exc
        except dns.resolver.NoNameservers as exc:
            raise ProviderUnavailable(f"DNS {rtype} {name}: nenhum servidor respondeu", code="DNS_SERVFAIL") from exc
        ttl = answer.rrset.ttl if answer.rrset is not None else None
        return [(r.to_text(), ttl) for r in answer]


def _txt(value: str) -> str:
    """TXT vem como '"parte1" "parte2"' → 'parte1parte2'."""
    parts = [p for p in value.split('"') if p.strip()]
    return "".join(parts) if parts else value


def _host(value: str) -> str:
    return normalize_host(value.rstrip("."))


@register_provider
class DNSProvider(APIProvider):
    name = "infra.dns"
    display_name = "DNS"
    description = "Registros DNS públicos, DNS reverso e IP→ASN (Team Cymru via DNS)."
    source_tag = "DNS"
    tier = SourceTier.TIER_1
    cost_category = "dns"
    supported_identifiers = frozenset({
        IdentifierType.DOMAIN, IdentifierType.SUBDOMAIN, IdentifierType.HOSTNAME,
        IdentifierType.IPV4, IdentifierType.IPV6, IdentifierType.ASN,
    })
    default_concurrency = 10
    default_requests_per_second = 20
    default_cache_ttl_seconds = 3600
    default_timeout_seconds = 20.0
    parser_version = "1"

    def lookup(self) -> DNSLookup:
        injected = getattr(self.runtime, "dns_resolver", None)
        return injected if injected is not None else DnsPythonLookup(timeout=min(5.0, self.timeout))

    async def _search(self, identifier: NormalizedIdentifier, query: str | None) -> list[ProviderResult]:
        lookup = self.lookup()
        self._resolver_desc = getattr(lookup, "description", "custom")
        if identifier.type in (IdentifierType.IPV4, IdentifierType.IPV6):
            return await self._ip(lookup, identifier.value)
        if identifier.type == IdentifierType.ASN:
            return await self._asn(lookup, identifier.value)
        return await self._domain(lookup, identifier.value)

    # --- helpers ---------------------------------------------------------------------

    def _result(self, etype: EntityType, value: str, rtype: str, qname: str, rdata: str, ttl: int | None,
                relation: RelationType | None, reason: str | None, *, source_name: str = "DNS",
                direction: str = "forward", source_entity: EntityRef | None = None,
                attributes: dict[str, Any] | None = None) -> ProviderResult:
        return ProviderResult(
            type=etype, value=value, source_name=source_name, confidence=0.95,
            raw={"record_type": rtype, "name": qname, "rdata": rdata, "ttl": ttl, "resolver": self._resolver_desc},
            relation_to_query=relation, relation_reason=reason, relation_direction=direction,
            source_entity=source_entity, attributes=attributes or {},
        )

    async def _safe(self, lookup: DNSLookup, name: str, rtype: str, errors: dict[str, str]):
        try:
            return await lookup.query(name, rtype)
        except NXDomain:
            raise
        except Exception as exc:  # falha de um tipo não invalida os demais
            errors[rtype] = f"{type(exc).__name__}: {exc}"
            return []

    async def _domain(self, lookup: DNSLookup, domain: str) -> list[ProviderResult]:
        results: list[ProviderResult] = []
        errors: dict[str, str] = {}
        records: dict[str, list[tuple[str, int | None]]] = {}
        try:
            for rtype in DOMAIN_RECORD_TYPES:
                records[rtype] = await self._safe(lookup, domain, rtype, errors)
        except NXDomain:
            return []  # nome inexistente: resposta válida → NO_RESULTS
        if errors and len(errors) == len(DOMAIN_RECORD_TYPES):
            raise ProviderUnavailable("Todas as consultas DNS falharam: " + "; ".join(errors.values()),
                                      code="DNS_ERROR")

        for rtype in ("A", "AAAA"):
            for rdata, ttl in records.get(rtype, []):
                results.append(self._result(EntityType.IP, rdata, rtype, domain, rdata, ttl, RelationType.RESOLVES_TO,
                                            f"Registro {rtype} de {domain}", attributes={"record_type": rtype}))
        for rdata, ttl in records.get("CNAME", []):
            target = _host(rdata)
            if is_valid_dns_name(target):
                results.append(self._result(host_entity_type(target), target, "CNAME", domain, rdata, ttl,
                                            RelationType.RESOLVES_TO, f"CNAME de {domain} aponta para {target}"))
        null_mx = False
        for rdata, ttl in records.get("MX", []):
            pref, _, host = rdata.partition(" ")
            target = _host(host)
            if target in ("", "."):
                null_mx = True  # RFC 7505: domínio não recebe email
                continue
            if is_valid_hostname(target):
                results.append(self._result(host_entity_type(target), target, "MX", domain, rdata, ttl,
                                            RelationType.USES, f"Servidor de email (MX, preferência {pref}) de {domain}",
                                            attributes={"role": "mail_server"}))
        for rdata, ttl in records.get("NS", []):
            target = _host(rdata)
            if is_valid_hostname(target):
                results.append(self._result(host_entity_type(target), target, "NS", domain, rdata, ttl,
                                            RelationType.USES, f"Nameserver autoritativo (NS) de {domain}",
                                            attributes={"role": "nameserver"}))
        txt_values = [_txt(r) for r, _ in records.get("TXT", [])]
        for value in txt_values:
            if value.lower().startswith("v=spf1"):
                results.extend(self._spf(domain, value))

        # Atributos do próprio domínio consultado (sem criar relação).
        own_attrs: dict[str, Any] = {}
        if txt_values:
            own_attrs["txt_records"] = txt_values
        if null_mx:
            own_attrs["null_mx"] = True
        if own_attrs:
            results.append(self._result(host_entity_type(domain), domain, "TXT/MX", domain,
                                        "; ".join(txt_values), None, None, None, attributes=own_attrs))
        return results

    def _spf(self, domain: str, spf: str) -> list[ProviderResult]:
        out = []
        for token in spf.split()[1:]:
            mech = token.lstrip("+-~?")
            key, _, value = mech.partition(":")
            if key == "redirect" or mech.startswith("redirect="):
                key, value = "include", mech.split("=", 1)[1]
            if key == "include" and value and is_valid_dns_name(value):
                target = _host(value)
                out.append(self._result(host_entity_type(target), target, "TXT", domain, spf, None,
                                        RelationType.ASSOCIATED_WITH,
                                        f"SPF de {domain} inclui {target} (autoriza envio de email em nome do domínio)",
                                        attributes={"spf_mechanism": "include"}))
            elif key in ("ip4", "ip6") and value:
                try:
                    net = ipaddress.ip_network(value, strict=False)
                except ValueError:
                    continue
                single = net.num_addresses == 1
                out.append(self._result(EntityType.IP if single else EntityType.NETWORK,
                                        str(net.network_address) if single else str(net), "TXT", domain, spf, None,
                                        RelationType.ASSOCIATED_WITH,
                                        f"SPF de {domain} autoriza envio a partir de {value}",
                                        attributes={"spf_mechanism": key}))
        return out

    async def _ip(self, lookup: DNSLookup, ip: str) -> list[ProviderResult]:
        results: list[ProviderResult] = []
        errors: dict[str, str] = {}
        addr = ipaddress.ip_address(ip)
        ptr_name = dns.reversename.from_address(ip).to_text()
        try:
            ptr = await self._safe(lookup, ptr_name, "PTR", errors)
        except NXDomain:
            ptr = []
        for rdata, ttl in ptr:
            host = _host(rdata)
            if is_valid_hostname(host):
                results.append(self._result(host_entity_type(host), host, "PTR", ptr_name, rdata, ttl,
                                            RelationType.RESOLVES_TO, f"DNS reverso (PTR) de {ip}"))
        if addr.is_global:
            results.extend(await self._cymru_origin(lookup, addr, errors))
        if errors and not results and len(errors) >= 2:
            raise ProviderUnavailable("Consultas DNS falharam: " + "; ".join(errors.values()), code="DNS_ERROR")
        return results

    async def _cymru_origin(self, lookup: DNSLookup, addr, errors: dict[str, str]) -> list[ProviderResult]:
        if addr.version == 4:
            qname = ".".join(reversed(str(addr).split("."))) + ".origin.asn.cymru.com"
        else:
            qname = ".".join(reversed(addr.exploded.replace(":", ""))) + ".origin6.asn.cymru.com"
        try:
            answers = await self._safe(lookup, qname, "TXT", errors)
        except NXDomain:
            return []
        out = []
        source = "Team Cymru IP-to-ASN (DNS)"
        for rdata, ttl in answers:
            fields = [f.strip() for f in _txt(rdata).split("|")]
            if len(fields) < 2:
                continue
            asns, prefix = fields[0].split(), fields[1]
            attrs = {"country": fields[2] if len(fields) > 2 else None,
                     "registry": fields[3] if len(fields) > 3 else None,
                     "allocated": fields[4] if len(fields) > 4 else None}
            attrs = {k: v for k, v in attrs.items() if v}
            try:
                network = str(ipaddress.ip_network(prefix, strict=False))
            except ValueError:
                network = None
            if network:
                out.append(self._result(EntityType.NETWORK, network, "TXT", qname, rdata, ttl, RelationType.PART_OF,
                                        f"{addr} pertence ao prefixo anunciado {network}", source_name=source,
                                        attributes=attrs))
            for asn in asns:
                if asn.isdigit():
                    out.append(self._result(EntityType.ASN, f"AS{asn}", "TXT", qname, rdata, ttl,
                                            RelationType.PART_OF, f"{addr} é anunciado pelo AS{asn} (prefixo {prefix})",
                                            source_name=source))
                    if network:
                        out.append(self._result(EntityType.ASN, f"AS{asn}", "TXT", qname, rdata, ttl,
                                                RelationType.HOSTED_ON, f"Prefixo {network} anunciado pelo AS{asn}",
                                                source_name=source,
                                                source_entity=EntityRef(type=EntityType.NETWORK, value=network)))
        return out

    async def _asn(self, lookup: DNSLookup, asn: str) -> list[ProviderResult]:
        number = asn.upper().removeprefix("AS")
        qname = f"AS{number}.asn.cymru.com"
        errors: dict[str, str] = {}
        try:
            answers = await self._safe(lookup, qname, "TXT", errors)
        except NXDomain:
            return []
        if errors:
            raise ProviderUnavailable("; ".join(errors.values()), code="DNS_ERROR")
        out = []
        for rdata, ttl in answers:
            fields = [f.strip() for f in _txt(rdata).split("|")]
            if len(fields) < 5:
                continue
            attrs = {"country": fields[1], "registry": fields[2], "allocated": fields[3], "as_name": fields[4]}
            out.append(self._result(EntityType.ASN, f"AS{number}", "TXT", qname, rdata, ttl, None, None,
                                    source_name="Team Cymru ASN (DNS)",
                                    attributes={k: v for k, v in attrs.items() if v}))
        return out

    async def _healthcheck(self) -> str:
        lookup = self.lookup()
        answers = await lookup.query("iana.org", "A")
        return f"resolver {getattr(lookup, 'description', 'custom')}: {len(answers)} registro(s) A para iana.org"
