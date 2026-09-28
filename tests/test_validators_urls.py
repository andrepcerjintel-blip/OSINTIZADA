from osintizada.core.urls import canonical_url, parse_social_url
from osintizada.core.validators import is_valid_cnpj, is_valid_cpf, registrable_domain


def test_cpf_cnpj_checksums():
    assert is_valid_cpf("529.982.247-25")
    assert not is_valid_cpf("111.111.111-11")
    assert not is_valid_cpf("529.982.247-26")
    assert is_valid_cnpj("11.222.333/0001-81")
    assert not is_valid_cnpj("11.222.333/0001-82")


def test_registrable_domain():
    assert registrable_domain("a.b.example.com") == "example.com"
    assert registrable_domain("www.receita.fazenda.gov.br") == "fazenda.gov.br"


def test_canonical_url_equivalence():
    a = canonical_url("http://www.Example.com:80/page/?utm_campaign=x&id=3#top")
    b = canonical_url("https://example.com/page?id=3")
    assert a == b


def test_canonical_url_keeps_non_default_port():
    assert canonical_url("https://example.com:8443/") == "https://example.com:8443/"


def test_parse_social_url():
    assert parse_social_url("https://x.com/jack/status/20").handle == "jack"
    assert parse_social_url("https://www.tiktok.com/@some.user").handle == "some.user"
    assert parse_social_url("https://reddit.com/u/spez").handle == "spez"
    assert parse_social_url("https://example.com/user") is None
