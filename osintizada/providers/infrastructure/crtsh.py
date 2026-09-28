"""CertificateTransparencyProvider — descoberta PASSIVA de subdomínios via crt.sh.

Os logs de Certificate Transparency (RFC 6962) registram todo certificado TLS
emitido publicamente. Os nomes (CN/SAN) revelam subdomínios sem tocar a
infraestrutura investigada.

Normalização: minúsculas, sem ponto final, ``*.x.example.com`` → ``x.example.com``
(marcado ``wildcard``); descarta nomes fora do domínio investigado, curingas
inválidos e duplicatas. Emails presentes em certificados viram entidades EMAIL.
"""

from __future__ import annotations

from datetime import datetime, timezone

from osintizada.core.enums import EntityType, IdentifierType, RelationType, SourceTier
from osintizada.core.models import NormalizedIdentifier, ProviderResult
from osintizada.core.validators import is_valid_hostname
from osintizada.providers.base import APIProvider, register_provider

CRTSH_ENDPOINT = "https://crt.sh/"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def normalize_ct_name(name: str) -> tuple[str | None, bool]:
    """Retorna (hostname normalizado, era_wildcard). ``None`` se inválido."""
    host = name.strip().lower().rstrip(".")
    wildcard = host.startswith("*.")
    if wildcard:
        host = host[2:]
    if "*" in host or " " in host or not is_valid_hostname(host):
        return None, wildcard
    return host, wildcard


@register_provider
class CertificateTransparencyProvider(APIProvider):
    name = "infra.crtsh"
    display_name = "Certificate Transparency (crt.sh)"
    description = "Subdomínios e emails presentes em certificados TLS públicos (logs CT via crt.sh)."
    source_tag = "CT"
    tier = SourceTier.TIER_2
    supported_identifiers = frozenset({IdentifierType.DOMAIN})
    trusted_hosts = ("crt.sh",)
    default_rate_limit_per_minute = 12  # crt.sh é um serviço comunitário: seja gentil
    default_concurrency = 2
    default_cache_ttl_seconds = 86400
    default_timeout_seconds = 90.0
    default_max_results = 500
    max_response_bytes = 50 * 1024 * 1024
    parser_version = "1"

    async def _search(self, identifier: NormalizedIdentifier, query: str | None) -> list[ProviderResult]:
        domain = identifier.value.lower().rstrip(".")
        async with self.http_client() as client:
            result = await self.fetch(client, "GET", CRTSH_ENDPOINT, params={"q": f"%.{domain}", "output": "json"},
                                      headers={"Accept": "application/json"})
        rows = result.json() if result.content.strip() else []
        return self.parse(domain, rows if isinstance(rows, list) else [])

    async def _healthcheck(self) -> str:
        async with self.http_client() as client:
            result = await self.fetch(client, "GET", CRTSH_ENDPOINT)
        return f"ok ({result.status_code})"

    def parse(self, domain: str, rows: list[dict]) -> list[ProviderResult]:
        names: dict[str, dict] = {}
        emails: dict[str, dict] = {}
        for row in rows:
            candidates = {row.get("common_name") or ""} | set(str(row.get("name_value") or "").split("\n"))
            not_before, not_after = _parse_ts(row.get("not_before")), _parse_ts(row.get("not_after"))
            for raw_name in candidates:
                raw_name = raw_name.strip()
                if not raw_name:
                    continue
                if "@" in raw_name:
                    email = raw_name.lower()
                    if email.split("@", 1)[1] == domain or email.split("@", 1)[1].endswith("." + domain):
                        agg = emails.setdefault(email, {"cert_ids": [], "first": not_before, "last": not_after})
                        _merge(agg, row, not_before, not_after)
                    continue
                host, wildcard = normalize_ct_name(raw_name)
                if host is None or not (host == domain or host.endswith("." + domain)):
                    continue  # fora do domínio investigado
                agg = names.setdefault(host, {"cert_ids": [], "issuers": set(), "wildcard": False,
                                              "first": not_before, "last": not_after})
                agg["wildcard"] = agg["wildcard"] or wildcard
                if row.get("issuer_name"):
                    agg["issuers"].add(_issuer_cn(row["issuer_name"]))
                _merge(agg, row, not_before, not_after)

        now = datetime.now(timezone.utc)
        results: list[ProviderResult] = []
        ordered = sorted(names.items(), key=lambda kv: kv[1]["last"] or datetime.min.replace(tzinfo=timezone.utc),
                         reverse=True)
        for host, agg in ordered:
            expired = agg["last"] is not None and agg["last"] < now
            attrs = {
                "certificate_count": len(agg["cert_ids"]),
                "first_seen": agg["first"].isoformat() if agg["first"] else None,
                "last_valid_until": agg["last"].isoformat() if agg["last"] else None,
                "wildcard_certificate": agg["wildcard"] or None,
                "issuers": sorted(i for i in agg["issuers"] if i)[:5] or None,
                "all_certificates_expired": expired or None,
            }
            attrs = {k: v for k, v in attrs.items() if v is not None}
            raw = {"name": host, "certificate_ids": agg["cert_ids"][:20], "source": "crt.sh"}
            source_url = f"https://crt.sh/?q={host}"
            if host == domain:
                results.append(ProviderResult(type=EntityType.DOMAIN, value=host, source_url=source_url,
                                              source_name=self.display_name, confidence=0.9, raw=raw,
                                              observed_at=agg["first"], attributes=attrs))
                continue
            results.append(ProviderResult(
                type=EntityType.SUBDOMAIN, value=host, source_url=source_url, source_name=self.display_name,
                confidence=0.9, raw=raw, observed_at=agg["first"], is_historical=expired, attributes=attrs,
                relation_to_query=RelationType.PART_OF, relation_direction="reverse",
                relation_reason=(f"{host} aparece em {len(agg['cert_ids'])} certificado(s) público(s) de {domain}"
                                 + (" — todos já expirados (dado histórico)" if expired else "")),
            ))
        for email, agg in emails.items():
            results.append(ProviderResult(
                type=EntityType.EMAIL, value=email, source_url=f"https://crt.sh/?id={agg['cert_ids'][0]}",
                source_name=self.display_name, confidence=0.85, observed_at=agg["first"],
                raw={"email": email, "certificate_ids": agg["cert_ids"][:20]},
                relation_to_query=RelationType.ASSOCIATED_WITH,
                relation_reason=f"Email presente em certificado TLS emitido para {domain}",
            ))
        return results


def _merge(agg: dict, row: dict, not_before, not_after) -> None:
    cert_id = row.get("id")
    if cert_id is not None and cert_id not in agg["cert_ids"]:
        agg["cert_ids"].append(cert_id)
    if not_before and (agg["first"] is None or not_before < agg["first"]):
        agg["first"] = not_before
    if not_after and (agg["last"] is None or not_after > agg["last"]):
        agg["last"] = not_after


def _issuer_cn(issuer: str) -> str:
    for part in issuer.split(","):
        key, _, value = part.strip().partition("=")
        if key.upper() == "CN":
            return value.strip()
    return issuer.strip()[:120]
