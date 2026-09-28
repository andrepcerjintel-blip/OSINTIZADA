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

providers:
  <nome.do.provider>:
    enabled: true
    timeout_seconds: 20
    tier: 2                     # sobrescreve o tier declarado
    rate_limit_per_minute: 30   # (reservado — Fase 2)
    cache_ttl_seconds: 3600     # (reservado — Fase 2)

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
