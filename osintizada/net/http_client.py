"""Cliente HTTP seguro para providers.

  * proteção SSRF em toda URL e em cada redirecionamento (seguidos manualmente);
  * timeouts obrigatórios;
  * limite de tamanho de resposta (streaming);
  * mapeamento padronizado de erros HTTP para os erros tipados de provider;
  * User-Agent identificável.
"""

from __future__ import annotations

import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin

import httpx

from osintizada.net.ssrf import (
    DEFAULT_ALLOWED_PORTS,
    Resolver,
    UnsafeURLError,
    resolve_and_validate,
)

DEFAULT_USER_AGENT = "RINO/0.5 (+investigation research tool)"
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
    connected_ips: list[str | None] = field(default_factory=list)  # IP efetivamente usado em cada salto

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


def https_proxy_for(host: str) -> bool:
    """True se requisições HTTPS para ``host`` sairão por um proxy do ambiente (HTTPS_PROXY)."""
    proxies = urllib.request.getproxies() or {}
    if not (proxies.get("https") or proxies.get("all")):
        return False
    return not urllib.request.proxy_bypass_environment(host)


def proxy_resolves_dns() -> bool:
    """True se o proxy configurado resolve DNS do lado dele (SOCKS com 'h'): não dá para pinar."""
    for value in (urllib.request.getproxies() or {}).values():
        if str(value).lower().startswith(("socks5h://", "socks4a://")):
            return True
    return False


class SafeHTTPClient:
    """Cliente HTTP com proteção SSRF e anti-DNS-rebinding por IP pinning.

    Para cada salto (requisição inicial e cada redirecionamento):
      resolve → valida TODOS os IPs → conecta no IP validado → Host/SNI = hostname original.
    Nenhuma nova resolução acontece entre validação e conexão.
    """

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
        allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS,
        blocked_networks: Iterable[str] = (),
        proxy_policy: str = "pin",
    ) -> None:
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.trusted_hosts = tuple(trusted_hosts)
        self.resolver = resolver
        self.allowed_ports = frozenset(allowed_ports)
        self.blocked_networks = tuple(blocked_networks)
        # Com proxy que resolve DNS, o pin não é garantido → URLs arbitrárias são recusadas.
        self.deny_untrusted = proxy_policy == "deny" or (transport is None and proxy_resolves_dns())
        self._env_proxies = transport is None and proxy is None
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
        connected: list[str | None] = []
        logical = str(httpx.URL(url, params=params)) if params else url
        for _ in range(self.max_redirects + 1):
            target = await resolve_and_validate(
                logical, resolver=self.resolver, trusted_hosts=self.trusted_hosts,
                allowed_ports=self.allowed_ports, extra_networks=self.blocked_networks)
            if self.deny_untrusted and not target.trusted:
                raise UnsafeURLError("Proxy configurado resolve DNS remotamente; URL arbitrária não permitida")
            if (target.pinned_ip and target.scheme == "https" and self._env_proxies
                    and https_proxy_for(target.host)):
                # Túnel CONNECT não preserva SNI com IP pinado, e tunelar pelo hostname deixaria o
                # proxy resolver DNS (rebinding). Falha fechada para URLs não confiáveis.
                raise UnsafeURLError("HTTPS via proxy não permite IP pinning; URL não confiável recusada")
            req = self._pinned_request(method, logical, target, headers, json, data)
            connected.append(target.pinned_ip)
            resp = await self._client.send(req, stream=True)
            try:
                if resp.status_code in REDIRECT_STATUSES and "location" in resp.headers:
                    redirects.append(logical)
                    # Relativo à URL LÓGICA (hostname), nunca ao IP pinado.
                    logical = urljoin(logical, resp.headers["location"])
                    if resp.status_code == 303:
                        method, json, data = "GET", None, None
                    continue
                content = await self._read_limited(resp)
                return HTTPResult(
                    status_code=resp.status_code,
                    url=logical,
                    headers=resp.headers,
                    content=content,
                    elapsed_ms=resp.elapsed.total_seconds() * 1000 if _has_elapsed(resp) else 0.0,
                    redirects=redirects,
                    connected_ips=connected,
                )
            finally:
                await resp.aclose()
        raise UnsafeURLError(f"Excesso de redirecionamentos (> {self.max_redirects})")

    def _pinned_request(self, method: str, logical: str, target, headers, json, data) -> httpx.Request:
        url = httpx.URL(logical)
        extensions: dict[str, Any] = {}
        merged = dict(headers or {})
        if target.pinned_ip is not None:
            default_port = 443 if url.scheme == "https" else 80
            host_header = target.host if target.port == default_port else f"{target.host}:{target.port}"
            merged["Host"] = host_header
            url = url.copy_with(host=target.pinned_ip)
            if url.scheme == "https":
                extensions["sni_hostname"] = target.host  # TLS valida o certificado do hostname original
        return self._client.build_request(method, url, headers=merged, json=json, data=data, extensions=extensions)

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
