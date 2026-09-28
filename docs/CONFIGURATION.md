# Configuração

## Fontes e precedência

1. Padrões em `osintizada/config.py`
2. YAML: `$OSINTIZADA_CONFIG` ou `config/osintizada.yaml` (merge profundo — só informe o que muda)
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

network:
  user_agent: "OSINTIZADA/0.2 (+investigation research tool)"

providers:
  <nome.do.provider>:
    enabled: true
    timeout_seconds: 20
    tier: 2                     # sobrescreve o tier declarado
    rate_limit_per_minute: 30   # token bucket local; 0 desativa o padrão do provider
    max_concurrency: 2          # chamadas simultâneas deste provider
    cache_ttl_seconds: 3600     # 0 desativa cache

modes:
  quick:                        # também: deep, investigation, deep_sweep, raw
    max_queries: 15
    max_queries_per_identifier: 8
    max_provider_tier: 3
    query_categories: [exact, social, telegram, code]   # vazio = todas
```

## Modos

| Modo | depth | consultas | tier máx. | Observação |
|---|---|---|---|---|
| quick | 0 | 15 | 3 | poucas categorias, rápido |
| deep | 1 | 60 | 4 | pivôs habilitados (Fase 6) |
| investigation | 2 | 100 | 4 | persistência em Case (Fase 7) |
| deep_sweep | 3 | 250 | 5 | Tor só se `tor.mode` habilitar |
| raw | 0 | 1 | 3 | consulta manual do investigador |

## Secrets

- Somente via variáveis de ambiente (ou `.env` local, ignorado pelo Git — ver `.env.example`).
- Providers declaram `required_secrets`; sem eles o status é `NOT_CONFIGURED`.
- Exibição sempre mascarada (`mask_secret`), logs filtrados por `SecretRedactingFilter`.
- Nunca colocar chaves no YAML, no código ou em prompts enviados a modelos de IA.

## Resiliência — comportamento

| Situação | Comportamento |
|---|---|
| HTTP 5xx, erro de conexão | retry com backoff exponencial + jitter (`max_retries`) |
| HTTP 429 | **não** re-tenta; status `RATE_LIMITED`, provider entra em pausa pelo `Retry-After` (ou `default_cooldown_seconds`); chamadas durante a pausa retornam `RATE_LIMITED` sem tocar o serviço |
| Falhas consecutivas ≥ limite | circuit breaker abre; chamadas retornam `SKIPPED` até o período de recuperação |
| Mesma consulta no mesmo provider | servida do cache (`metadata.cache_hit = true`); falhas nunca são cacheadas |
| Timeout | `TIMEOUT` (conta para o circuit breaker) |

A chave de cache inclui provider, `parser_version`, tipo e valor do identificador, consulta e parâmetros.
O backend padrão é em memória (`InMemoryTTLCache`); a interface `CacheBackend` permite Redis sem alterar providers.

## Rede e SSRF

Todo acesso HTTP de provider passa por `SafeHTTPClient`:

- somente `http`/`https`, sem credenciais embutidas na URL;
- o host é resolvido e **todos** os IPs precisam ser públicos (bloqueia loopback, redes privadas,
  `169.254.169.254`/metadados de nuvem, IPv4 mapeado em IPv6, `localhost`, `*.internal`);
- redirecionamentos são seguidos manualmente e cada destino é revalidado (máx. 5);
- limite de tamanho de resposta e timeout obrigatórios;
- `trusted_hosts` do provider (endpoints fixos de API declarados no código) dispensam apenas a resolução DNS.
