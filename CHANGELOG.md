# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).

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
