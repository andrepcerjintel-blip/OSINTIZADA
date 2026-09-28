"""Cliente HTTP seguro para providers.

  * proteção SSRF em toda URL e em cada redirecionamento (seguidos manualmente);
  * timeouts obrigatórios;
  * limite de tamanho de resposta (streaming);
  * mapeamento padronizado de erros HTTP para os erros tipados de provider;
  * User-Agent identificável.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin

import httpx

from osintizada.net.ssrf import Resolver, UnsafeURLError, validate_url

DEFAULT_USER_AGENT = "OSINTIZADA/0.2 (+investigation research tool)"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class ResponseTooLargeError(Exception):
    pass


@dataclass
class HTTPResult:
    status_code: int
    url: str
    headers: httpx.Headers
    content: bytes
    elapsed_ms: float
    redirects: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.content.decode(_charset(self.headers), errors="replace")

    def json(self) -> Any:
        import json

        return json.loads(self.content)


def _charset(headers: httpx.Headers) -> str:
    ctype = headers.get("content-type", "")
    for part in ctype.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            return part.split("=", 1)[1].strip("\"'") or "utf-8"
    return "utf-8"


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Converte o header Retry-After (segundos ou HTTP-date) em segundos."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())


class SafeHTTPClient:
    def __init__(
        self,
        timeout: float = 20.0,
        user_agent: str = DEFAULT_USER_AGENT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_redirects: int = 5,
        trusted_hosts: Iterable[str] = (),
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
    ) -> None:
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.trusted_hosts = tuple(trusted_hosts)
        self.resolver = resolver
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(timeout),
            "headers": {"User-Agent": user_agent},
            "follow_redirects": False,
        }
        if transport is not None:
            kwargs["transport"] = transport
        if proxy is not None:
            kwargs["proxy"] = proxy
        self._client = httpx.AsyncClient(**kwargs)

    async def __aenter__(self) -> SafeHTTPClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json: Any = None,
        data: Any = None,
    ) -> HTTPResult:
        redirects: list[str] = []
        current = url
        for _ in range(self.max_redirects + 1):
            await validate_url(current, resolver=self.resolver, trusted_hosts=self.trusted_hosts)
            req = self._client.build_request(method, current, params=params, headers=headers, json=json, data=data)
            resp = await self._client.send(req, stream=True)
            try:
                if resp.status_code in REDIRECT_STATUSES and "location" in resp.headers:
                    redirects.append(str(resp.url))
                    current = urljoin(str(resp.url), resp.headers["location"])
                    params = None  # a query já está na Location
                    if resp.status_code == 303:
                        method, json, data = "GET", None, None
                    continue
                content = await self._read_limited(resp)
                return HTTPResult(
                    status_code=resp.status_code,
                    url=str(resp.url),
                    headers=resp.headers,
                    content=content,
                    elapsed_ms=resp.elapsed.total_seconds() * 1000 if _has_elapsed(resp) else 0.0,
                    redirects=redirects,
                )
            finally:
                await resp.aclose()
        raise UnsafeURLError(f"Excesso de redirecionamentos (> {self.max_redirects})")

    async def get(self, url: str, **kwargs: Any) -> HTTPResult:
        return await self.request("GET", url, **kwargs)

    async def _read_limited(self, resp: httpx.Response) -> bytes:
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            raise ResponseTooLargeError(f"Resposta declarada com {declared} bytes (limite {self.max_bytes})")
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > self.max_bytes:
                raise ResponseTooLargeError(f"Resposta excede {self.max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)


def _has_elapsed(resp: httpx.Response) -> bool:
    try:
        resp.elapsed  # noqa: B018 - só disponível após fechar/ler
        return True
    except RuntimeError:
        return False
