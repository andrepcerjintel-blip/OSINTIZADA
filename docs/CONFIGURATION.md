# Configuração do RINO

## Fontes e precedência

1. Padrões em `osintizada/config.py`
2. YAML: `$RINO_CONFIG` ou `config/rino.yaml` (merge profundo — só informe o que muda). Legado aceito:
   `$OSINTIZADA_CONFIG` e `config/osintizada.yaml` (usado só se `config/rino.yaml` não existir)
3. Overrides em código: `load_settings(overrides={...})`

## Seções

```yaml
search:
  max_depth: 2                  # profundidade máxima de pivôs (Fase 6)
  max_queries: 100
  max_entities: 500
  max_results_per_provider: 50  # resultados além disso são truncados
  concurrency: 8
  provider_timeout_seconds: 30
  depth_priority_penalty: 10    # prioridade de consulta subtraída por nível de profundidade

query_budget:
  max_cost_per_search: 200      # soma dos custos dos providers executados
  costs: {local: 0, web: 1, api: 2, browser: 3, tor: 4, paid: 5}

tor:
  mode: TOR_DISABLED            # TOR_DISABLED | TOR_PASSIVE | TOR_DEEP_SEARCH

resilience:
  max_retries: 2                # re-tentativas apenas para falhas transitórias (5xx, conexão)
  backoff_base_seconds: 0.5     # backoff exponencial com jitter: 0.5s, 1s, 2s… até o máximo
  backoff_max_seconds: 8
  breaker_failure_threshold: 5  # falhas consecutivas que abrem o circuit breaker
  breaker_recovery_seconds: 120 # após isso, uma chamada de teste (half-open)
  default_cooldown_seconds: 60  # pausa após HTTP 429 sem Retry-After
  default_cache_ttl_seconds: 3600
  max_response_bytes: 5242880   # respostas maiores são recusadas
  shared_rate_limit: true       # com Redis: rate limit e cooldown de 429 comuns a API + todos os workers

network:
  user_agent: "RINO/0.5 (+investigation research tool)"
  allowed_ports: [80, 443]      # portas aceitas em URLs não confiáveis
  blocked_networks: []          # redes extras bloqueadas (CIDR)
  proxy_policy: pin             # pin | deny

redis:                          # URL somente via REDIS_URL (pode conter senha)
  socket_timeout_seconds: 5

cache:
  backend: memory               # memory (por processo) | redis (compartilhado entre API e workers)
  prefix: osintizada            # prefixo de TODAS as chaves Redis (nome legado mantido: chaves existentes)

jobs:
  queue_name: osintizada        # nome legado mantido: jobs já enfileirados
  heartbeat_interval_seconds: 15
  stale_after_seconds: 90       # heartbeat mais antigo → INTERRUPTED → RETRYING/FAILED
  max_attempts: 3               # tentativas do JOB (≠ retry de provider)
  retry_backoff_seconds: 30     # × 2^(tentativa-1)
  pending_dispatch_after_seconds: 10    # PENDING não publicado → republica
  queued_redispatch_after_seconds: 300  # QUEUED sem mensagem na fila → republica
  case_lock_ttl_seconds: null   # null → igual a stale_after_seconds (mínimo: 2 heartbeats + 1 s)
  case_lock_wait_seconds: 5     # espera pelo lock do Case antes de devolver o job (sem gastar tentativa)
  reconcile_interval_seconds: 30
  worker_heartbeat_interval_seconds: 10 # worker ONLINE se heartbeat ≤ 2× intervalo
  reuse_executions_max_age_hours: 24    # consulta concluída no Case é reaproveitada (refresh=true força)
  export_dir: data/exports
  artifact_dir: data/artifacts

images:
  max_bytes: 5242880
  max_pixels: 40000000          # proteção contra decompression bomb
  phash_very_similar: 6         # Hamming (64 bits); também exige dHash ≤ dhash_confirm
  phash_similar: 12
  dhash_confirm: 12
  generic_reuse_threshold: 3    # mesma imagem em ≥ N contas do Case → LOW_IDENTITY_VALUE
  known_generic_sha256: []      # avatares padrão conhecidos

providers:
  <nome.do.provider>:
    enabled: true
    timeout_seconds: 20
    tier: 2                     # sobrescreve o tier declarado
    requests_per_second: 2      # ou rate_limit_per_minute; aplicado ANTES da requisição; 0 desativa
    concurrency: 2              # (= max_concurrency) chamadas simultâneas deste provider
    cache_ttl: 3600             # (= cache_ttl_seconds) 0 desativa cache
    max_results: 200            # itens aceitos por resposta

modes:
  quick:                        # também: deep, investigation, deep_sweep, raw
    max_queries: 15
    max_queries_per_identifier: 8
    max_provider_tier: 3
    query_categories: [exact, social, telegram, code]   # vazio = todas
```

## Modos e orçamentos

| Modo | max_depth | tier máx. | max_entities | max_pivots | max_provider_calls | max_runtime |
|---|---|---|---|---|---|---|
| quick | 1 | 3 (sem archive) | 100 | 10 | 60 | 120 s |
| deep | 2 | 5 | 250 | 40 | 300 | 600 s |
| investigation | 2 | 5 | 500 | 60 | 500 | 900 s |
| deep_sweep | 3 | 5 | 1000 | 150 | 1500 | 1800 s |
| raw | 0 | 3 | 100 | 0 | 10 | 60 s |

Todos os modos persistem em Case. Os valores podem ser sobrepostos por requisição (`max_depth`,
`max_entities`, `max_pivots`, `max_provider_calls`, `max_runtime_seconds`), sempre limitados por
`search.hard_max_depth`. Esgotar um orçamento registra `BUDGET_EXHAUSTED` no audit e no resumo e **não** é
tratado como erro.

**Profundidade:** `depth=0` é o seed. `depth=1` são os resultados diretos do seed, `depth=2` os derivados de
depth 1, e assim por diante. Entidades com `depth ≤ max_depth` são investigadas. As de profundidade maior
são armazenadas, mas não pivotadas. Fingerprints visitados e agendados impedem loops.

## Pivôs

```yaml
pivots:
  min_confidence: 0.5          # hipóteses abaixo disso não viram pivô (seeds sempre viram)
  skip_non_public_ips: true
  priorities: {EMAIL: 3, PHONE: 3, DOMAIN: 3, IP: 3, USERNAME: 2, SUBDOMAIN: 2, ASN: 2, URL: 1, LOCATION: 0}
  blocklist_domains: [gmail.com, github.com, cloudflare.com, amazonaws.com, ...]
```

3 = HIGH, 2 = MEDIUM, 1 = LOW, 0 = nunca. Os candidatos são ordenados por prioridade e confiança e cortados
por `max_pivots`. A blocklist evita investigar a plataforma em vez do alvo (ex.: nameservers da
Cloudflare). O investigador pode bloquear valores por requisição (`blocked_values`).

## Correlação

```yaml
correlation:
  weights: {same_telegram_id: 60, same_phone: 50, same_email: 45, same_username_rare: 25,
            same_username_common: 10, same_domain: 15, same_name: 5, same_location: 5,
            conflicting_country: -15, conflicting_location: -10}
  strong_signals: [same_telegram_id, same_phone, same_email]
  same_as_threshold: 80        # + exige sinal forte → SAME_AS
  possible_threshold: 35       # → POSSIBLY_SAME_AS
  weak_threshold: 10           # → registrado como WEAK, sem relação
  conflict_attributes: [country, location, city, display_name]
```

## Banco de dados

- `DATABASE_URL` (env) > `database.url` > `sqlite:///data/osintizada.db` (arquivo com nome legado, preservado).
- PostgreSQL: `pip install -e ".[postgres]"` e `DATABASE_URL=postgresql+psycopg://…`.
- Migrações: `rino db upgrade` (Alembic). A API aplica as migrações na inicialização (`RINO_AUTO_MIGRATE=0` desativa).

## Variáveis de ambiente

| Variável | Uso |
|---|---|
| `DATABASE_URL` | conexão do banco |
| `REDIS_CACHE_URL` | opcional: Redis/DB só para o cache (ex.: `maxmemory-policy allkeys-lru`); ausente → `REDIS_URL` |
| `REDIS_URL` | Redis (fila, locks, cache, heartbeats). `rediss://:senha@host:6380/0` para TLS. Sem ela: jobs ficam `PENDING` (`QUEUE_UNAVAILABLE`) e `rino worker` se recusa a iniciar |
| `RINO_AUTO_MIGRATE` | `1` (padrão) aplica migrações na subida; `0` exige banco já migrado (falha com mensagem clara) |
| `RINO_TEST_DATABASE_URL` | só testes: roda `tests/test_jobs.py` contra PostgreSQL real (schema recriado) |
| `POSTGRES_PASSWORD`, `REDIS_PASSWORD` | só `docker-compose.yml` (obrigatórias; nunca versionadas) |
| `RINO_API_TOKEN` | exige `Authorization: Bearer` na API (obrigatório fora de localhost) |
| `BRAVE_SEARCH_API_KEY`, `GOOGLE_CSE_API_KEY`, `GOOGLE_CSE_CX` | buscadores |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION` | Telegram |
| `RINO_DNS_SERVERS` | resolvers DNS (padrão: sistema) |
| `RINO_RDAP_BASE` | bootstrap RDAP (padrão: https://rdap.org) |
| `RINO_PSL_FILE` | Public Suffix List mais recente que a embutida |
| `RINO_CONFIG`, `RINO_ENV_FILE` | caminhos alternativos de configuração |

Toda variável `RINO_<NOME>` também é aceita com o prefixo legado `OSINTIZADA_<NOME>`; se as duas existirem,
vale a `RINO_`.

## Secrets

- Somente via variáveis de ambiente (ou `.env` local, ignorado pelo Git — ver `.env.example`).
- Providers declaram `required_secrets`; sem eles o status é `NOT_CONFIGURED`.
- Exibição sempre mascarada (`mask_secret`), logs filtrados por `SecretRedactingFilter`.
- Nunca colocar chaves no YAML, no código ou em prompts enviados a modelos de IA.

## Resiliência — comportamento

| Situação | Comportamento |
|---|---|
| timeout, HTTP 408/502/503/504, erro de rede | retry com backoff exponencial + jitter (`max_retries`) |
| HTTP 400/401/403/404/500 | sem retry |
| HTTP 429 | até `rate_limit_retries` novas tentativas se `Retry-After` (ou backoff) ≤ `max_rate_limit_wait_seconds`; senão `RATE_LIMITED` e o provider entra em pausa; chamadas durante a pausa retornam `RATE_LIMITED` sem tocar o serviço |
| Concorrência | semáforo global (`search.global_concurrency`) + do modo + por provider (`concurrency`) |
| Falhas consecutivas ≥ limite | circuit breaker abre; chamadas retornam `SKIPPED` até o período de recuperação |
| Mesma consulta no mesmo provider | servida do cache (`metadata.cache_hit = true`); falhas nunca são cacheadas |
| Timeout | `TIMEOUT` (conta para o circuit breaker) |

A chave de cache inclui provider, `parser_version`, tipo e valor do identificador, consulta e parâmetros.
O backend padrão é em memória (`InMemoryTTLCache`); a interface `CacheBackend` permite Redis sem alterar providers.

## Rede e SSRF

Todo acesso HTTP de provider passa por `SafeHTTPClient`:

- somente `http`/`https`, sem credenciais embutidas na URL, porta em `network.allowed_ports`
  (providers podem declarar outras);
- o host é resolvido **uma vez** e **todos** os IPs precisam ser globais (bloqueia loopback, redes
  privadas, CGNAT, link-local, multicast, reservados, metadados de nuvem `169.254.169.254` /
  `fd00:ec2::254` / `100.100.100.200`, IPv4 embutido em IPv6 mapped/6to4/Teredo/NAT64, `localhost`,
  `*.internal` e `network.blocked_networks`). Um único IP proibido recusa a URL inteira;
- **IP pinning**: a conexão vai para o IP validado, com `Host` e SNI do nome original. Uma nova resolução
  (DNS rebinding) não muda o destino;
- redirecionamentos são seguidos manualmente e cada destino é revalidado (máx. 5);
- proxy: com `pin` (padrão) o CONNECT vai ao IP validado. Com `proxy_policy: deny`, ou quando o proxy de
  ambiente resolve DNS por conta própria (`socks5h`, detectado automaticamente), URLs fora de
  `trusted_hosts` são recusadas, porque o pinning não pode ser garantido;
- limite de tamanho de resposta e timeout obrigatórios;
- `trusted_hosts` do provider (endpoints fixos de API declarados no código, ex.: RIRs do RDAP) dispensam
  apenas a validação de IP; porta e esquema continuam validados.
