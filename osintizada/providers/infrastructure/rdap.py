"""RDAPProvider — Registration Data Access Protocol (RFC 9082/9083).

Usa o bootstrap público https://rdap.org, que redireciona para o servidor RDAP
autoritativo (ARIN, RIPE, LACNIC, registro.br, Verisign...). Cada redirecionamento
passa pelo SafeHTTPClient (validação SSRF) e a URL final vira ``source_url``.

Somente registra o que a fonte declara: papéis (registrant, registrar, abuse)
são mantidos como estão; nada de inferir "dono" além disso. Campos redigidos
(privacidade/GDPR) são marcados como tal, nunca preenchidos.
"""

from __future__ import annotations

import ipaddress
import os
import re
from typing import Any

from osintizada.core.domains import host_entity_type
from osintizada.core.enums import EntityType, IdentifierType, RelationType, SourceTier
from osintizada.core.models import EntityRef, NormalizedIdentifier, ProviderResult
from osintizada.core.normalization import normalize_host
from osintizada.core.validators import is_valid_hostname
from osintizada.providers.base import APIProvider, register_provider

DEFAULT_RDAP_BASE = "https://rdap.org"
_REDACTED = re.compile(r"(?i)redacted|data protected|privacy|withheld|not disclosed|gdpr")


def vcard(entity: dict) -> dict[str, Any]:
    """Extrai campos úteis de ``vcardArray`` (jCard)."""
    out: dict[str, Any] = {"emails": [], "phones": []}
    arr = entity.get("vcardArray")
    if not isinstance(arr, list) or len(arr) < 2:
        return out
    for prop in arr[1]:
        if not isinstance(prop, list) or len(prop) < 4:
            continue
        name, params, value = prop[0], prop[1] or {}, prop[3]
        if name == "fn" and isinstance(value, str):
            out["fn"] = value.strip()
        elif name == "org":
            out["org"] = value if isinstance(value, str) else " ".join(map(str, value))
        elif name == "kind":
            out["kind"] = value
        elif name == "email" and isinstance(value, str):
            out["emails"].append(value.strip())
        elif name == "tel":
            out["phones"].append(str(value).removeprefix("tel:"))
        elif name == "adr":
            label = params.get("label") if isinstance(params, dict) else None
            if label:
                out["address"] = " ".join(str(label).split())
            if isinstance(value, list) and value and value[-1]:
                out["country"] = str(value[-1])
    return out


def events(obj: dict) -> dict[str, str]:
    return {e["eventAction"].replace(" ", "_"): e["eventDate"] for e in obj.get("events", [])
            if isinstance(e, dict) and e.get("eventAction") and e.get("eventDate")}


def walk_entities(obj: dict, depth: int = 0):
    """Entidades aninhadas (ARIN/RIPE colocam 'abuse' dentro da organização)."""
    for ent in obj.get("entities", []) or []:
        if isinstance(ent, dict):
            yield ent
            if depth < 3:
                yield from walk_entities(ent, depth + 1)


def is_redacted(value: str | None) -> bool:
    return bool(value) and bool(_REDACTED.search(value))


@register_provider
class RDAPProvider(APIProvider):
    name = "infra.rdap"
    display_name = "RDAP"
    description = "Dados de registro de IPs, ASNs e domínios via RDAP (bootstrap rdap.org)."
    source_tag = "RDAP"
    tier = SourceTier.TIER_1
    supported_identifiers = frozenset({IdentifierType.IPV4, IdentifierType.IPV6, IdentifierType.ASN,
                                       IdentifierType.DOMAIN, IdentifierType.CIDR})
    trusted_hosts = ("rdap.org",)
    default_requests_per_second = 1
    default_concurrency = 4
    default_cache_ttl_seconds = 86400
    default_timeout_seconds = 30.0
    parser_version = "1"

    @property
    def base_url(self) -> str:
        return os.environ.get("OSINTIZADA_RDAP_BASE", DEFAULT_RDAP_BASE).rstrip("/")

    def _path(self, identifier: NormalizedIdentifier) -> str:
        if identifier.type == IdentifierType.ASN:
            return f"autnum/{identifier.value.upper().removeprefix('AS')}"
        if identifier.type == IdentifierType.DOMAIN:
            return f"domain/{identifier.value}"
        if identifier.type == IdentifierType.CIDR:
            return f"ip/{identifier.value}"
        return f"ip/{identifier.value}"

    async def _search(self, identifier: NormalizedIdentifier, query: str | None) -> list[ProviderResult]:
        url = f"{self.base_url}/{self._path(identifier)}"
        async with self.http_client() as client:
            result = await self.fetch(client, "GET", url, headers={"Accept": "application/rdap+json"},
                                      not_found_ok=True)
        if result is None:
            return []  # 404: objeto não existe no registro → NO_RESULTS
        data = result.json()
        source_url = result.url
        klass = data.get("objectClassName")
        if klass == "ip network":
            return self._ip_network(identifier, data, source_url)
        if klass == "autnum":
            return self._autnum(identifier, data, source_url)
        if klass == "domain":
            return self._domain(identifier, data, source_url)
        return []

    # --- construção de resultados ------------------------------------------------------

    def _res(self, etype: EntityType, value: str, source_url: str, raw: dict, relation: RelationType | None,
             reason: str | None, *, source_entity: EntityRef | None = None, attributes: dict | None = None,
             direction: str = "forward", confidence: float = 0.95) -> ProviderResult:
        return ProviderResult(
            type=etype, value=value, source_url=source_url, source_name=f"RDAP ({_host_of(source_url)})",
            confidence=confidence, raw=raw, relation_to_query=relation, relation_reason=reason,
            source_entity=source_entity, relation_direction=direction,
            attributes={k: v for k, v in (attributes or {}).items() if v not in (None, "", [], {})},
        )

    def _parties(self, data: dict, source_url: str, owner: EntityRef, owner_label: str) -> list[ProviderResult]:
        out: list[ProviderResult] = []
        for ent in walk_entities(data):
            roles = [r.lower() for r in ent.get("roles", [])]
            card = vcard(ent)
            name = card.get("org") or card.get("fn")
            raw = {"handle": ent.get("handle"), "roles": roles, "vcard": card}
            if "registrant" in roles and name and not is_redacted(name):
                etype = EntityType.PERSON if card.get("kind") == "individual" else EntityType.ORGANIZATION
                out.append(self._res(etype, name, source_url, raw, RelationType.REGISTERED_TO,
                                     f"{owner_label} registrado para '{name}' (papel RDAP: registrant)",
                                     source_entity=owner,
                                     attributes={"rdap_handle": ent.get("handle"), "country": card.get("country")}))
            elif "registrar" in roles and name and not is_redacted(name):
                out.append(self._res(EntityType.ORGANIZATION, name, source_url, raw, RelationType.ASSOCIATED_WITH,
                                     f"Registrar de {owner_label} segundo o RDAP", source_entity=owner,
                                     attributes={"role": "registrar", "iana_id": _public_id(ent)}))
            for email in card["emails"]:
                if "@" in email and not is_redacted(email):
                    role = "abuse" if "abuse" in roles else ",".join(roles) or "contato"
                    out.append(self._res(EntityType.EMAIL, email.lower(), source_url, raw,
                                         RelationType.ASSOCIATED_WITH,
                                         f"Email de contato ({role}) de {owner_label} no RDAP", source_entity=owner,
                                         attributes={"role": role}, confidence=0.9))
        return out

    def _ip_network(self, ident: NormalizedIdentifier, data: dict, source_url: str) -> list[ProviderResult]:
        cidrs = []
        for c in data.get("cidr0_cidrs", []) or []:
            prefix = c.get("v4prefix") or c.get("v6prefix")
            if prefix and c.get("length") is not None:
                cidrs.append(f"{prefix}/{c['length']}")
        if not cidrs and data.get("startAddress") and data.get("endAddress"):
            try:
                cidrs = [str(n) for n in ipaddress.summarize_address_range(
                    ipaddress.ip_address(data["startAddress"]), ipaddress.ip_address(data["endAddress"]))]
            except (ValueError, TypeError):
                cidrs = []
        attrs = {"rdap_handle": data.get("handle"), "network_name": data.get("name"), "country": data.get("country"),
                 "allocation_type": data.get("type"), "parent_handle": data.get("parentHandle"),
                 "range": f"{data.get('startAddress')} - {data.get('endAddress')}", **events(data)}
        raw = {k: data.get(k) for k in ("handle", "name", "type", "country", "startAddress", "endAddress",
                                        "parentHandle", "port43", "status")}
        out: list[ProviderResult] = []
        for cidr in cidrs[:8]:
            try:
                cidr = str(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                continue
            owner = EntityRef(type=EntityType.NETWORK, value=cidr)
            if ident.type != IdentifierType.CIDR or cidr != ident.value:
                out.append(self._res(EntityType.NETWORK, cidr, source_url, raw, RelationType.PART_OF,
                                     f"{ident.value} está no bloco {cidr} ({data.get('name') or data.get('handle')})",
                                     attributes=attrs))
            else:
                out.append(self._res(EntityType.NETWORK, cidr, source_url, raw, None, None, attributes=attrs))
            out.extend(self._parties(data, source_url, owner, f"bloco {cidr}"))
        for asn in data.get("arin_originas0_originautnums", []) or []:
            out.append(self._res(EntityType.ASN, f"AS{asn}", source_url, {"originautnum": asn}, RelationType.PART_OF,
                                 f"Origin AS de {ident.value} informado pelo RDAP"))
        return out

    def _autnum(self, ident: NormalizedIdentifier, data: dict, source_url: str) -> list[ProviderResult]:
        asn = f"AS{data.get('startAutnum') or ident.value.upper().removeprefix('AS')}"
        attrs = {"rdap_handle": data.get("handle"), "as_name": data.get("name"), "country": data.get("country"),
                 **events(data)}
        raw = {k: data.get(k) for k in ("handle", "name", "country", "startAutnum", "endAutnum", "port43")}
        out = [self._res(EntityType.ASN, asn, source_url, raw, None, None, attributes=attrs)]
        out.extend(self._parties(data, source_url, EntityRef(type=EntityType.ASN, value=asn), asn))
        return out

    def _domain(self, ident: NormalizedIdentifier, data: dict, source_url: str) -> list[ProviderResult]:
        domain = (data.get("ldhName") or ident.value).lower()
        secure = data.get("secureDNS") or {}
        attrs = {"status": data.get("status"), "dnssec": secure.get("delegationSigned"),
                 "rdap_handle": data.get("handle"), **events(data)}
        raw = {k: data.get(k) for k in ("handle", "ldhName", "status", "port43")}
        out = [self._res(host_entity_type(domain), domain, source_url, raw, None, None, attributes=attrs)]
        for ns in data.get("nameservers", []) or []:
            host = normalize_host(str(ns.get("ldhName", "")))
            if host and is_valid_hostname(host):
                out.append(self._res(host_entity_type(host), host, source_url, {"nameserver": ns.get("ldhName")},
                                     RelationType.USES, f"Nameserver de {domain} delegado no registro (RDAP)",
                                     attributes={"role": "nameserver"}))
        out.extend(self._parties(data, source_url, EntityRef(type=host_entity_type(domain), value=domain), domain))
        return out

    async def _healthcheck(self) -> str:
        async with self.http_client() as client:
            result = await self.fetch(client, "GET", f"{self.base_url}/autnum/15169",
                                      headers={"Accept": "application/rdap+json"})
        return f"ok ({result.status_code})"


def _host_of(url: str) -> str:
    return re.sub(r"^https?://", "", url).split("/", 1)[0]


def _public_id(entity: dict) -> str | None:
    for pid in entity.get("publicIds", []) or []:
        if isinstance(pid, dict) and pid.get("identifier"):
            return str(pid["identifier"])
    return None
