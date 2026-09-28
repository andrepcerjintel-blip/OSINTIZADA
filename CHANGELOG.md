# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).

## [0.4.1] — 2026-09-28 — Revisão da fase 4

### Adicionado
- **Rate limit compartilhado via Redis** (`infrastructure/redis_rate_limit.py`): token bucket atômico (Lua,
  relógio do próprio Redis) por provider, comum a API e a todos os workers; cooldown de HTTP 429 também
  compartilhado. Sem Redis, ou com `resilience.shared_rate_limit: false`, o limite é por processo. Redis
  indisponível no meio da execução degrada para o limite local (aviso + métrica `rate_limit_redis_error`).
- `REDIS_CACHE_URL` (opcional): cache em Redis/DB separado (política LRU), enquanto fila e locks ficam num
  Redis `noeviction`.
- Timeline: `published_at` informado pela fonte (ex.: `page_age` da Brave, `article:published_time` do
  Google) é gravado na evidência e usado como `time_basis=published_at`; página sem data não vira
  `DOCUMENT_PUBLISHED`. Providers podem declarar o tipo do fato (`raw.event_type`, ex.: `MESSAGE_POSTED`,
  `COMMIT_CREATED`); `LOCATION` gera `ADDRESS_OBSERVED`.
- SSRF: `.onion` recusado no cliente HTTP comum (exige o cliente Tor dedicado).
- `infrastructure.redis_cache.build_runtime()`: único ponto que monta cache + rate limiter para API e worker.

## [0.4.0] — 2026-09-28 — Fase 4: resiliência, jobs persistentes e segurança

### Adicionado
- **Jobs persistentes** (`jobs`, `job_attempts`, `job_events`; migração `0002_jobs`), separados do Case.
  Status `PENDING → QUEUED → RUNNING → COMPLETED | RETRYING | FAILED | CANCELLED | INTERRUPTED`.
  A API só cria e enfileira: `POST /cases/{id}/investigate` → `202 {case_id, job_id, status: "QUEUED"}`.
- **Fila RQ sobre Redis** (`infrastructure/queue.py`, `JSONSerializer`: sem pickle). A mensagem leva só o
  `job_id`; todo estado fica no banco. Outbox simplificado: commit do Job como `PENDING`, publicação e só então
  `QUEUED`. Se o Redis cair, o Job permanece `PENDING` e o reconciliador republica.
- **Worker separado** (`osintizada worker`): claim atômico (`UPDATE … WHERE status IN (…)` +
  `execution_token`), heartbeat do job e do worker, **lock distribuído por Case** (TTL + token, renovado no
  heartbeat, nunca liberado por outro dono), checkpoints por rodada com retomada, cancelamento cooperativo.
- **JobRecoveryService / reconciliador** (na subida da API e do worker, e periodicamente com lock próprio):
  RUNNING sem heartbeat → `INTERRUPTED` → `RETRYING` (retoma do checkpoint) ou `FAILED`
  `MAX_ATTEMPTS_EXCEEDED` (dead letter); `PENDING` antigos e `RETRYING` vencidos → fila; `QUEUED` sem
  mensagem na fila → republicados. Nunca marca `COMPLETED`.
- **Idempotência**: entrega duplicada é descartada pelo claim; resultado de worker que perdeu a posse é
  descartado (heartbeat/finish/checkpoint condicionados ao token); consultas já concluídas no Case são
  reaproveitadas (`ALREADY_EXECUTED`) em vez de repetidas.
- **RedisCacheBackend** (JSON, TTL por provider, chave `osintizada:provider:<nome>:<parser>:<sha256>`);
  Redis fora → degrada para sem cache, sem falhar a investigação.
- **Endpoints**: `GET /jobs/{id}`, `GET /jobs/{id}/events` (SSE com `Last-Event-ID`), `POST /jobs/{id}/cancel`,
  `POST /jobs/{id}/retry`, `GET /jobs/{id}/download`, `GET /cases/{id}/jobs`, `POST /cases/{id}/exports`
  (job JSON/CSV/HTML), `GET /cases/{id}/timeline`, `POST /cases/{id}/entities/{eid}/flags`, `GET /metrics`
  (Prometheus), `POST /providers/{name}/validate-credentials`; `/health` com api, database, redis, worker
  (`ONLINE`/`STALE`/`OFFLINE`/`WORKER_UNAVAILABLE`), fila e contagem de jobs.
- **SSRF com IP pinning** (anti DNS rebinding): resolução única, todos os IPs validados (tudo-ou-nada),
  conexão no IP validado com `Host`/SNI do nome original, revalidação a cada redirect, política de portas
  (80/443 por padrão), bloqueio de metadata cloud, IPv4 embutido em IPv6 (NAT64/6to4/Teredo/mapped),
  credenciais em URL recusadas, política de proxy (`pin`/`deny`).
- **Correlação visual**: SHA256 + pHash (DCT) + dHash; níveis `EXACT_IMAGE_MATCH`, `PERCEPTUAL_VERY_SIMILAR`,
  `PERCEPTUAL_SIMILAR`; imagens genéricas `LOW_IDENTITY_VALUE` (manual, lista de hashes, reuso em ≥ 3 contas)
  com peso reduzido. Avatar nunca gera `SAME_AS` sozinho. Avatar do Telegram baixado e armazenado por hash.
- **Timeline** pelo momento do fato (`observed_at`), não da coleta; filtros por tipo, provider e período.
- **Exports** JSON, CSV (zip, proteção contra injeção de fórmula) e HTML autocontido, gerados por job.
- `validate_credentials()` e lista `missing` de variáveis por provider (Telegram/Search `NOT_CONFIGURED`).
- Logs com `case_id`, `job_id` e `provider`; métricas compartilhadas entre processos via Redis.
- Validação de subida (banco/migrações/Redis), `Dockerfile` e `docker-compose.yml` (api, worker, redis,
  postgres, healthchecks, sem senhas no arquivo), `osintizada worker-status`, `osintizada reconcile`.

### Alterado
- Status do Case derivado dos Jobs (nunca de flag solta).
- TTL do lock do Case segue `stale_after_seconds` por padrão (lock de worker morto expira junto com o
  abandono do job).
- SQLite em arquivo usa WAL + `busy_timeout` (API e worker em processos separados).

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
