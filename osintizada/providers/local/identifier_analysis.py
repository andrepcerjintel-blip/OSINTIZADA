"""Provider local de derivação estrutural.

Extrai entidades que decorrem deterministicamente da estrutura do próprio
identificador (ex.: domínio de um email, handle de uma URL de perfil).
Nenhuma rede é usada e todo resultado é classificado como DERIVED.
"""

from __future__ import annotations

from osintizada.core.enums import DataClassification, EntityType, IdentifierType, RelationType
from osintizada.core.models import NormalizedIdentifier, ProviderResult
from osintizada.core.normalization import normalize_host
from osintizada.core.urls import parse_social_url
from osintizada.core.validators import registrable_domain
from osintizada.providers.base import LocalProvider, register_provider

_SOCIAL_ENTITY = {
    IdentifierType.TELEGRAM_USERNAME: EntityType.TELEGRAM_USER,
}

# Provedores de email gratuitos: o domínio não indica organização.
FREE_MAIL_DOMAINS = frozenset(
    {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "yahoo.com.br",
     "icloud.com", "me.com", "proton.me", "protonmail.com", "gmx.com", "aol.com", "uol.com.br", "bol.com.br",
     "terra.com.br", "ig.com.br", "yandex.ru", "mail.ru", "tutanota.com", "zoho.com"}
)


@register_provider
class IdentifierAnalysisProvider(LocalProvider):
    name = "local.identifier_analysis"
    display_name = "Derivação estrutural local"
    description = "Deriva entidades diretamente da estrutura do identificador (sem rede)."
    supported_identifiers = frozenset(
        {IdentifierType.EMAIL, IdentifierType.URL, IdentifierType.TELEGRAM_LINK,
         IdentifierType.SUBDOMAIN, IdentifierType.HOSTNAME}
    )
    default_timeout_seconds = 5.0

    async def _search(self, identifier: NormalizedIdentifier, query: str | None) -> list[ProviderResult]:
        if identifier.type == IdentifierType.EMAIL:
            return self._from_email(identifier)
        if identifier.type in (IdentifierType.URL, IdentifierType.TELEGRAM_LINK):
            return self._from_url(identifier)
        return self._from_host(identifier.value, identifier.original)

    def _derived(self, etype: EntityType, value: str, source: str, rule: str, relation: RelationType,
                 reason: str, confidence: float = 1.0, **extra) -> ProviderResult:
        return ProviderResult(
            type=etype,
            value=value,
            source_name="OSINTIZADA (derivação local)",
            confidence=confidence,
            classification=DataClassification.DERIVED,
            raw={"derived_from": source, "rule": rule, **extra},
            relation_to_query=relation,
            relation_reason=reason,
        )

    def _from_email(self, ident: NormalizedIdentifier) -> list[ProviderResult]:
        domain = ident.metadata.get("domain") or ident.value.split("@", 1)[1]
        local = ident.metadata.get("local_part") or ident.value.split("@", 1)[0]
        free = domain in FREE_MAIL_DOMAINS
        results = [
            self._derived(EntityType.DOMAIN, domain, ident.value, "email_domain", RelationType.HOSTED_ON,
                          "Domínio do endereço de email", free_mail_provider=free),
            # Hipótese: local-part frequentemente é reutilizado como username.
            self._derived(EntityType.USERNAME, local.split("+", 1)[0], ident.value, "email_local_part",
                          RelationType.POSSIBLY_SAME_AS,
                          "Local-part do email pode ser reutilizado como username (hipótese, não confirmação)",
                          confidence=0.4),
        ]
        return results

    def _from_url(self, ident: NormalizedIdentifier) -> list[ProviderResult]:
        original = ident.metadata.get("original_url", ident.original)
        host = normalize_host(original)
        results = self._from_host(host, original)
        social = parse_social_url(original)
        if social is not None:
            etype = _SOCIAL_ENTITY.get(social.identifier_type, EntityType.SOCIAL_ACCOUNT)
            value = social.handle.lower() if social.platform != "youtube" else social.handle
            results.append(
                self._derived(etype, f"{social.platform}:{value}", original, "social_profile_url",
                              RelationType.LINKS_TO, f"URL aponta para perfil {social.platform}",
                              platform=social.platform, handle=social.handle)
            )
        return results

    def _from_host(self, host: str, source: str) -> list[ProviderResult]:
        if not host or host.endswith(".onion"):
            return []
        results: list[ProviderResult] = []
        root = registrable_domain(host)
        bare = host[4:] if host.startswith("www.") else host
        if bare != root:
            results.append(self._derived(EntityType.SUBDOMAIN, bare, source, "hostname", RelationType.PART_OF,
                                         "Hostname extraído do identificador"))
        results.append(self._derived(EntityType.DOMAIN, root, source, "registrable_domain", RelationType.PART_OF,
                                     "Domínio registrável (eTLD+1 aproximado)"))
        return results
