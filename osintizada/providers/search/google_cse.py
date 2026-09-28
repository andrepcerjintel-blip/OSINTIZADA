"""Google Programmable Search Engine — Custom Search JSON API (oficial).

Documentação: https://developers.google.com/custom-search/v1/overview
Credenciais: GOOGLE_CSE_API_KEY e GOOGLE_CSE_CX (ID do mecanismo configurado
para "pesquisar em toda a web"). O Google restringiu esta API a clientes
existentes; se a conta não tiver acesso, o provider fica NOT CONFIGURED.
"""

from __future__ import annotations

import json

import httpx

from osintizada.net.http_client import HTTPResult, SafeHTTPClient
from osintizada.providers.base import (
    AuthRequiredError,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimitedError,
    register_provider,
)
from osintizada.providers.search.base import (
    SearchEngineProvider,
    SearchHit,
    clean_snippet,
    parse_date,
)

GOOGLE_CSE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
_RATE_REASONS = {"rateLimitExceeded", "dailyLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}


def _error_reasons(result: HTTPResult) -> set[str]:
    try:
        error = json.loads(result.content).get("error", {})
    except (ValueError, AttributeError):
        return set()
    reasons = {e.get("reason") for e in error.get("errors", []) if isinstance(e, dict)}
    if error.get("status"):
        reasons.add(error["status"])
    return {r for r in reasons if r}


@register_provider
class GoogleCSEProvider(SearchEngineProvider):
    name = "search.google_cse"
    display_name = "Google Programmable Search"
    description = "Busca web via Custom Search JSON API oficial."
    required_secrets = ("GOOGLE_CSE_API_KEY", "GOOGLE_CSE_CX")
    requires_auth = True
    trusted_hosts = ("www.googleapis.com",)
    supported_operators = frozenset({"quote", "site", "filetype", "inurl", "intitle", "exclude", "or"})
    max_results_per_query = 10  # limite da API por requisição
    default_rate_limit_per_minute = 60

    async def execute_query(self, client: SafeHTTPClient, query: str) -> list[SearchHit]:
        try:
            result = await client.get(GOOGLE_CSE_ENDPOINT, params={
                "key": self.secret("GOOGLE_CSE_API_KEY") or "",
                "cx": self.secret("GOOGLE_CSE_CX") or "",
                "q": query,
                "num": self.max_results_per_query,
            })
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"{type(exc).__name__}: {exc}") from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailable(f"{type(exc).__name__}: {exc}", code="CONNECTION_ERROR") from exc
        self._raise_for_google_error(result)
        items = result.json().get("items") or []
        hits = []
        for rank, item in enumerate(items, start=1):
            if not item.get("link"):
                continue
            metatags = ((item.get("pagemap") or {}).get("metatags") or [{}])[0]
            published = metatags.get("article:published_time") or metatags.get("og:updated_time")
            hits.append(SearchHit(
                url=item["link"],
                title=clean_snippet(item.get("title")),
                snippet=clean_snippet(item.get("snippet")),
                rank=rank,
                engine=self.name,
                published_at=parse_date(published),
                raw={k: item.get(k) for k in ("link", "title", "snippet", "displayLink", "fileFormat", "mime")
                     if item.get(k) is not None},
            ))
        return hits

    async def _healthcheck(self) -> str:
        async with self.http_client() as client:
            hits = await self.execute_query(client, "osintizada")  # consome 1 consulta da quota
        return f"ok ({len(hits)} resultado(s))"

    def _raise_for_google_error(self, result: HTTPResult) -> None:
        if result.status_code < 400:
            return
        reasons = _error_reasons(result)
        if result.status_code == 403 and reasons & _RATE_REASONS:
            raise RateLimitedError(f"Quota do Google CSE excedida ({', '.join(sorted(reasons))})", code="QUOTA_EXCEEDED")
        if result.status_code == 400 and "keyInvalid" in reasons:
            raise AuthRequiredError("Chave de API do Google CSE inválida", code="INVALID_KEY")
        self.raise_for_status(result, auth_statuses=(401, 403))
