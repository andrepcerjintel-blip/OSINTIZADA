"""Suíte SSRF / DNS rebinding.

Os testes usam um transporte simulado que registra EXATAMENTE para onde a conexão foi
feita (URL com IP), o header Host e o SNI — provando que a conexão usa o IP validado.
"""

import httpx
import pytest

from osintizada.net import SafeHTTPClient, UnsafeURLError, ip_block_reason, resolve_and_validate

PUBLIC = "93.184.216.34"
PUBLIC_2 = "203.0.113.10"  # TEST-NET-3: não global → usado só onde queremos bloqueio


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, routes=None):
        self.requests: list[httpx.Request] = []
        self.routes = routes or {}

    async def handle_async_request(self, request):
        self.requests.append(request)
        route = self.routes.get((request.headers["host"], request.url.path))
        if route is not None:
            return route
        return httpx.Response(200, text="ok")


class SequenceResolver:
    """Resolver que muda a resposta a cada chamada (simula DNS rebinding)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[str] = []

    async def __call__(self, host):
        self.calls.append(host)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def client(transport, resolver, **kw):
    return SafeHTTPClient(transport=transport, resolver=resolver, **kw)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/", "http://127.1.2.3/", "http://localhost/", "http://LOCALHOST.localdomain/",
        "http://api.localhost/", "http://10.0.0.1/", "http://172.16.5.4/", "http://192.168.0.1/",
        "http://169.254.169.254/latest/meta-data/", "http://100.64.1.1/", "http://0.0.0.0/",
        "http://[::1]/", "http://[fe80::1]/", "http://[fc00::1]/", "http://[fd00:ec2::254]/",
        "http://[::ffff:127.0.0.1]/", "http://[::ffff:10.0.0.1]/", "http://[64:ff9b::7f00:1]/",
        "http://[2002:7f00:1::]/", "http://metadata.google.internal/", "http://printer.local/",
        "file:///etc/passwd", "ftp://example.com/", "gopher://example.com/", "dict://example.com:11211/",
        "data:text/html,<script>", "javascript:alert(1)", "http://user:pass@example.com/",
        "http://example.com:6379/", "https://example.com:8443/",
        "http://expyuzz4wqqyqhjn.onion/", "https://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion/",
    ],
)
async def test_blocked_urls(url):
    with pytest.raises(UnsafeURLError):
        await resolve_and_validate(url, resolver=SequenceResolver([PUBLIC]))


async def test_numeric_hostname_resolving_to_loopback_is_blocked():
    # "http://2130706433/" é resolvido pelo sistema para 127.0.0.1: o que vale é o IP resolvido.
    with pytest.raises(UnsafeURLError, match="não públic"):
        await resolve_and_validate("http://2130706433/", resolver=SequenceResolver(["127.0.0.1"]))


async def test_hostname_with_private_resolution_is_blocked():
    with pytest.raises(UnsafeURLError, match="resolve para"):
        await resolve_and_validate("http://intranet.example.com/", resolver=SequenceResolver(["10.1.2.3"]))


async def test_multi_answer_with_any_private_ip_is_blocked():
    with pytest.raises(UnsafeURLError, match="resolve para"):
        await resolve_and_validate("http://mixed.example.com/", resolver=SequenceResolver([PUBLIC, "127.0.0.1"]))


async def test_extra_blocked_networks():
    with pytest.raises(UnsafeURLError, match="rede interna configurada"):
        await resolve_and_validate("http://corp.example.com/", resolver=SequenceResolver([PUBLIC]),
                                   extra_networks=["93.184.216.0/24"])


def test_ip_block_reasons():
    assert "metadata" in ip_block_reason("169.254.169.254")
    assert "metadata" in ip_block_reason("fd00:ec2::254")
    assert ip_block_reason("8.8.8.8") is None and ip_block_reason("2606:4700:4700::1111") is None


async def test_connection_uses_the_validated_ip_with_original_host_and_sni():
    transport = RecordingTransport()
    resolver = SequenceResolver([PUBLIC])
    async with client(transport, resolver) as c:
        result = await c.get("https://site.example.com/path?q=1")
    [req] = transport.requests
    assert req.url.host == PUBLIC                      # conexão no IP validado
    assert req.headers["host"] == "site.example.com"   # Host original preservado
    assert req.extensions["sni_hostname"] == "site.example.com"  # TLS valida o certificado do hostname
    assert req.url.path == "/path" and req.url.params["q"] == "1"
    assert result.url == "https://site.example.com/path?q=1" and result.connected_ips == [PUBLIC]
    assert resolver.calls == ["site.example.com"]      # resolvido UMA vez: sem 2ª resolução do cliente HTTP


async def test_dns_rebinding_is_neutralized_by_pinning():
    """1ª resolução: IP público (validado). Um cliente ingênuo resolveria de novo e cairia em 127.0.0.1.
    Com pinning, a conexão vai para o IP validado; a PRÓXIMA requisição revalida e é bloqueada."""
    transport = RecordingTransport()
    resolver = SequenceResolver([PUBLIC], ["127.0.0.1"])
    async with client(transport, resolver) as c:
        await c.get("http://rebind.attacker.test/")
        assert transport.requests[0].url.host == PUBLIC
        assert all(r.url.host != "127.0.0.1" for r in transport.requests)
        with pytest.raises(UnsafeURLError):
            await c.get("http://rebind.attacker.test/")  # pin não é reutilizado: nova validação
    assert len(transport.requests) == 1


async def test_redirect_to_localhost_is_blocked():
    transport = RecordingTransport({
        ("public.example", "/"): httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})})
    async with client(transport, SequenceResolver([PUBLIC])) as c:
        with pytest.raises(UnsafeURLError):
            await c.get("http://public.example/")
    assert len(transport.requests) == 1  # o destino interno nunca foi contatado


async def test_redirect_to_hostname_resolving_private_is_blocked():
    transport = RecordingTransport({
        ("public.example", "/"): httpx.Response(301, headers={"location": "http://evil.example/x"})})
    resolver = SequenceResolver([PUBLIC], ["192.168.1.10"])
    async with client(transport, resolver) as c:
        with pytest.raises(UnsafeURLError):
            await c.get("http://public.example/")
    assert resolver.calls == ["public.example", "evil.example"]  # cada salto revalidado


async def test_redirect_each_hop_is_pinned():
    transport = RecordingTransport({
        ("a.example", "/"): httpx.Response(302, headers={"location": "https://b.example/final"})})
    resolver = SequenceResolver([PUBLIC], ["198.51.100.7"])  # 198.51.100.0/24 é TEST-NET (não global)
    async with client(transport, resolver) as c:
        with pytest.raises(UnsafeURLError):
            await c.get("http://a.example/")
    resolver2 = SequenceResolver([PUBLIC], ["8.8.4.4"])
    transport2 = RecordingTransport({
        ("a.example", "/"): httpx.Response(302, headers={"location": "https://b.example/final"})})
    async with client(transport2, resolver2) as c:
        result = await c.get("http://a.example/")
    assert [r.url.host for r in transport2.requests] == [PUBLIC, "8.8.4.4"]
    assert transport2.requests[1].headers["host"] == "b.example"
    assert result.connected_ips == [PUBLIC, "8.8.4.4"] and result.url == "https://b.example/final"


async def test_redirect_limit():
    transport = RecordingTransport({("loop.example", "/"): httpx.Response(302, headers={"location": "/"})})
    async with client(transport, SequenceResolver([PUBLIC]), max_redirects=3) as c:
        with pytest.raises(UnsafeURLError, match="redirecionamentos"):
            await c.get("http://loop.example/")
    assert len(transport.requests) == 4


async def test_trusted_host_is_not_pinned_but_redirect_away_is():
    transport = RecordingTransport({
        ("api.fixed.example", "/"): httpx.Response(302, headers={"location": "http://10.0.0.5/"})})
    async with client(transport, SequenceResolver([PUBLIC]), trusted_hosts=["api.fixed.example"]) as c:
        with pytest.raises(UnsafeURLError):
            await c.get("https://api.fixed.example/")
    assert transport.requests[0].url.host == "api.fixed.example"


async def test_custom_port_allowed_only_when_declared():
    async with client(RecordingTransport(), SequenceResolver([PUBLIC]), allowed_ports={80, 443, 8443}) as c:
        await c.get("https://svc.example:8443/")


async def test_https_through_env_proxy_refuses_untrusted_urls(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    monkeypatch.setenv("NO_PROXY", "")
    c = SafeHTTPClient(resolver=SequenceResolver([PUBLIC]))
    with pytest.raises(UnsafeURLError, match="proxy"):
        await c.get("https://site.example.com/")
    await c.aclose()


def test_socks5h_proxy_is_detected(monkeypatch):
    from osintizada.net.http_client import proxy_resolves_dns

    monkeypatch.setenv("ALL_PROXY", "socks5h://127.0.0.1:9050")
    assert proxy_resolves_dns()
    monkeypatch.setenv("ALL_PROXY", "http://proxy:3128")
    assert not proxy_resolves_dns()


async def test_deny_policy_refuses_arbitrary_urls_but_keeps_trusted():
    transport = RecordingTransport()
    c = SafeHTTPClient(transport=transport, resolver=SequenceResolver([PUBLIC]), proxy_policy="deny",
                       trusted_hosts=["api.fixed.example"])
    with pytest.raises(UnsafeURLError, match="resolve DNS remotamente"):
        await c.get("http://site.example.com/")
    await c.get("https://api.fixed.example/")
    assert [r.url.host for r in transport.requests] == ["api.fixed.example"]
    await c.aclose()
