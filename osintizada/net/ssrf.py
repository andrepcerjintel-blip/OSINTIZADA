"""Proteção contra SSRF.

Toda URL vinda de fonte não confiável (input do investigador, conteúdo
coletado, redirecionamentos) é validada antes da requisição:
  * somente http/https;
  * sem credenciais embutidas (user:pass@);
  * o host é resolvido e TODOS os IPs precisam ser globais (bloqueia loopback,
    redes privadas, link-local/metadados de nuvem, multicast, reservados).
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "metadata.google.internal", "metadata"})

Resolver = Callable[[str], Awaitable[list[str]] | list[str]]


class UnsafeURLError(ValueError):
    """URL recusada pela política de SSRF."""


def is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


async def system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


async def _resolve(resolver: Resolver, host: str) -> list[str]:
    result = resolver(host)
    if inspect.isawaitable(result):
        result = await result
    return list(result)


async def validate_url(
    url: str,
    resolver: Resolver | None = None,
    trusted_hosts: Iterable[str] = (),
) -> str:
    """Valida a URL e retorna o hostname. Levanta ``UnsafeURLError`` se inseguro.

    ``trusted_hosts`` (endpoints fixos de APIs declarados no código do provider)
    dispensam a resolução DNS, mas ainda exigem esquema válido.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"Esquema não permitido: {parts.scheme or '(vazio)'}")
    if parts.username or parts.password:
        raise UnsafeURLError("URL com credenciais embutidas não é permitida")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeURLError("URL sem host")
    if host in {h.lower() for h in trusted_hosts}:
        return host
    if host in BLOCKED_HOSTNAMES or host.endswith(".localhost") or host.endswith(".internal"):
        raise UnsafeURLError(f"Host bloqueado: {host}")

    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            addresses = await _resolve(resolver or system_resolver, host)
        except (OSError, socket.gaierror) as exc:
            raise UnsafeURLError(f"Não foi possível resolver {host}: {exc}") from exc
    if not addresses:
        raise UnsafeURLError(f"Host sem endereços: {host}")
    for ip in addresses:
        if not is_public_ip(ip):
            raise UnsafeURLError(f"Host {host} resolve para endereço não público ({ip})")
    return host
