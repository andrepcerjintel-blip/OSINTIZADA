# Providers

Toda fonte externa (ou processamento local) é um **provider** independente. O Core nunca contém lógica
específica de site.

## Tipos base

| Classe | `provider_type` | Tier padrão | Uso |
|---|---|---|---|
| `LocalProvider` | local | 1 | processamento sem rede (custo 0, dados `DERIVED`) |
| `APIProvider` | api | 1 | APIs oficiais/estruturadas |
| `HTTPProvider` | http | 3 | coleta HTTP pública (inclui search engines) |
| `BrowserProvider` | browser | 4 | navegador automatizado, quando permitido |
| `TorProvider` | tor | 5 | somente via worker dedicado; desabilitado se `tor.mode = TOR_DISABLED` |
| `PaidProvider` | paid | 2 | serviços contratados (`PAID_SOURCE`, exige credenciais) |

## Criando um provider

```python
# osintizada/providers/<categoria>/<nome>.py
from osintizada.core.enums import EntityType, IdentifierType, SourceTier
from osintizada.core.models import NormalizedIdentifier, ProviderResult
from osintizada.providers.base import APIProvider, RateLimitedError, register_provider


@register_provider
class ExampleProvider(APIProvider):
    name = "example.api"                      # único; usado na config
    display_name = "Example API"
    supported_identifiers = frozenset({IdentifierType.EMAIL, IdentifierType.USERNAME})
    required_secrets = ("EXAMPLE_API_KEY",)   # nomes de variáveis de ambiente
    source_tag = "EXAMPLE"
    tier = SourceTier.TIER_2

    async def _search(self, identifier: NormalizedIdentifier, query: str | None) -> list[ProviderResult]:
        key = self.secret("EXAMPLE_API_KEY")
        ...  # chamada HTTP com httpx.AsyncClient
        # 429 → raise RateLimitedError(retry_after=...)
        # 401/403 de credencial → raise AuthRequiredError(...)
        # outra falha → raise ProviderError(...) (ou deixe a exceção propagar)
        return [ProviderResult(type=EntityType.URL, value=url, source_url=url, raw=payload, confidence=0.8)]
```

Depois, adicione o pacote da categoria em `_BUILTIN_PACKAGES` (`providers/base/registry.py`) se ainda não
estiver lá. Nada mais no sistema precisa mudar.

## Contrato

- `_search` retorna `list[ProviderResult]`. **Lista vazia = NO_RESULTS** (o provider respondeu e não achou nada).
- Se a coleta **não foi concluída**, levante exceção — nunca devolva lista vazia para esconder falha.
- `BaseProvider.search()` aplica automaticamente:

| Situação | Status |
|---|---|
| tipo de identificador não suportado / provider desabilitado | `SKIPPED` |
| secret ausente | `NOT_CONFIGURED` |
| tempo excedido | `TIMEOUT` |
| `RateLimitedError` | `RATE_LIMITED` (+ `metadata.retry_after`) |
| `AuthRequiredError` | `AUTH_REQUIRED` |
| qualquer outra exceção | `FAILED` (mensagem sanitizada) |
| resultados | `SUCCESS` / vazio → `NO_RESULTS` |

- Cada `ProviderResult` deve informar `source_url` quando existir, `raw` (dado original), `observed_at`
  (data do conteúdo, se conhecida), `is_historical` para dados de arquivo, `classification` e
  `confidence` — que é a confiança **do provider no dado**, não confiança de identidade.
- Nunca logar ou devolver secrets; mensagens de erro passam por `sanitize()`.

## Healthcheck

Implemente `_healthcheck()` com uma chamada leve (ex.: endpoint de quota). O `healthcheck()` público mede
latência, trata falhas e expõe credenciais apenas mascaradas (`sk-****37ad`).

## Providers existentes

| Nome | Tipo | Suporta | Descrição |
|---|---|---|---|
| `local.identifier_analysis` | local | email, url, telegram_link, subdomain, hostname | domínio de email, local-part como *hipótese* de username, domínio/subdomínio de URL, perfil social em URL |
