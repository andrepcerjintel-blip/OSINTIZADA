from datetime import datetime, timezone

import httpx
import pytest

from osintizada.net import (
    ResponseTooLargeError,
    SafeHTTPClient,
    UnsafeURLError,
    parse_retry_after,
    validate_url,
)

PUBLIC = "93.184.216.34"


def resolver_for(mapping):
    async def resolve(host):
        return mapping.get(host, [PUBLIC])
    return resolve


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "http://user:pass@example.com/",
        "http://localhost/admin",
        "http://127.0.0.1:8080/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://service.internal/",
        "http:///nohost",
    ],
)
async def test_blocks_unsafe_urls(url):
    with pytest.raises(UnsafeURLError):
        await validate_url(url, resolver=resolver_for({}))


async def test_blocks_hostname_resolving_to_private():
    with pytest.raises(UnsafeURLError, match="resolve para"):
        await validate_url("http://evil.example/", resolver=resolver_for({"evil.example": [PUBLIC, "10.1.1.1"]}))


async def test_allows_public_and_trusted_hosts():
    assert await validate_url("https://example.com/x", resolver=resolver_for({})) == "example.com"

    async def boom(host):
        raise AssertionError("não deveria resolver host confiável")

    assert await validate_url("https://api.search.brave.com/x", resolver=boom,
                              trusted_hosts=["api.search.brave.com"]) == "api.search.brave.com"


async def test_unresolvable_host_is_rejected():
    async def fail(host):
        raise OSError("NXDOMAIN")

    with pytest.raises(UnsafeURLError, match="resolver"):
        await validate_url("https://nope.example/", resolver=fail)


async def test_redirect_to_private_is_blocked():
    def handler(request):
        if request.headers["host"] == "public.example":
            return httpx.Response(302, headers={"location": "http://internal.example/secret"})
        return httpx.Response(200, text="segredo")

    resolver = resolver_for({"internal.example": ["10.0.0.9"]})
    async with SafeHTTPClient(transport=httpx.MockTransport(handler), resolver=resolver) as client:
        with pytest.raises(UnsafeURLError):
            await client.get("http://public.example/")


async def test_redirect_followed_and_recorded():
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, text="ok")

    async with SafeHTTPClient(transport=httpx.MockTransport(handler), resolver=resolver_for({})) as client:
        result = await client.get("https://site.example/old")
    assert result.status_code == 200 and result.text == "ok"
    assert result.url.endswith("/new") and len(result.redirects) == 1


async def test_too_many_redirects():
    def handler(request):
        return httpx.Response(302, headers={"location": "/loop"})

    async with SafeHTTPClient(transport=httpx.MockTransport(handler), resolver=resolver_for({}),
                              max_redirects=2) as client:
        with pytest.raises(UnsafeURLError, match="redirecionamentos"):
            await client.get("https://site.example/")


async def test_response_size_limit():
    def handler(request):
        return httpx.Response(200, content=b"x" * 2048)

    async with SafeHTTPClient(transport=httpx.MockTransport(handler), resolver=resolver_for({}),
                              max_bytes=1024) as client:
        with pytest.raises(ResponseTooLargeError):
            await client.get("https://site.example/")


async def test_user_agent_sent():
    seen = {}

    def handler(request):
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, json={"a": 1})

    async with SafeHTTPClient(transport=httpx.MockTransport(handler), resolver=resolver_for({}),
                              user_agent="RINO/test") as client:
        result = await client.get("https://site.example/")
    assert seen["ua"] == "RINO/test" and result.json() == {"a": 1}


def test_parse_retry_after():
    assert parse_retry_after("120") == 120
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert parse_retry_after("Thu, 01 Jan 2026 12:00:30 GMT", now=now) == 30
