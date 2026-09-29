"""ArchiveProvider — Internet Archive / Wayback Machine (CDX Server API).

Documentação: https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server

TODO resultado deste provider é HISTORICAL_DATA: descreve o que existiu em
algum momento, nunca a situação atual. ``observed_at`` = data da captura.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

from osintizada.core.domains import host_entity_type
from osintizada.core.enums import (
    EntityType,
    IdentifierType,
    RelationType,
    SourceTier,
    SourceType,
)
from osintizada.core.models import NormalizedIdentifier, ProviderResult
from osintizada.core.urls import canonical_url
from osintizada.core.validators import is_valid_hostname
from osintizada.providers.base import APIProvider, register_provider

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
FIELDS = "timestamp,original,statuscode,mimetype,digest"


def parse_wayback_ts(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts[:14].ljust(14, "0"), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@register_provider
class WaybackProvider(APIProvider):
    name = "archive.wayback"
    display_name = "Internet Archive (Wayback Machine)"
    description = "URLs, páginas e hosts históricos arquivados (CDX API). Sempre dado histórico."
    source_tag = "ARCHIVE"
    tier = SourceTier.TIER_5
    cost_category = "archive"
    supported_identifiers = frozenset({IdentifierType.DOMAIN, IdentifierType.SUBDOMAIN, IdentifierType.URL})
    trusted_hosts = ("web.archive.org",)
    default_rate_limit_per_minute = 30
    default_concurrency = 2
    default_cache_ttl_seconds = 7 * 86400
    default_timeout_seconds = 60.0
    default_max_results = 200
    parser_version = "1"

    async def _search(self, identifier: NormalizedIdentifier, query: str | None) -> list[ProviderResult]:
        if identifier.type == IdentifierType.URL:
            target = identifier.metadata.get("original_url") or identifier.value
            params = {"url": target, "matchType": "exact", "collapse": "digest"}
        else:
            target = identifier.value
            params = {"url": target, "matchType": "domain", "collapse": "urlkey"}
        params.update({"output": "json", "fl": FIELDS, "limit": str(self.max_results), "filter": "statuscode:200"})
        async with self.http_client() as client:
            result = await self.fetch(client, "GET", CDX_ENDPOINT, params=params)
        rows = result.json() if result.content.strip() else []
        return self.parse(identifier, rows if isinstance(rows, list) else [])

    async def _healthcheck(self) -> str:
        async with self.http_client() as client:
            result = await self.fetch(client, "GET", CDX_ENDPOINT,
                                      params={"url": "example.com", "limit": "1", "output": "json"})
        return f"ok ({result.status_code})"

    def parse(self, identifier: NormalizedIdentifier, rows: list) -> list[ProviderResult]:
        if not rows:
            return []
        header, data = rows[0], rows[1:]
        results: list[ProviderResult] = []
        hosts: dict[str, dict] = {}
        domain = identifier.value if identifier.type != IdentifierType.URL else None
        for row in data:
            item = dict(zip(header, row, strict=False))
            ts, original = item.get("timestamp", ""), item.get("original", "")
            archived_at = parse_wayback_ts(ts)
            if not original or archived_at is None:
                continue
            snapshot = f"https://web.archive.org/web/{ts}/{original}"
            attrs = {"archived_at": archived_at.isoformat(), "status_code": item.get("statuscode"),
                     "mimetype": item.get("mimetype"), "snapshot_url": snapshot}
            if identifier.type == IdentifierType.URL:
                # Capturas da própria URL: atributos históricos da entidade consultada.
                results.append(self._res(EntityType.URL, identifier.value, snapshot, item, archived_at, None, None,
                                         attrs))
                continue
            try:
                page = canonical_url(original)
            except ValueError:
                continue
            results.append(self._res(EntityType.URL, page, snapshot, item, archived_at, RelationType.HOSTED_ON,
                                     f"URL arquivada em {archived_at.date()} sob {domain} (dado histórico)", attrs,
                                     direction="reverse"))
            host = (urlsplit(original if "://" in original else "http://" + original).hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
            if host and host != domain and host.endswith("." + (domain or "")) and is_valid_hostname(host):
                agg = hosts.setdefault(host, {"first": archived_at, "last": archived_at, "count": 0,
                                              "snapshot": snapshot, "item": item})
                agg["count"] += 1
                agg["first"] = min(agg["first"], archived_at)
                agg["last"] = max(agg["last"], archived_at)
        for host, agg in hosts.items():
            results.append(self._res(
                host_entity_type(host), host, agg["snapshot"], agg["item"], agg["first"], RelationType.PART_OF,
                f"Host {host} observado em {agg['count']} URL(s) arquivada(s) entre {agg['first'].date()} e "
                f"{agg['last'].date()} (dado histórico)",
                {"first_archived": agg["first"].isoformat(), "last_archived": agg["last"].isoformat(),
                 "archived_urls": agg["count"]}, direction="reverse"))
        return results

    def _res(self, etype, value, source_url, item, observed_at, relation, reason, attrs, direction="forward"):
        return ProviderResult(
            type=etype, value=value, source_url=source_url, source_name=self.display_name, confidence=0.8,
            raw={"cdx": item}, observed_at=observed_at, is_historical=True, source_type=SourceType.ARCHIVE,
            relation_to_query=relation, relation_reason=reason, relation_direction=direction,
            attributes={k: v for k, v in attrs.items() if v is not None},
        )
