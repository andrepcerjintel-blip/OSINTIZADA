import pytest

from osintizada.core.enums import IdentifierType as T
from osintizada.core.normalization import normalize


def test_brazilian_mobile_variants():
    n = normalize("(61) 99999-9999", T.PHONE)
    assert n.value == "+5561999999999"
    for v in ["61999999999", "+5561999999999", "5561999999999", "61 99999-9999"]:
        assert v in n.variants
    assert n.metadata["ddd"] == "61"
    assert n.metadata["is_mobile"] is True
    assert n.original == "(61) 99999-9999"


def test_phone_with_country_code_55():
    assert normalize("+55 11 91234-5678", T.PHONE).value == "+5511912345678"
    assert normalize("5511912345678", T.PHONE).value == "+5511912345678"


def test_international_phone():
    n = normalize("+44 20 7946 0958", T.PHONE)
    assert n.value == "+442079460958"


def test_email_lowercase_and_gmail_canonical():
    n = normalize("Fear.Less+tag@GMAIL.com", T.EMAIL)
    assert n.value == "fear.less+tag@gmail.com"
    assert n.metadata["provider_canonical"] == "fearless@gmail.com"
    assert n.metadata["domain"] == "gmail.com"


def test_cpf_keeps_original_digits_and_formatted():
    n = normalize("529.982.247-25", T.CPF)
    assert n.original == "529.982.247-25"
    assert n.value == "52998224725"
    assert n.metadata["formatted"] == "529.982.247-25"
    assert n.metadata["checksum_valid"] is True


def test_cnpj_root():
    n = normalize("11222333000181", T.CNPJ)
    assert n.metadata["formatted"] == "11.222.333/0001-81"
    assert n.metadata["cnpj_root"] == "11222333"
    assert n.metadata["is_headquarters"] is True


@pytest.mark.parametrize("raw", ["HTTPS://Example.COM/", "example.com.", "http://EXAMPLE.com/path?x=1"])
def test_domain_normalization(raw):
    assert normalize(raw, T.DOMAIN).value == "example.com"


def test_idn_domain_has_punycode_variant():
    n = normalize("exemplo-ção.com.br", T.DOMAIN)
    assert n.metadata["punycode"].startswith("xn--")


def test_url_preserves_original_and_canonicalizes():
    raw = "HTTPS://www.Example.com/Path/?utm_source=x&b=2&a=1#frag"
    n = normalize(raw, T.URL)
    assert n.metadata["original_url"] == raw.strip()
    assert n.value == "https://example.com/Path?a=1&b=2"


def test_username_preserves_case_and_creates_search_variants():
    n = normalize("@Dark_Wolf88", T.USERNAME)
    assert n.value == "Dark_Wolf88"
    assert "darkwolf88" not in n.variants  # compactação preserva case
    assert "DarkWolf88" in n.variants
    assert "@Dark_Wolf88" in n.variants
    assert n.metadata["mutable"] is True


def test_telegram_username_case_insensitive():
    assert normalize("@Durov", T.TELEGRAM_USERNAME).value == "durov"


def test_telegram_channel_id():
    n = normalize("-1001234567890", T.TELEGRAM_ID)
    assert n.metadata["peer_kind"] == "channel_or_supergroup"
    assert n.metadata["bare_id"] == "1234567890"


def test_ip_and_asn():
    assert normalize("2001:4860:4860:0:0:0:0:8888", T.IPV6).value == "2001:4860:4860::8888"
    assert normalize("10.0.0.5/8", T.CIDR).value == "10.0.0.0/8"
    asn = normalize("asn 15169", T.ASN)
    assert asn.value == "AS15169" and "15169" in asn.variants


def test_evm_lowercase():
    n = normalize("0x52908400098527886E0F7030069857D2E4169EE7", T.EVM_ADDRESS)
    assert n.value == "0x52908400098527886e0f7030069857d2e4169ee7"


def test_name_accentless_variant():
    n = normalize("  João   da  Silva ", T.FULL_NAME)
    assert n.value == "João da Silva"
    assert "Joao da Silva" in n.variants
    assert n.metadata["first_last"] == "João Silva"


def test_invalid_ip_raises():
    with pytest.raises(ValueError):
        normalize("999.1.1.1", T.IPV4)
