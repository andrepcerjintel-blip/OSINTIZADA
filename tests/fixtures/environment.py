"""Ambiente de investigação totalmente simulado (sem internet) para testes de integração.

Os providers são os REAIS de produção; apenas o transporte HTTP e o resolver DNS são falsos.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from osintizada.config import Settings
from osintizada.db import Database
from osintizada.investigation.service import InvestigationService
from osintizada.orchestration.source_orchestrator import SourceOrchestrator
from osintizada.providers.archive.wayback import WaybackProvider
from osintizada.providers.infrastructure.crtsh import CertificateTransparencyProvider
from osintizada.providers.infrastructure.dns import DNSProvider, NXDomain
from osintizada.providers.infrastructure.rdap import RDAPProvider
from osintizada.providers.local.identifier_analysis import IdentifierAnalysisProvider
from osintizada.providers.search.brave import BraveSearchProvider
from osintizada.providers.telegram.telethon_provider import TelegramProvider
from osintizada.resilience import ProviderRuntime
from tests.fixtures.rdap import DOMAIN_EXAMPLE

DNS_RECORDS = {
    ("example.com", "A"): ["93.184.216.34"],
    ("example.com", "NS"): ["a.iana-servers.net.", "b.iana-servers.net."],
    ("example.com", "MX"): ["0 ."],
    ("example.com", "TXT"): ['"v=spf1 -all"'],
    ("www.example.com", "A"): ["93.184.216.34"],  # mesmo IP: não pode duplicar entidade
    # PTR aponta de volta para o seed: não pode gerar loop.
    ("34.216.184.93.in-addr.arpa.", "PTR"): ["example.com."],
    ("34.216.184.93.origin.asn.cymru.com", "TXT"): ['"15133 | 93.184.216.0/24 | EU | ripencc | 2008-06-02"'],
    ("AS15133.asn.cymru.com", "TXT"): ['"15133 | US | arin | 2007-03-19 | EDGECAST - Verizon Business, US"'],
}

RDAP_IP = {
    "objectClassName": "ip network", "handle": "EDGECAST-NETBLK-03", "name": "EDGECAST-NETBLK-03",
    "startAddress": "93.184.216.0", "endAddress": "93.184.216.255", "country": "US",
    "cidr0_cidrs": [{"v4prefix": "93.184.216.0", "length": 24}],
    "entities": [{"objectClassName": "entity", "handle": "ORG-EC", "roles": ["registrant"],
                  "vcardArray": ["vcard", [["fn", {}, "text", "Edgecast Inc."], ["kind", {}, "text", "org"]]]}],
}
RDAP_AUTNUM = {
    "objectClassName": "autnum", "handle": "AS15133", "startAutnum": 15133, "name": "EDGECAST",
    "entities": [{"objectClassName": "entity", "roles": ["registrant"],
                  "vcardArray": ["vcard", [["fn", {}, "text", "Edgecast Inc."], ["kind", {}, "text", "org"]]]}],
}


class FakeDNS:
    description = "fake-dns"

    def __init__(self) -> None:
        self.queries: list[tuple[str, str]] = []

    async def query(self, name, rtype):
        self.queries.append((name, rtype))
        if name.endswith(".invalid"):
            raise NXDomain(name)
        return [(v, 300) for v in DNS_RECORDS.get((name, rtype), [])]


def _ct_rows():
    now = datetime.now(timezone.utc)
    fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S")  # noqa: E731
    return [{"id": 10, "issuer_name": "CN=DigiCert", "common_name": "www.example.com",
             "name_value": "www.example.com\nexample.com",
             "not_before": fmt(now - timedelta(days=5)), "not_after": fmt(now + timedelta(days=300))}]


def http_handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if host == "rdap.org":
        return httpx.Response(302, headers={"location": f"https://rdap.test-rir.net{path}"})
    if host == "rdap.test-rir.net":
        if path == "/ip/93.184.216.34":
            return httpx.Response(200, json=RDAP_IP)
        if path == "/autnum/15133":
            return httpx.Response(200, json=RDAP_AUTNUM)
        if path == "/domain/example.com":
            return httpx.Response(200, json=DOMAIN_EXAMPLE)
        return httpx.Response(404, json={})
    if host == "crt.sh":
        return httpx.Response(200, json=_ct_rows() if request.url.params["q"] == "%.example.com" else [])
    if host == "web.archive.org":
        return httpx.Response(200, json=[])
    return httpx.Response(599, text="host inesperado no teste")


class FakeClock:
    """Relógio simulado: esperas de rate limit/backoff avançam o tempo sem dormir."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


async def _public(host):
    return ["93.184.216.34"]


def build_service(settings: Settings | None = None, db_url: str = "sqlite://"):
    settings = settings or Settings()
    db = Database(db_url)
    db.create_all()
    clock = FakeClock()
    runtime = ProviderRuntime(transport=httpx.MockTransport(http_handler), resolver=_public, clock=clock,
                              sleep=clock.sleep)
    runtime.dns_resolver = FakeDNS()
    providers = [cls(settings, runtime) for cls in (
        DNSProvider, RDAPProvider, CertificateTransparencyProvider, WaybackProvider, IdentifierAnalysisProvider,
        BraveSearchProvider, TelegramProvider)]
    orchestrator = SourceOrchestrator(settings=settings, providers=providers, runtime=runtime)
    return InvestigationService(db, orchestrator, settings), db, runtime
