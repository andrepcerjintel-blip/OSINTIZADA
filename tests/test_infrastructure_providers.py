from datetime import datetime, timedelta, timezone

import httpx
import pytest

from osintizada.core.enums import EntityType, IdentifierType, ProviderStatus, RelationType, SourceType
from osintizada.core.normalization import normalize
from osintizada.providers.archive.wayback import WaybackProvider
from osintizada.providers.infrastructure.crtsh import CertificateTransparencyProvider, normalize_ct_name
from osintizada.providers.infrastructure.dns import DNSProvider, NXDomain
from osintizada.providers.infrastructure.rdap import RDAPProvider
from osintizada.resilience import ProviderRuntime
from tests.fixtures.rdap import AUTNUM_15169, DOMAIN_EXAMPLE, IP_8_8_8_8

D = lambda v: normalize(v, IdentifierType.DOMAIN)  # noqa: E731


async def _no_sleep(_):
    return None


async def public_resolver(host):
    return ["93.184.216.34"]


def http_runtime(handler) -> ProviderRuntime:
    from tests.fixtures.environment import FakeClock

    clock = FakeClock()
    return ProviderRuntime(transport=httpx.MockTransport(handler), resolver=public_resolver, clock=clock,
                           sleep=clock.sleep)


def by(results, etype):
    return {r.value: r for r in results if r.type == etype}


# --- DNS ------------------------------------------------------------------------------


class FakeDNS:
    description = "fake"

    def __init__(self, records, nxdomain=()):
        self.records = records
        self.nxdomain = set(nxdomain)
        self.queries = []

    async def query(self, name, rtype):
        self.queries.append((name, rtype))
        if name in self.nxdomain:
            raise NXDomain(name)
        return [(v, 300) for v in self.records.get((name, rtype), [])]


def dns_provider(settings, fake):
    rt = ProviderRuntime(sleep=_no_sleep)
    rt.dns_resolver = fake
    return DNSProvider(settings, rt)


async def test_dns_domain_records(settings):
    fake = FakeDNS({
        ("example.com", "A"): ["93.184.216.34"],
        ("example.com", "AAAA"): ["2606:2800:220:1:248:1893:25c8:1946"],
        ("example.com", "MX"): ["10 mail.example.com.", "20 alt.mx.provider.net."],
        ("example.com", "NS"): ["a.iana-servers.net.", "b.iana-servers.net."],
        ("example.com", "TXT"): ['"v=spf1 include:_spf.google.com ip4:192.0.2.0/24 -all"', '"google-site-verification=abc"'],
        ("example.com", "CNAME"): [],
    })
    resp = await dns_provider(settings, fake).search(D("example.com"))
    assert resp.status == ProviderStatus.SUCCESS
    ips = by(resp.results, EntityType.IP)
    assert ips["93.184.216.34"].relation_to_query == RelationType.RESOLVES_TO
    assert "2606:2800:220:1:248:1893:25c8:1946" in ips
    subs = by(resp.results, EntityType.SUBDOMAIN)
    assert subs["mail.example.com"].relation_to_query == RelationType.USES
    assert subs["mail.example.com"].attributes["role"] == "mail_server"
    assert subs["a.iana-servers.net"].attributes["role"] == "nameserver"
    assert subs["_spf.google.com"].relation_to_query == RelationType.ASSOCIATED_WITH
    assert "192.0.2.0/24" in by(resp.results, EntityType.NETWORK)
    own = by(resp.results, EntityType.DOMAIN)["example.com"]
    assert own.relation_to_query is None and "google-site-verification=abc" in own.attributes["txt_records"]
    assert resp.results[0].raw["record_type"] == "A" and resp.results[0].raw["ttl"] == 300


async def test_dns_null_mx_and_nxdomain(settings):
    fake = FakeDNS({("example.org", "MX"): ["0 ."]}, nxdomain=["nope.example"])
    resp = await dns_provider(settings, fake).search(D("example.org"))
    assert by(resp.results, EntityType.DOMAIN)["example.org"].attributes["null_mx"] is True
    nx = await dns_provider(settings, fake).search(D("nope.example"))
    assert nx.status == ProviderStatus.NO_RESULTS  # nome inexistente ≠ falha


async def test_dns_ip_ptr_and_cymru_asn(settings):
    fake = FakeDNS({
        ("8.8.8.8.in-addr.arpa.", "PTR"): ["dns.google."],
        ("8.8.8.8.origin.asn.cymru.com", "TXT"): ['"15169 | 8.8.8.0/24 | US | arin | 2023-12-28"'],
    })
    resp = await dns_provider(settings, fake).search(normalize("8.8.8.8", IdentifierType.IPV4))
    assert by(resp.results, EntityType.DOMAIN)["dns.google"].relation_to_query == RelationType.RESOLVES_TO
    net = by(resp.results, EntityType.NETWORK)["8.8.8.0/24"]
    assert net.attributes == {"country": "US", "registry": "arin", "allocated": "2023-12-28"}
    asns = [r for r in resp.results if r.type == EntityType.ASN]
    assert {r.relation_to_query for r in asns} == {RelationType.PART_OF, RelationType.HOSTED_ON}
    assert next(r for r in asns if r.source_entity).source_entity.value == "8.8.8.0/24"


async def test_dns_private_ip_skips_cymru(settings):
    fake = FakeDNS({})
    await dns_provider(settings, fake).search(normalize("10.0.0.1", IdentifierType.IPV4))
    assert not any("cymru" in q[0] for q in fake.queries)


async def test_dns_asn_description(settings):
    fake = FakeDNS({("AS15169.asn.cymru.com", "TXT"): ['"15169 | US | arin | 2000-03-30 | GOOGLE - Google LLC, US"']})
    resp = await dns_provider(settings, fake).search(normalize("AS15169", IdentifierType.ASN))
    [asn] = resp.results
    assert asn.value == "AS15169" and asn.attributes["as_name"] == "GOOGLE - Google LLC, US"
    assert asn.relation_to_query is None


async def test_dns_total_failure_is_failed(settings):
    class Broken:
        async def query(self, name, rtype):
            raise RuntimeError("SERVFAIL")

    rt = ProviderRuntime(sleep=_no_sleep)
    rt.dns_resolver = Broken()
    resp = await DNSProvider(settings, rt).search(D("example.com"))
    assert resp.status == ProviderStatus.FAILED and resp.error_code == "DNS_ERROR"


# --- RDAP -----------------------------------------------------------------------------


def rdap_provider(settings, payloads, requests=None):
    def handler(request):
        if requests is not None:
            requests.append(request)
        path = request.url.path
        if request.url.host == "rdap.org":  # bootstrap redireciona ao servidor autoritativo
            return httpx.Response(302, headers={"location": f"https://rdap.arin.net/registry{path}"})
        for suffix, (code, body) in payloads.items():
            if path.endswith(suffix):
                return httpx.Response(code, json=body)
        return httpx.Response(404, json={})

    return RDAPProvider(settings, http_runtime(handler))


async def test_rdap_ip_network(settings):
    reqs = []
    resp = await rdap_provider(settings, {"/ip/8.8.8.8": (200, IP_8_8_8_8)}, reqs).search(
        normalize("8.8.8.8", IdentifierType.IPV4))
    assert resp.status == ProviderStatus.SUCCESS
    assert reqs[0].headers["accept"] == "application/rdap+json"
    net = by(resp.results, EntityType.NETWORK)["8.8.8.0/24"]
    assert net.relation_to_query == RelationType.PART_OF
    assert net.source_url == "https://rdap.arin.net/registry/ip/8.8.8.8"  # URL final após redirect
    assert net.attributes["network_name"] == "GOGL" and net.attributes["registration"].startswith("2023")
    org = by(resp.results, EntityType.ORGANIZATION)["Google LLC"]
    assert org.relation_to_query == RelationType.REGISTERED_TO
    assert org.source_entity.type == EntityType.NETWORK and org.source_entity.value == "8.8.8.0/24"
    abuse = by(resp.results, EntityType.EMAIL)["network-abuse@google.com"]
    assert abuse.attributes["role"] == "abuse"
    assert by(resp.results, EntityType.ASN)["AS15169"].relation_to_query == RelationType.PART_OF


async def test_rdap_autnum(settings):
    resp = await rdap_provider(settings, {"/autnum/15169": (200, AUTNUM_15169)}).search(
        normalize("AS15169", IdentifierType.ASN))
    asn = by(resp.results, EntityType.ASN)["AS15169"]
    assert asn.relation_to_query is None and asn.attributes["as_name"] == "GOOGLE"
    org = by(resp.results, EntityType.ORGANIZATION)["Google LLC"]
    assert org.source_entity.value == "AS15169" and org.relation_to_query == RelationType.REGISTERED_TO


async def test_rdap_domain_redacted_registrant_is_not_invented(settings):
    resp = await rdap_provider(settings, {"/domain/example.com": (200, DOMAIN_EXAMPLE)}).search(D("example.com"))
    ns = by(resp.results, EntityType.SUBDOMAIN)
    assert ns["a.iana-servers.net"].relation_to_query == RelationType.USES
    orgs = by(resp.results, EntityType.ORGANIZATION)
    assert "RESERVED-Internet Assigned Numbers Authority" in orgs  # registrar
    assert orgs["RESERVED-Internet Assigned Numbers Authority"].relation_to_query == RelationType.ASSOCIATED_WITH
    assert not any("REDACTED" in v for v in orgs)  # nada inventado a partir de dado redigido
    assert not by(resp.results, EntityType.EMAIL)
    own = by(resp.results, EntityType.DOMAIN)["example.com"]
    assert own.attributes["dnssec"] is True and own.attributes["expiration"].startswith("2026")


async def test_rdap_404_is_no_results(settings):
    resp = await rdap_provider(settings, {}).search(D("naoexiste-xyz.com"))
    assert resp.status == ProviderStatus.NO_RESULTS


async def test_rdap_429(settings):
    resp = await rdap_provider(settings, {"/ip/1.1.1.1": (429, {})}).search(normalize("1.1.1.1", IdentifierType.IPV4))
    assert resp.status == ProviderStatus.RATE_LIMITED


async def test_rdap_redirect_to_private_host_is_blocked(settings):
    def handler(request):
        return httpx.Response(302, headers={"location": "http://10.0.0.1/ip/8.8.8.8"})

    resp = await RDAPProvider(settings, http_runtime(handler)).search(normalize("8.8.8.8", IdentifierType.IPV4))
    assert resp.status == ProviderStatus.FAILED and resp.error_code == "SSRF_BLOCKED"


# --- Certificate Transparency ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,host,wildcard",
    [("*.Example.com", "example.com", True), ("api.example.com.", "api.example.com", False),
     ("*.*.example.com", None, True), ("foo bar.example.com", None, False)],
)
def test_normalize_ct_name(raw, host, wildcard):
    assert normalize_ct_name(raw) == (host, wildcard)


def ct_rows():
    now = datetime.now(timezone.utc)
    fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S")  # noqa: E731
    return [
        {"id": 1, "issuer_name": "C=US, O=Let's Encrypt, CN=R3", "common_name": "example.com",
         "name_value": "example.com\n*.example.com\nwww.example.com",
         "not_before": fmt(now - timedelta(days=30)), "not_after": fmt(now + timedelta(days=60))},
        {"id": 2, "issuer_name": "C=US, O=Let's Encrypt, CN=R3", "common_name": "api.example.com",
         "name_value": "api.example.com\nWWW.example.com\nexample.org\nadmin@example.com",
         "not_before": fmt(now - timedelta(days=10)), "not_after": fmt(now + timedelta(days=80))},
        {"id": 3, "issuer_name": "CN=Old CA", "common_name": "legacy.example.com", "name_value": "legacy.example.com",
         "not_before": "2015-01-01T00:00:00", "not_after": "2016-01-01T00:00:00"},
    ]


async def test_crtsh_subdomains(settings):
    seen = {}

    def handler(request):
        seen["q"] = request.url.params["q"]
        return httpx.Response(200, json=ct_rows())

    resp = await CertificateTransparencyProvider(settings, http_runtime(handler)).search(D("example.com"))
    assert seen["q"] == "%.example.com"
    subs = by(resp.results, EntityType.SUBDOMAIN)
    assert set(subs) == {"www.example.com", "api.example.com", "legacy.example.com"}  # sem duplicatas/externos
    www = subs["www.example.com"]
    assert www.relation_to_query == RelationType.PART_OF and www.relation_direction == "reverse"
    assert www.attributes["certificate_count"] == 2 and www.raw["certificate_ids"] == [1, 2]
    assert subs["legacy.example.com"].is_historical is True
    assert subs["api.example.com"].is_historical is False
    assert by(resp.results, EntityType.DOMAIN)["example.com"].attributes["wildcard_certificate"] is True
    assert "admin@example.com" in by(resp.results, EntityType.EMAIL)
    assert "example.org" not in {r.value for r in resp.results}


async def test_crtsh_empty(settings):
    resp = await CertificateTransparencyProvider(
        settings, http_runtime(lambda r: httpx.Response(200, json=[]))).search(D("example.com"))
    assert resp.status == ProviderStatus.NO_RESULTS


# --- Wayback ------------------------------------------------------------------------------


CDX = [
    ["timestamp", "original", "statuscode", "mimetype", "digest"],
    ["20020120142510", "http://example.com:80/", "200", "text/html", "AAA"],
    ["20150302101010", "http://old.example.com/contato.html", "200", "text/html", "BBB"],
    ["20180101000000", "https://old.example.com/sobre", "200", "text/html", "CCC"],
]


async def test_wayback_domain_is_historical(settings):
    params = {}

    def handler(request):
        params.update(request.url.params)
        return httpx.Response(200, json=CDX)

    resp = await WaybackProvider(settings, http_runtime(handler)).search(D("example.com"))
    assert params["matchType"] == "domain" and params["output"] == "json"
    assert all(r.is_historical and r.source_type == SourceType.ARCHIVE for r in resp.results)
    urls = by(resp.results, EntityType.URL)
    page = urls["https://old.example.com/contato.html"]
    assert page.source_url == "https://web.archive.org/web/20150302101010/http://old.example.com/contato.html"
    assert page.observed_at.year == 2015 and page.relation_direction == "reverse"
    old = by(resp.results, EntityType.SUBDOMAIN)["old.example.com"]
    assert old.attributes["archived_urls"] == 2 and "histórico" in old.relation_reason


async def test_wayback_url_snapshots(settings):
    rows = [CDX[0], ["20100101000000", "http://example.com/", "200", "text/html", "X"]]
    resp = await WaybackProvider(settings, http_runtime(lambda r: httpx.Response(200, json=rows))).search(
        normalize("http://example.com/", IdentifierType.URL))
    [snap] = resp.results
    assert snap.relation_to_query is None and snap.attributes["archived_at"].startswith("2010")


async def test_wayback_no_captures(settings):
    resp = await WaybackProvider(settings, http_runtime(lambda r: httpx.Response(200, json=[]))).search(
        D("example.com"))
    assert resp.status == ProviderStatus.NO_RESULTS
