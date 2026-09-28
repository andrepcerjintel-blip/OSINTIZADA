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

    trusted_hosts = ("api.example.com",)      # endpoint fixo (dispensa DNS no check SSRF)
    default_requests_per_second = 2           # aplicado ANTES de cada requisição
    default_concurrency = 3                   # chamadas simultâneas deste provider
    default_cache_ttl_seconds = 3600
    parser_version = "1"                      # incremente ao mudar o parsing (invalida cache)

    async def _search(self, identifier: NormalizedIdentifier, query: str | None) -> list[ProviderResult]:
        async with self.http_client() as client:           # SafeHTTPClient (SSRF, timeout, limite)
            result = await self.fetch(                     # 429/401/5xx já mapeados
                client, "GET", "https://api.example.com/v1/lookup",
                params={"q": identifier.value},
                headers={"Authorization": f"Bearer {self.secret('EXAMPLE_API_KEY')}"},
            )
        payload = result.json()
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
| `SkippedError` | `SKIPPED` |
| `ProviderNotConfigured` | `NOT_CONFIGURED` |
| `ProviderTimeout` (timeout, HTTP 408) | re-tentado com backoff; esgotado → `TIMEOUT` |
| `ProviderUnavailable` (502/503/504, conexão) | re-tentado com backoff; esgotado → `FAILED` |
| HTTP 400/401/403/404/500 | **sem retry** (`404` com `not_found_ok=True` = `NO_RESULTS`) |
| HTTP 429 | 1 nova tentativa se `Retry-After` ≤ 15s; senão `RATE_LIMITED` + pausa do provider |
| circuit breaker aberto | `SKIPPED` (sem chamada) |
| pausa após 429 em andamento | `RATE_LIMITED` (sem chamada) |
| URL recusada pelo SSRF check | `FAILED` |
| qualquer outra exceção | `FAILED` (mensagem sanitizada) |
| resultados | `SUCCESS` / vazio → `NO_RESULTS` |

- Cada `ProviderResult` (alias `ProviderItem`) deve informar `source_url` quando existir, `raw` (dado
  original), `observed_at` (data do conteúdo, se conhecida), `is_historical` para dados de arquivo,
  `classification` e `confidence`. Essa confiança é a **do provider no dado**, não confiança de identidade.
- Relações: por padrão `entidade consultada --relation_to_query--> item`. Use `relation_direction="reverse"`
  para inverter (ex.: `SUBDOMAIN --PART_OF--> DOMAIN`) e `source_entity=EntityRef(...)` para ligar dois itens
  do mesmo resultado (ex.: `NETWORK --REGISTERED_TO--> ORGANIZATION`). Sempre com `relation_reason`.
- Um item com o mesmo tipo e valor da entidade consultada não cria relação. Ele só anexa evidência e
  `attributes` (ex.: nome do AS, TXT do domínio).
- `attributes` são observações da entidade (país, TTL, papel...). Valores divergentes entre fontes viram
  `CONFLICTING_EVIDENCE` e nunca são sobrescritos.
- `source_type` sobrepõe a natureza da fonte (`ARCHIVE`, `TOR`...).
- Todo código de erro fica em `ProviderResponse.error_code` (ex.: `HTTP_429`, `CONNECTION_ERROR`,
  `CIRCUIT_OPEN`, `SSRF_BLOCKED`, `BUDGET_EXHAUSTED`) e é persistido em `search_executions`.
- Nunca logar ou devolver secrets; mensagens de erro passam por `sanitize()`.

## Healthcheck

Implemente `_healthcheck()` com uma chamada leve (ex.: endpoint de quota). O `healthcheck()` público mede
latência, trata falhas e expõe credenciais apenas mascaradas (`sk-****37ad`).

Rate limit, cache, retry e circuit breaker são aplicados pelo `BaseProvider`; o provider não precisa
implementar nada disso. Para HTTP, use sempre `self.http_client()` + `self.fetch()`.

## Search engines

Subclasse `SearchEngineProvider` (`providers/search/base.py`) e implemente apenas `execute_query(client, query)`
devolvendo `SearchHit`s em ordem de ranking. A base cuida de:

- recusar (`SKIPPED`) consultas com operadores fora de `supported_operators` — a consulta nunca é alterada;
- gerar um resultado `URL` por página (`relation MENTIONED_IN`, confiança maior se o identificador aparece no snippet);
- rodar os Entity Extractors sobre título+snippet (co-ocorrência, `ASSOCIATED_WITH`, marcada como hipótese);
- consumir as consultas do QueryPlanner (`consumes_planned_queries = True`) e custar `web` no orçamento.

## Entity Extractors

`osintizada/extractors/` — extração determinística (regex + validação) usada por qualquer provider que colete texto:

| Extractor | Entidades | Salvaguardas contra falso positivo |
|---|---|---|
| `url` | URL | remove pontuação final, URL canônica, preserva original |
| `telegram` | TELEGRAM_USER, TELEGRAM_GROUP (convites) | caminhos reservados ignorados; `tg: @handle` só com contexto |
| `social_profile` | SOCIAL_ACCOUNT (`plataforma:handle`) | caminhos reservados (explore, search…) ignorados |
| `email` | EMAIL (inclusive `[at]`/`[dot]`) | ignora `logo@2x.png` |
| `crypto` | CRYPTO_ADDRESS, CRYPTO_TRANSACTION | checksum Bitcoin validado |
| `cpf` / `cnpj` | CPF, CNPJ | DV validado; CPF sem máscara exige a palavra "CPF" antes |
| `ip` | IP, NETWORK | ignora "versão 1.2.3.4"; marca `is_global` |
| `asn` | ASN | apenas `AS`/`ASN` em maiúsculas |
| `domain` | DOMAIN, SUBDOMAIN | TLD conhecido; ignora hosts já dentro de URL/email |
| `phone` | PHONE (E.164) | DDD válido; exige formatação ou palavra-chave; não reaproveita dígitos de CPF/CNPJ/IP |
| `hash` | HASH (md5/sha1/sha256) | ignora sequências só numéricas; não reaproveita endereços 0x |
| `mention` | USERNAME (plataforma desconhecida) | confiança 0.5; ignora `@` de emails |

Nomes de pessoa e localizações exigem análise semântica e ficarão na camada de IA (sempre `DERIVED`).

## Providers existentes

| Nome | Tipo / tier | Suporta | Credenciais | Entrega |
|---|---|---|---|---|
| `infra.dns` | API / 1 | domain, subdomain, hostname, ipv4, ipv6, asn | — | A/AAAA/CNAME/MX/NS/TXT (SPF interpretado), PTR, IP→ASN e prefixo (Team Cymru via DNS), nome do AS |
| `infra.rdap` | API / 1 | ipv4, ipv6, cidr, asn, domain | — | bloco de rede, ASN de origem, organização registrante, registrar, nameservers delegados, contatos de abuso (dados redigidos são ignorados) |
| `infra.crtsh` | API / 2 | domain | — | subdomínios passivos via Certificate Transparency (wildcards normalizados, fora do domínio descartados), emails em certificados |
| `archive.wayback` | API / 5 | domain, subdomain, url | — | URLs e hosts históricos, capturas de páginas. **Sempre `HISTORICAL_DATA`** |
| `social.telegram` | API / 2 | telegram_username, telegram_id, telegram_link, username | `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION` (+ pacote `telethon`) | Telegram ID (estável) + username (mutável, `USES_USERNAME` datado), nome exibido, tipo de peer |
| `search.brave` | API (search) / 3 | todos (consultas textuais) | `BRAVE_SEARCH_API_KEY` | páginas + entidades co-ocorrentes; operadores: aspas, site, filetype, intitle, -, OR |
| `search.google_cse` | API (search) / 3 | todos (consultas textuais) | `GOOGLE_CSE_API_KEY`, `GOOGLE_CSE_CX` | idem; todos os operadores; acesso restrito pelo Google a contas existentes |
| `local.identifier_analysis` | local / 1 | email, url, telegram_link, subdomain, hostname | — | derivações estruturais (DERIVED) |

Seleção por tipo de identificador (sem executar providers irrelevantes):

| Identificador | Providers |
|---|---|
| DOMAIN | DNS, RDAP, Certificate Transparency, Wayback, Search |
| SUBDOMAIN | DNS, Wayback, Search |
| IP | DNS (PTR + ASN), RDAP, Search |
| ASN / CIDR | DNS (ASN), RDAP, Search |
| USERNAME / Telegram | Telegram (se configurado), Search |
| EMAIL | derivação local, Search |

Mecanismos não integrados e por quê: **Bing Web Search API** foi descontinuada pela Microsoft em 2025;
**DuckDuckGo** e **Yahoo** não oferecem API de resultados web e scraping violaria os termos de uso;
**Mojeek** (API paga) e **Yandex** (Search API paga via Yandex Cloud) são candidatos aos próximos providers.
