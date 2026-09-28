# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).

## [0.3.0] — 2026-09-28 — Fase 3: primeiro ciclo investigativo real

### Adicionado
- **Public Suffix List oficial** (`core/domains.py`, pacote `publicsuffixlist`, seções ICANN + PRIVATE):
  `DomainParser.parse_domain()` → hostname, subdomain, registrable_domain, suffix. `OSINTIZADA_PSL_FILE`
  permite uma lista mais recente.
- **Persistência** SQLAlchemy 2.0 (SQLite em dev, PostgreSQL em prod) com migração Alembic `0001_initial`:
  cases, investigations, case_inputs (seeds `USER_INPUT`), entities, evidence, relationships +
  relationship_evidence, search_executions, audit_log, pivots, correlations, conflicts.
- **Repositories** (única camada com acesso ao banco) e **canonicalização** central
  (`normalize_email/phone/domain/ip/username/url`); fingerprint de entidade `sha256(TIPO:valor)`.
- **Providers reais**: `infra.dns` (A/AAAA/CNAME/MX/NS/TXT, SPF, PTR, IP→ASN e nome do AS via Team Cymru),
  `infra.rdap` (IP/ASN/domínio via rdap.org, sem inventar dado redigido), `infra.crtsh` (subdomínios por
  Certificate Transparency), `archive.wayback` (Wayback CDX, sempre `HISTORICAL_DATA`),
  `social.telegram` (Telethon; Telegram ID estável + username mutável; `NOT_CONFIGURED` sem credenciais).
- **InvestigationService**: Case → seeds → rodadas por profundidade → persistência → pivôs → correlação,
  com orçamentos `max_depth`, `max_entities`, `max_pivots`, `max_provider_calls` e `max_runtime`
  (`BUDGET_EXHAUSTED` sem erro) e anti-loop por fingerprints visitados e agendados.
- **PivotEngine**: prioridades HIGH/MEDIUM/LOW configuráveis, confiança mínima, blocklist de plataformas,
  bloqueio por investigador, IPs não públicos ignorados; cada decisão registrada com motivo.
- **CorrelationEngine**: score determinístico com sinais positivos/negativos e evidências; `SAME_AS` só com
  sinal forte (Telegram ID, telefone, email); **ConflictRecord** (`CONFLICTING_EVIDENCE`) para atributos
  divergentes, nunca sobrescritos.
- **API FastAPI**: cases, investigate (background), entities (com "como chegamos aqui?"), evidence,
  relationships, searches, audit, pivots, correlations, conflicts, providers, providers/health; token
  opcional (`OSINTIZADA_API_TOKEN`).
- **CLI**: `investigate`, `cases`, `case`, `serve`, `db upgrade`, `--log-level`.
- **Logs estruturados** JSON com `case_id` propagado por contextvar e sanitização automática;
  `sanitize_payload` remove credenciais de raw_data, metadata e audit.
- Contrato de providers: `ProviderItem`, `EntityRef` (`source_entity`), `relation_direction`, `attributes`,
  `source_type`, `error_code`; exceções `ProviderTimeout`, `ProviderRateLimited`,
  `ProviderAuthenticationError`, `ProviderNotConfigured`, `ProviderUnavailable`.
- Configuração por provider: `requests_per_second`, `concurrency`, `cache_ttl`, `max_results`; teto global
  de concorrência (`search.global_concurrency`).
- 108 novos testes (297 no total), incluindo o fluxo completo domínio → DNS → IP → pivô → RDAP → ASN/ORG.

### Alterado
- Retry: apenas timeout, 408, 502/503/504 e erros de rede; 400/401/403/404/500 nunca são re-tentados; 429 é
  re-tentado uma vez quando o `Retry-After` é curto, senão `RATE_LIMITED` com pausa do provider.
- Modos: QUICK com depth 1, DEEP com depth 2 e tier 5 (inclui archive), todos com orçamentos de investigação.
- Custos do query budget: `dns: 0`, `api: 1`, `archive: 1`.
- `registrable_domain()` delega à PSL; a lista manual de sufixos e a lista de TLDs foram removidas.

### Corrigido
- Nomes DNS com `_` (ex.: `_spf.google.com`) eram descartados como hostnames inválidos.
- `row_to_dict` confundia a coluna `metadata` com o `MetaData` do SQLAlchemy.

## [0.2.0] — 2026-09-28 — Fase 2a–2c: resiliência, extractors e search engines

### Adicionado
- **Resiliência** (`osintizada/resilience/`): token bucket por provider, cooldown após HTTP 429 respeitando
  `Retry-After`, retry com backoff exponencial + jitter apenas para falhas transitórias, circuit breaker
  (closed/open/half-open), cache TTL por provider com versão de parser na chave, `ProviderRuntime` compartilhado
  com métricas (chamadas, cache hits, retries, rate limits).
- **Rede segura** (`osintizada/net/`): `SafeHTTPClient` com proteção SSRF (resolve e bloqueia IPs não públicos,
  metadados de nuvem, IPv4 mapeado), redirecionamentos revalidados, limite de tamanho, timeouts, parsing de
  `Retry-After`.
- **Entity Extractors** (`osintizada/extractors/`): URL, Telegram (inclui convites), perfis sociais, email
  (inclui ofuscado), cripto, CPF, CNPJ, IP/CIDR, ASN, domínio, telefone, hash e menções, com pipeline que evita
  sobreposição e deduplica ocorrências.
- **Search engines**: `SearchEngineProvider` base, **Brave Search API** e **Google Programmable Search**;
  consultas com operadores não suportados são recusadas (SKIPPED), nunca alteradas.
- **SearchManager**: compatibilidade consulta×mecanismo e agregação de páginas entre mecanismos.
- Orquestrador executa as consultas do QueryPlanner nos mecanismos, com limite de consultas do modo,
  orçamento e registro de `reason`/prioridade de cada consulta no search log; **RAW SEARCH executável**.
- Carregamento de `.env` local (sem sobrescrever o ambiente).
- CLI: `search`, `extract`, `providers --health`; `run` mostra páginas deduplicadas e uso do orçamento.
- `BaseProvider.http_client()` / `fetch()` com mapeamento padronizado de HTTP 429/401/5xx.
- Status `SKIPPED` via `SkippedError`; `rate_limit_per_minute: 0` desativa o limite padrão.
- 78 novos testes (189 no total), todos com rede simulada.

### Alterado
- `BaseProvider.search()` passa a aplicar cache → cooldown → circuit breaker → concorrência → rate limit →
  retry → timeout. Contrato público (`search`, `_search`, status) inalterado.
- Valor canônico de contas sociais unificado como `plataforma:handle` (seeds e derivações usavam formatos
  diferentes); `TELEGRAM_USER` usa o handle em minúsculas.

## [0.1.0] — 2026-09-28 — Fase 1: Core

### Adicionado
- **Identifier Engine** (`osintizada/core/identifiers.py`): detecção de ~35 tipos de identificador com
  múltiplas hipóteses ordenadas por confiança e motivo explícito (identidade, internet, redes sociais,
  Telegram, cripto, hashes, onion). Validação de DV de CPF/CNPJ e checksum Bitcoin (base58check, bech32/bech32m).
- **Normalização** (`osintizada/core/normalization.py`): valor original sempre preservado, forma canônica
  para deduplicação e variantes apenas para pesquisa (telefone BR/E.164, CPF/CNPJ formatado/dígitos,
  raiz de CNPJ, email com forma canônica Gmail, domínios com punycode, URLs canônicas, IP/CIDR/ASN).
- **Modelos de domínio** (`osintizada/core/models.py`): `Entity` (com origem SEED/DISCOVERED/DERIVED/MANUAL),
  `Relationship` (exige motivo e evidência), `Evidence`, `ProviderResult`, `ProviderResponse`.
- **Enumerações** controladas: entidades, relações, status de provider (SUCCESS/NO_RESULTS/FAILED/
  RATE_LIMITED/AUTH_REQUIRED/NOT_CONFIGURED/TIMEOUT/SKIPPED/CANCELLED), classificação de dados, tiers, modos.
- **Query Planner** + `QueryBuilder`: consultas por tipo com `reason`, prioridade, custo, categoria, profundidade,
  operadores detectados, deduplicação, limites por modo, exclusão de domínios e RAW SEARCH.
- **Evidence Engine**: hash SHA-256 do conteúdo bruto, fingerprint independente de provider, deduplicação por URL
  canônica (N motores → 1 evidência com `duplicate_sightings`).
- **Interface de providers**: `BaseProvider` (template method com timeout, mapeamento de erros, sanitização)
  e especializações `LocalProvider`, `APIProvider`, `HTTPProvider`, `BrowserProvider`, `TorProvider`, `PaidProvider`;
  `ProviderRegistry` plugin-like; healthcheck com credenciais mascaradas.
- **SourceOrchestrator**: seleção de fontes justificada, execução paralela com concorrência limitada, orçamento
  de custo, bloqueio/whitelist de providers, cancelamento e search log.
- **Provider local** `local.identifier_analysis`: derivações estruturais (domínio do email, local-part como
  hipótese de username, domínio/subdomínio de URL, perfil social de URL), todas marcadas como DERIVED.
- **Configuração central** (`config/osintizada.yaml` + `osintizada/config.py`) com perfis de modo.
- **Secrets**: leitura apenas de variáveis de ambiente, mascaramento e filtro de logging que remove credenciais.
- **CLI**: `detect`, `plan`, `raw`, `run`, `providers`.
- 111 testes automatizados.
