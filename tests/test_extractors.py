import pytest

from osintizada.core.enums import DataClassification, EntityType, RelationType
from osintizada.extractors import ExtractionPipeline, extractions_to_results

pipeline = ExtractionPipeline()


def found(text, **kw):
    return {(e.type, e.value) for e in pipeline.extract(text, **kw)}


def test_email_and_obfuscated_email():
    got = found("Mande para Fulano@Example.COM ou fulano [at] proton [dot] me.")
    assert (EntityType.EMAIL, "fulano@example.com") in got
    assert (EntityType.EMAIL, "fulano@proton.me") in got


def test_email_ignores_image_retina_names():
    assert not any(t == EntityType.EMAIL for t, _ in found("logo@2x.png e icon@3x.jpg"))


def test_email_domain_not_duplicated_as_domain():
    got = found("contato: joao@empresa.com.br")
    assert (EntityType.DOMAIN, "empresa.com.br") not in got


@pytest.mark.parametrize(
    "text,value",
    [
        ("Tel: (61) 99999-9999", "+5561999999999"),
        ("whatsapp 61 99999 9999", "+5561999999999"),
        ("+55 11 91234-5678", "+5511912345678"),
        ("fixo (11) 3456-7890", "+551134567890"),
        ("call +44 20 7946 0958", "+442079460958"),
    ],
)
def test_phones(text, value):
    assert (EntityType.PHONE, value) in found(text)


def test_raw_digits_without_context_are_not_phones():
    assert not any(t == EntityType.PHONE for t, _ in found("protocolo 61999999999"))


def test_cpf_digits_not_reused_as_phone():
    got = found("CPF 529.982.247-25")
    assert (EntityType.CPF, "52998224725") in got
    assert not any(t == EntityType.PHONE for t, _ in got)


def test_cpf_requires_valid_dv_and_context_when_unformatted():
    assert (EntityType.CPF, "52998224725") in found("CPF: 52998224725")
    assert not any(t == EntityType.CPF for t, _ in found("número 52998224725"))
    assert not any(t == EntityType.CPF for t, _ in found("CPF 529.982.247-26"))


def test_cnpj():
    assert (EntityType.CNPJ, "11222333000181") in found("CNPJ nº 11.222.333/0001-81")
    items = pipeline.extract("inscrição 11222333000181")
    assert items[0].type == EntityType.CNPJ and items[0].confidence < 0.9


def test_urls_trailing_punctuation_and_canonical():
    items = pipeline.extract("Veja (https://www.site.com.br/pagina?utm_source=x). Fim.")
    urls = [e for e in items if e.type == EntityType.URL]
    assert urls[0].value == "https://site.com.br/pagina"
    assert urls[0].metadata["original_url"] == "https://www.site.com.br/pagina?utm_source=x"


def test_domains_need_known_tld():
    got = found("arquivo relatorio.pdf, exemplo.com.br e api.exemplo.io")
    assert (EntityType.DOMAIN, "exemplo.com.br") in got
    assert (EntityType.SUBDOMAIN, "api.exemplo.io") in got
    assert not any(v == "relatorio.pdf" for _, v in got)


def test_ips_networks_and_version_numbers():
    got = found("servidor 8.8.8.8, bloco 10.0.0.0/8, v6 2001:4860:4860::8888, versão 1.2.3.4")
    assert (EntityType.IP, "8.8.8.8") in got
    assert (EntityType.NETWORK, "10.0.0.0/8") in got
    assert (EntityType.IP, "2001:4860:4860::8888") in got
    assert (EntityType.IP, "1.2.3.4") not in got


def test_ip_not_phone_or_domain():
    got = found("IP 200.160.2.3")
    assert got == {(EntityType.IP, "200.160.2.3")}


def test_asn_is_case_sensitive():
    assert (EntityType.ASN, "AS15169") in found("rota via AS15169")
    assert not any(t == EntityType.ASN for t, _ in found("as 2020 approaches"))


def test_crypto_and_hashes():
    got = found("BTC 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa inválido 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb "
                "ETH 0x52908400098527886E0F7030069857D2E4169EE7 md5 d41d8cd98f00b204e9800998ecf8427e")
    assert (EntityType.CRYPTO_ADDRESS, "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa") in got
    assert (EntityType.CRYPTO_ADDRESS, "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb") not in got
    assert (EntityType.CRYPTO_ADDRESS, "0x52908400098527886e0f7030069857d2e4169ee7") in got
    assert (EntityType.HASH, "d41d8cd98f00b204e9800998ecf8427e") in got
    # o endereço EVM não é reaproveitado como hash
    assert not any(t == EntityType.HASH and v.startswith("52908400") for t, v in got)


def test_telegram_links_invites_and_context_handles():
    got = found("canal https://t.me/s/Fearless_News/42, grupo t.me/joinchat/AAAAAEk8xZ3dQ0Pq e tg: @DarkFearless")
    assert (EntityType.TELEGRAM_USER, "fearless_news") in got
    assert (EntityType.TELEGRAM_GROUP, "invite:AAAAAEk8xZ3dQ0Pq") in got
    assert (EntityType.TELEGRAM_USER, "darkfearless") in got
    # handle com contexto Telegram não vira também menção genérica
    assert (EntityType.USERNAME, "DarkFearless") not in got


def test_social_profiles():
    got = found("https://github.com/fearless1999 https://www.instagram.com/some.user/ https://x.com/jack/status/1")
    assert (EntityType.SOCIAL_ACCOUNT, "github:fearless1999") in got
    assert (EntityType.SOCIAL_ACCOUNT, "instagram:some.user") in got
    assert (EntityType.SOCIAL_ACCOUNT, "twitter:jack") in got


def test_mentions_are_low_confidence_usernames():
    items = pipeline.extract("fala @fearless1999, email a@b.com")
    mentions = [e for e in items if e.type == EntityType.USERNAME]
    assert [m.value for m in mentions] == ["fearless1999"]
    assert mentions[0].confidence <= 0.5


def test_dedup_counts_occurrences_and_keeps_context():
    items = pipeline.extract("a@b.com ... repetindo a@b.com")
    email = next(e for e in items if e.type == EntityType.EMAIL)
    assert email.metadata["occurrences"] == 2
    assert "a@b.com" in email.context


def test_exclude_values():
    assert not found("contato fearless1999@gmail.com", exclude_values=["FEARLESS1999@gmail.com"])


def test_empty_text():
    assert pipeline.extract("") == []


def test_extractions_to_results_preserve_provenance():
    items = pipeline.extract("email: x@y.com")
    [res] = extractions_to_results(items, source_url="https://p.example/1", source_name="teste")
    assert res.source_url == "https://p.example/1"
    assert res.relation_to_query == RelationType.ASSOCIATED_WITH
    assert "hipótese" in res.relation_reason
    assert res.raw["context"] and res.raw["extractor"] == "email"
    assert res.confidence < items[0].confidence
    assert res.classification == DataClassification.PUBLIC
