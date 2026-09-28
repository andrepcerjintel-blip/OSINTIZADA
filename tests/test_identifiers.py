import pytest

from osintizada.core.enums import IdentifierType as T
from osintizada.core.identifiers import IdentifierEngine

engine = IdentifierEngine()


def types(value: str) -> list[T]:
    return [c.type for c in engine.detect(value).candidates]


def primary(value: str) -> T:
    return engine.detect(value).primary.type


@pytest.mark.parametrize(
    "value,expected",
    [
        ("user@example.com", T.EMAIL),
        ("8.8.8.8", T.IPV4),
        ("2001:4860:4860::8888", T.IPV6),
        ("10.0.0.0/8", T.CIDR),
        ("AS15169", T.ASN),
        ("asn 13335", T.ASN),
        ("example.com", T.DOMAIN),
        ("exemplo.com.br", T.DOMAIN),
        ("mail.exemplo.com.br", T.SUBDOMAIN),
        ("https://example.com/a?b=1", T.URL),
        ("0x52908400098527886E0F7030069857D2E4169EE7", T.EVM_ADDRESS),
        ("0x" + "ab" * 32, T.TX_HASH),
        ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", T.BTC_ADDRESS),
        ("bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq", T.BTC_ADDRESS),
        ("d41d8cd98f00b204e9800998ecf8427e", T.MD5),
        ("da39a3ee5e6b4b0d3255bfef95601890afd80709", T.SHA1),
        ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", T.SHA256),
        ("529.982.247-25", T.CPF),
        ("11.222.333/0001-81", T.CNPJ),
        ("11222333000181", T.CNPJ),
        ("+44 20 7946 0958", T.PHONE),
        ("(61) 99999-9999", T.PHONE),
        ("-1001234567890", T.TELEGRAM_ID),
        ("João da Silva", T.FULL_NAME),
        ("@fearless1999", T.USERNAME),
        ("fearless1999", T.USERNAME),
    ],
)
def test_primary_detection(value, expected):
    assert primary(value) == expected


def test_username_returns_multiple_hypotheses():
    result = engine.detect("darkwolf88")
    got = {c.type: c.confidence for c in result.candidates}
    assert got[T.USERNAME] == 0.96
    assert got[T.ALIAS] == 0.72
    assert got[T.POSSIBLE_NAME] == 0.11
    assert result.is_ambiguous
    assert all(c.reason for c in result.candidates)


def test_candidates_sorted_by_confidence():
    confs = [c.confidence for c in engine.detect("fearless1999").candidates]
    assert confs == sorted(confs, reverse=True)


def test_valid_cpf_digits_is_ambiguous_with_phone():
    # 11 dígitos com DV de CPF válido e formato de celular: ambas hipóteses.
    detected = types("52998224725")
    assert T.CPF in detected


def test_invalid_cpf_formatted_flagged():
    cand = engine.detect("123.456.789-00").primary
    assert cand.type == T.CPF
    assert cand.metadata["checksum_valid"] is False
    assert cand.confidence < 0.9


def test_invalid_btc_checksum_not_detected_as_btc():
    assert T.BTC_ADDRESS not in types("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb")


def test_telegram_url_yields_handle():
    result = engine.detect("https://t.me/durov")
    tg = next(c for c in result.candidates if c.type == T.TELEGRAM_USERNAME)
    assert tg.value == "durov"
    assert T.TELEGRAM_LINK in types("https://t.me/durov")


def test_github_url_yields_username():
    result = engine.detect("https://github.com/torvalds/linux")
    gh = next(c for c in result.candidates if c.type == T.GITHUB_USERNAME)
    assert gh.value == "torvalds"


def test_reserved_social_paths_are_ignored():
    assert T.INSTAGRAM_USERNAME not in types("https://instagram.com/explore/")


def test_at_handle_platform_hypotheses():
    detected = types("@fearless1999")
    assert T.TELEGRAM_USERNAME in detected
    assert T.TWITTER_USERNAME in detected


def test_unusual_tld_keeps_username_hypothesis():
    detected = types("john.doe")
    assert T.USERNAME in detected


def test_onion_detection():
    onion = "a" * 56 + ".onion"
    assert primary(onion) == T.ONION


def test_empty_input_has_no_candidates():
    assert engine.detect("   ").candidates == []


def test_free_text_falls_back_to_keyword():
    assert primary("quem é o dono disso?") == T.KEYWORD


def test_analyze_normalizes_candidates():
    items = engine.analyze("USER@Example.COM")
    assert items[0].type == T.EMAIL
    assert items[0].value == "user@example.com"
    assert items[0].original == "USER@Example.COM"


def test_single_accented_word_is_possible_name():
    assert primary("João") == T.POSSIBLE_NAME
