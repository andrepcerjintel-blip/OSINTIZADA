"""Proteção contra SSRF e DNS rebinding.

Política (aplicada a TODA URL não confiável e a CADA redirecionamento):

1. esquema: só ``http``/``https`` (``file:``, ``ftp:``, ``gopher:``, ``dict:``, ``data:``,
   ``javascript:`` e qualquer outro são recusados);
2. sem credenciais na URL (``user:pass@host``);
3. porta: apenas as permitidas (padrão 80/443; providers podem declarar outras);
4. host textual: ``localhost`` e variantes, ``*.localhost``, ``*.internal``, ``*.local`` e
   hostnames de metadata de nuvem são recusados — mas isso é só a primeira barreira;
5. o host é RESOLVIDO e **todos** os endereços precisam ser públicos. Se QUALQUER endereço
   for proibido, a URL inteira é recusada (política "all-or-nothing": um atacante não
   consegue misturar um IP público com um interno na mesma resposta DNS);
6. endereços embutidos também são verificados (IPv4-mapped, NAT64 ``64:ff9b::/96``,
   6to4 ``2002::/16``, Teredo);
7. **pinning**: o cliente HTTP conecta no IP validado (não resolve de novo) e envia o
   hostname original no ``Host`` e no SNI do TLS — elimina a janela TOCTOU do DNS
   rebinding. O pin vale só para aquela requisição; a próxima resolve e valida de novo.

``trusted_hosts`` são endpoints fixos de API declarados no código do provider (ex.:
``rdap.org``): dispensam a resolução, mas não o restante da validação; qualquer
redirecionamento para outro host volta a ser tratado como não confiável.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}
DEFAULT_ALLOWED_PORTS = frozenset({80, 443})

BLOCKED_HOSTNAMES = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "localhost6",
    "localhost6.localdomain6", "metadata", "metadata.google.internal", "instance-data",
    "instance-data.ec2.internal", "metadata.azure.internal", "kubernetes.default",
})
BLOCKED_SUFFIXES = (".localhost", ".internal", ".local", ".localdomain", ".home.arpa", ".svc", ".cluster.local")

# Endpoints de metadata de nuvem (bloqueados explicitamente, além das faixas não públicas).
METADATA_ADDRESSES = frozenset(ipaddress.ip_address(a) for a in (
    "169.254.169.254",  # AWS/GCP/Azure/OpenStack
    "169.254.170.2",    # AWS ECS task metadata
    "169.254.169.253",  # AWS DNS
    "100.100.100.200",  # Alibaba Cloud
    "fd00:ec2::254",    # AWS IMDS IPv6
))

# Faixas explicitamente proibidas (documentação; a stdlib já as considera não globais).
EXPLICIT_BLOCKED = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4",
    "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8",
))
NAT64 = ipaddress.ip_network("64:ff9b::/96")

Resolver = Callable[[str], Awaitable[list[str]] | list[str]]


class UnsafeURLError(ValueError):
    """URL recusada pela política de SSRF."""


@dataclass(frozen=True)
class ValidatedTarget:
    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]
    pinned_ip: str | None  # None apenas para trusted_hosts (endpoint fixo do código)
    trusted: bool


def _embedded(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> list:
    """Endereços IPv4 embutidos em formatos IPv6 de transição."""
    if not isinstance(addr, ipaddress.IPv6Address):
        return []
    found = []
    if addr.ipv4_mapped:
        found.append(addr.ipv4_mapped)
    if addr.sixtofour:
        found.append(addr.sixtofour)
    if addr.teredo:
        found.extend(addr.teredo)
    if addr in NAT64:
        found.append(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
    return found


def ip_block_reason(ip: str, extra_networks: Iterable[ipaddress._BaseNetwork] = ()) -> str | None:
    """Motivo do bloqueio, ou ``None`` se o endereço é público e permitido."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])  # remove zone id (fe80::1%eth0)
    except ValueError:
        return f"endereço inválido: {ip}"
    for candidate in [addr, *_embedded(addr)]:
        if candidate in METADATA_ADDRESSES:
            return f"endpoint de metadata de nuvem ({candidate})"
        if any(candidate in net for net in EXPLICIT_BLOCKED if net.version == candidate.version):
            return f"faixa não pública ({candidate})"
        if (not candidate.is_global or candidate.is_loopback or candidate.is_private or candidate.is_link_local
                or candidate.is_multicast or candidate.is_unspecified or candidate.is_reserved):
            return f"endereço não público ({candidate})"
        for net in extra_networks:
            if candidate.version == net.version and candidate in net:
                return f"rede interna configurada ({net})"
    return None


def is_public_ip(ip: str) -> bool:
    return ip_block_reason(ip) is None


async def system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


async def _resolve(resolver: Resolver, host: str) -> list[str]:
    result = resolver(host)
    if inspect.isawaitable(result):
        result = await result
    return list(result)


async def resolve_and_validate(
    url: str,
    *,
    resolver: Resolver | None = None,
    trusted_hosts: Iterable[str] = (),
    allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS,
    extra_networks: Iterable[str] = (),
) -> ValidatedTarget:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"Esquema não permitido: {scheme or '(vazio)'}")
    if parts.username is not None or parts.password is not None or "@" in (parts.netloc or ""):
        raise UnsafeURLError("URL com credenciais embutidas não é permitida")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeURLError("URL sem host")
    try:
        port = parts.port or DEFAULT_PORTS[scheme]
    except ValueError as exc:
        raise UnsafeURLError(f"Porta inválida em {url}") from exc

    if host == "onion" or host.endswith(".onion"):
        # Onion só pelo cliente Tor dedicado (TorProvider); o HTTP comum nunca vira rota para a rede Tor.
        raise UnsafeURLError(f"Endereço .onion exige o cliente Tor dedicado: {host}")
    if host in {h.lower() for h in trusted_hosts}:
        return ValidatedTarget(url, scheme, host, port, (), None, True)

    if port not in set(allowed_ports):
        raise UnsafeURLError(f"Porta {port} não permitida para URL não confiável")
    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
        raise UnsafeURLError(f"Host bloqueado: {host}")

    networks = [ipaddress.ip_network(n, strict=False) for n in extra_networks]
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        addresses = [host]
    except ValueError:
        try:
            addresses = await _resolve(resolver or system_resolver, host)
        except (OSError, socket.gaierror) as exc:
            raise UnsafeURLError(f"Não foi possível resolver {host}: {exc}") from exc
    if not addresses:
        raise UnsafeURLError(f"Host sem endereços: {host}")
    for ip in addresses:  # all-or-nothing
        if reason := ip_block_reason(ip, networks):
            raise UnsafeURLError(f"Host {host} resolve para {reason}")
    # Preferência determinística: IPv4 primeiro, depois IPv6.
    pinned = sorted(addresses, key=lambda a: (":" in a, a))[0]
    return ValidatedTarget(url, scheme, host, port, tuple(addresses), pinned, False)


async def validate_url(url: str, resolver: Resolver | None = None, trusted_hosts: Iterable[str] = (),
                       allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS) -> str:
    """Compatibilidade: valida e retorna o hostname (sem pin)."""
    target = await resolve_and_validate(url, resolver=resolver, trusted_hosts=trusted_hosts,
                                        allowed_ports=allowed_ports)
    return target.host
