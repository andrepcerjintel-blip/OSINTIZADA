"""Brave Search API (oficial).

Documentação: https://api-dashboard.search.brave.com/app/documentation/web-search
Autenticação: header ``X-Subscription-Token`` (variável BRAVE_SEARCH_API_KEY).
"""

from __future__ import annotations

from osintizada.net.http_client import SafeHTTPClient
from osintizada.providers.base import register_provider
from osintizada.providers.search.base import (
    SearchEngineProvider,
    SearchHit,
    clean_snippet,
    parse_date,
)

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


@register_provider
class BraveSearchProvider(SearchEngineProvider):
    name = "search.brave"
    display_name = "Brave Search"
    description = "Busca web via Brave Search API oficial."
    required_secrets = ("BRAVE_SEARCH_API_KEY",)
    requires_auth = True
    trusted_hosts = ("api.search.brave.com",)
    # Brave não documenta inurl:; usa ext:/filetype:, intitle:, site:, aspas, -, OR.
    supported_operators = frozenset({"quote", "site", "filetype", "intitle", "exclude", "or"})
    max_results_per_query = 20
    default_rate_limit_per_minute = 60  # plano gratuito: 1 req/s

    async def execute_query(self, client: SafeHTTPClient, query: str) -> list[SearchHit]:
        result = await self.fetch(
            client, "GET", BRAVE_ENDPOINT,
            params={"q": query, "count": self.max_results_per_query},
            headers={"Accept": "application/json", "X-Subscription-Token": self.secret("BRAVE_SEARCH_API_KEY") or ""},
            auth_statuses=(401, 403),
        )
        payload = result.json()
        items = (payload.get("web") or {}).get("results") or []
        hits = []
        for rank, item in enumerate(items, start=1):
            url = item.get("url")
            if not url:
                continue
            hits.append(SearchHit(
                url=url,
                title=clean_snippet(item.get("title")),
                snippet=clean_snippet(" ".join([item.get("description") or "", *(item.get("extra_snippets") or [])])),
                rank=rank,
                engine=self.name,
                published_at=parse_date(item.get("page_age")),
                raw={k: item.get(k) for k in ("url", "title", "description", "age", "page_age", "language")
                     if item.get(k) is not None},
            ))
        return hits

    async def _healthcheck(self) -> str:
        async with self.http_client() as client:
            result = await self.fetch(
                client, "GET", BRAVE_ENDPOINT, params={"q": "osintizada", "count": 1},
                headers={"Accept": "application/json",
                         "X-Subscription-Token": self.secret("BRAVE_SEARCH_API_KEY") or ""},
                auth_statuses=(401, 403),
            )
        remaining = result.headers.get("x-ratelimit-remaining")
        return f"quota restante: {remaining}" if remaining else "ok"
