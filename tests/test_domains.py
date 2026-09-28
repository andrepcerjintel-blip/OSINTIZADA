import pytest

from osintizada.core.domains import DomainParser, get_domain_parser, parse_domain
from osintizada.core.enums import IdentifierType as T
from osintizada.core.identifiers import IdentifierEngine
from osintizada.core.validators import registrable_domain


@pytest.mark.parametrize(
    "host,subdomain,registrable,suffix",
    [
        ("example.com.br", None, "example.com.br", "com.br"),
        ("www.example.com.br", "www", "example.com.br", "com.br"),
        ("example.co.uk", None, "example.co.uk", "co.uk"),
        ("a.b.example.co.uk", "a.b", "example.co.uk", "co.uk"),
        ("example.com.au", None, "example.com.au", "com.au"),
        ("mail.example.com", "mail", "example.com", "com"),
        ("portal.receita.fazenda.gov.br", "portal.receita", "fazenda.gov.br", "gov.br"),
        ("usuario.github.io", None, "usuario.github.io", "github.io"),  # seção PRIVATE da PSL
        ("WWW.Example.COM.", "www", "example.com", "com"),
        ("exemplo-ção.com.br", None, "exemplo-ção.com.br", "com.br"),
    ],
)
def test_parse_domain(host, subdomain, registrable, suffix):
    parts = parse_domain(host)
    assert parts["subdomain"] == subdomain
    assert parts["registrable_domain"] == registrable
    assert parts["suffix"] == suffix
    assert parts["is_known_suffix"] is True


def test_public_suffix_alone_has_no_registrable_domain():
    parts = get_domain_parser().parse_domain("com.br")
    assert parts.is_public_suffix and parts.registrable_domain is None


def test_unknown_tld_is_flagged():
    parts = get_domain_parser().parse_domain("john.doe")
    assert parts.is_known_suffix is False


def test_ip_is_not_a_domain():
    parts = get_domain_parser().parse_domain("8.8.8.8")
    assert parts.registrable_domain is None and parts.suffix is None


def test_custom_psl_file(tmp_path):
    psl = tmp_path / "psl.dat"
    psl.write_text("// teste\ncom\nexemplo.com\n")
    parser = DomainParser(str(psl))
    assert parser.parse_domain("a.cliente.exemplo.com").registrable_domain == "cliente.exemplo.com"
    assert parser.source == str(psl)


def test_legacy_registrable_domain_function_uses_psl():
    assert registrable_domain("x.y.example.com.au") == "example.com.au"
    assert registrable_domain("com.br") == "com.br"


def test_identifier_engine_uses_psl():
    engine = IdentifierEngine()
    assert engine.detect("example.co.uk").primary.type == T.DOMAIN
    assert engine.detect("shop.example.co.uk").primary.type == T.SUBDOMAIN
    assert T.DOMAIN not in [c.type for c in engine.detect("com.br").candidates]
    assert T.USERNAME in [c.type for c in engine.detect("john.doe").candidates]
