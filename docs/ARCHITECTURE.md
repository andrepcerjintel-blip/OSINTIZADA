# Arquitetura do OSINTIZADA

## 1. Diagnóstico inicial (2026-09-28)

O repositório `OSINTIZADA` estava **vazio** no início desta fase: nenhum commit, nenhuma branch remota e
nenhum arquivo além de `.git`. Consequências:

- não havia arquitetura, banco, rotas, UI, integrações ou código legado a preservar;
- risco de regressão inexistente na Fase 1; o foco foi estabelecer contratos estáveis para que as fases
  seguintes **estendam** em vez de reescrever;
- a stack sugerida (Python 3.11+, Pydantic, asyncio/httpx) foi adotada sem conflito com nada existente.

## 2. Fluxo conceitual

```
INPUT → IDENTIFIER ENGINE → QUERY PLANNER → SOURCE SELECTION → COLLECTORS/PROVIDERS → RAW EVIDENCE
      → PARSING → NORMALIZATION → ENTITY EXTRACTION → PIVOT ENGINE → CORRELATION ENGINE
      → CONFIDENCE SCORING → TIMELINE → ENTITY GRAPH → INVESTIGATIVE REPORT
```

Implementado na Fase 1 (em negrito):
**INPUT → IDENTIFIER ENGINE → NORMALIZATION → QUERY PLANNER → SOURCE SELECTION → PROVIDERS → RAW EVIDENCE →
ENTITIES (depth 0)**.

## 3. Estrutura atual

```
osintizada/
  config.py                      configuração central (YAML + padrões + perfis de modo)
  cli.py / __main__.py           CLI: detect, plan, raw, run, providers
  core/
    enums.py                     vocabulários controlados (tipos, relações, status, classificação, tiers)
    models.py                    Entity, Relationship, Evidence, ProviderResult/Response, candidatos
    validators.py                DV CPF/CNPJ, hostname, eTLD+1 aproximado, checksum BTC
    urls.py                      URL canônica (dedup) e parsing de perfis sociais
    identifiers.py               IdentifierEngine (detecção multi-hipótese)
    normalization.py             normalização por tipo (original + canônico + variantes)
    query_builder.py             operadores de busca (site:, filetype:, inurl:, intitle:, -, OR)
    query_planner.py             QueryPlanner (templates, prioridade, custo, motivo, limites)
    evidence.py                  EvidenceEngine (hash, fingerprint, dedup, entidades)
    secrets.py                   env vars, mascaramento, sanitização de logs
  providers/
    base/provider.py             BaseProvider + Local/API/HTTP/Browser/Tor/Paid
    base/registry.py             ProviderRegistry plugin-like
    local/identifier_analysis.py derivações estruturais (DERIVED)
  orchestration/
    source_orchestrator.py       seleção de fontes + execução paralela + search log
config/osintizada.yaml
tests/                           111 testes (pytest)
```

## 4. Decisões de design

| Decisão | Motivo |
|---|---|
| Detecção devolve **lista** de candidatos com `reason` | nunca forçar classificação única; ambiguidade é explícita |
| `original` / `value` / `variants` separados | preservar input, deduplicar pelo canônico, variantes só para busca |
| Input do usuário vira `Entity(origin=SEED)` | distinguir input do investigador de descoberta OSINT |
| `Relationship` exige `reason` e `evidence_ids` (validação) | impossível criar ligação sem motivo registrado |
| `BaseProvider.search()` como *template method* | status (SUCCESS/NO_RESULTS/FAILED/…) uniforme; falha de um provider não derruba a investigação |
| Lista vazia = NO_RESULTS; falha = exceção | nunca misturar ausência com falha |
| Fingerprint de evidência independe do provider | 15 motores com a mesma página = 1 evidência + `duplicate_sightings` |
| `ProviderResult.confidence` ≠ confiança de identidade | conceitos separados; identidade será do CorrelationEngine |
| Dados derivados marcados `DERIVED` | cálculo/inferência nunca aparece como dado de fonte |
| Constantes em `config.py`/YAML | nada de limites espalhados pelo código |

## 5. Gap analysis (itens do prompt mestre)

Legenda: **IMPLEMENTADO** · **PARCIAL** · **AUSENTE** · **PRECISA REFATORAÇÃO** (nenhum item, projeto novo).

| # | Item | Estado | Observação |
|---|---|---|---|
| 3 | Tipos de input / detecção multi-hipótese | IMPLEMENTADO | ~35 tipos; imagem/PDF (arquivos) ausentes |
| 4 | Normalização | IMPLEMENTADO | eTLD+1 por heurística (sem PSL completa) |
| 5 | SourceOrchestrator | PARCIAL | seleção + execução depth 0; sem presets |
| 6–7 | Provider architecture / response | IMPLEMENTADO | 6 tipos base, resposta padronizada |
| 8 | SearchManager (multi-engine) | AUSENTE | Fase 2 |
| 9 | QueryPlanner | IMPLEMENTADO | prioridade, custo, motivo, limites, dedup |
| 10 | Query expansion | PARCIAL | `plan(depth=n)` pronto; gatilho vem com PivotEngine |
| 11 | Deep Sweep | PARCIAL | perfil de modo configurado; recursão ausente |
| 12 | Execução paralela | IMPLEMENTADO | semáforo global por modo; limite por provider reservado |
| 13 | Rate limit / retry / backoff / circuit breaker | PARCIAL | status RATE_LIMITED + retry_after; política ausente |
| 14 | Cache | AUSENTE | Fase 2 |
| 15 | Telegram | AUSENTE | Fase 3 (Telethon) |
| 16–17 | Social / GitHub | PARCIAL | parsing de URLs sociais; coleta na Fase 3 |
| 18–19 | Brazil OSINT / busca documental | PARCIAL | consultas documentais geradas; providers na Fase 4 |
| 20 | Archive | AUSENTE | Fase 2 |
| 21–22 | Infraestrutura / subdomínios | PARCIAL | derivação domínio/subdomínio; coleta na Fase 5 |
| 23 | Tor | PARCIAL | `TorProvider` isolado e desabilitado por padrão; worker ausente |
| 24–25 | Image intelligence / pHash | AUSENTE | Fase 8 |
| 26 | Credilink | PARCIAL | `PaidProvider` + secrets; integração Fase 9 |
| 27 | Entity extractors | AUSENTE | Fase 2 (necessário para parsing de páginas) |
| 28 | PivotEngine | AUSENTE | Fase 6 |
| 29–30 | Entidades / relações | IMPLEMENTADO | modelos e vocabulários completos |
| 31–32 | Correlation / confidence score explicado | AUSENTE | Fase 6 |
| 33–34 | Evidence engine / raw evidence | IMPLEMENTADO | hash + raw em memória; armazenamento de arquivos ausente |
| 35 | Timeline | PARCIAL | `observed_at` vs `collected_at` já separados; engine ausente |
| 36–37 | Graph / path finder | AUSENTE | Fase 7 |
| 38 | Case management | AUSENTE | `case_id` já presente nos modelos |
| 39 | Modos | PARCIAL | perfis configurados; comportamento completo depende de fases futuras |
| 40–42 | Interface / execution view / abas | AUSENTE | CLI provisória com status por provider |
| 43–44 | Summary / AI layer | AUSENTE | regra "código produz evidência" refletida no design |
| 45–47 | Falsos positivos / contradições / duplicatas | PARCIAL | dedup de evidência pronto; contradições ausentes |
| 48 | Database | AUSENTE | tudo em memória na Fase 1 |
| 49 | Audit log | AUSENTE | search log de execução disponível |
| 50 | Security | PARCIAL | `.gitignore`, secrets, sanitização; SSRF/upload ausentes (sem HTTP ainda) |
| 51–52 | API key panel / healthcheck | PARCIAL | CLI `providers` + healthcheck mascarado |
| 53 | Configuração central | IMPLEMENTADO | |
| 54 | Plugin-like | IMPLEMENTADO | decorator `@register_provider` |
| 57 | Export | AUSENTE | JSON via `--json` apenas |
| 58 | Search history | PARCIAL | `SearchRun.search_log` (não persistido) |
| 59–60 | Query budget / source priority | IMPLEMENTADO | custos e tiers configuráveis |
| 61 | Fail gracefully | IMPLEMENTADO | |
| 62 | Observability | AUSENTE | |
| 63 | Testes | IMPLEMENTADO | 111 testes |
| 66–67 | Sem placeholders / ausência ≠ falha | IMPLEMENTADO | status distintos; `NOT CONFIGURED` |
| 68 | RAW SEARCH | PARCIAL | planejamento/registro; execução com search engines na Fase 2 |
| 69–70 | Multi-input / seed entities | IMPLEMENTADO | correlação entre inputs na Fase 6 |
| 71–73 | Notas manuais / classificação / derivados | PARCIAL | classificação e origem MANUAL/DERIVED prontas |
| 77 | Search explanation (`reason`) | IMPLEMENTADO | |
| 78 | Controle humano | PARCIAL | cancelar, bloquear/whitelist providers, limitar profundidade/fontes |

## 6. Riscos e débitos técnicos conhecidos

- **eTLD+1 heurístico**: lista curta de sufixos compostos; integrar a Public Suffix List (ex.: `tldextract`
  com snapshot offline) antes da Fase 5.
- **Estado em memória**: `SearchRun` não é persistido; Case/DB necessários antes de modos INVESTIGATION reais.
- **Detecção de nomes**: heurística por capitalização; nomes em caixa baixa têm confiança menor. Nunca usar
  sozinha como base de correlação.
- **Telefone internacional sem `+`**: tratado como BR quando 10–11 dígitos com DDD válido; caso contrário
  mantido como dígitos sem país (`metadata.note`).
- **Limites por provider** (`rate_limit_per_minute`, `max_concurrency`, `cache_ttl_seconds`) já estão no
  schema de configuração, mas só passam a valer na Fase 2.

## 7. Arquitetura alvo

```
                 ┌──────────── UI (React/Next.js) ─────────────┐
                 │ Home · Execution View (SSE) · Result tabs    │
                 └──────────────────────┬──────────────────────┘
                                        │ REST + SSE
┌───────────────────────────────── FastAPI ─────────────────────────────────┐
│ Cases · Search · Entities · Evidence · Graph · Timeline · Export · Admin    │
└───────────────┬──────────────────────────────────────────────┬────────────┘
                │ jobs                                          │
        ┌───────▼────────┐    ┌──────────────────────────┐     │
        │ Investigation  │───▶│ SourceOrchestrator        │     │
        │ Runner (worker)│    │  QueryPlanner · Budget    │     │
        │  DeepSweep loop│    │  RateLimiter · Cache      │     │
        └───────┬────────┘    └────────────┬─────────────┘     │
                │                          │ asyncio            │
                │     ┌────────────────────▼──────────────────┐ │
                │     │ Providers (plugins): search, social,   │ │
                │     │ telegram, github, brazil, infra,       │ │
                │     │ archive, image, crypto, commercial     │ │
                │     └────────────────────┬──────────────────┘ │
                │                          │ ProviderResponse    │
                │     ┌────────────────────▼──────────────────┐ │
                │     │ EvidenceEngine → Extractors →          │ │
                │     │ PivotEngine → CorrelationEngine →      │ │
                │     │ Timeline · Graph · Contradictions      │ │
                │     └────────────────────┬──────────────────┘ │
                ▼                          ▼                    ▼
        Redis (queue, cache, locks, rate limit)   PostgreSQL (cases, entidades, evidências, audit)
                                                  Object store local (raw evidence, arquivos)
        Tor worker isolado (SOCKS) — opcional     Neo4j — opcional, só se o grafo exigir
        AI layer (Claude): classificação, sumarização, explicação — nunca fonte da verdade
```

## 8. Roadmap

| Prioridade | Etapa | Entregas |
|---|---|---|
| **P0** | Fase 1 — Core ✅ | identificadores, normalização, modelos, planner, evidência, providers, orquestrador |
| **P0** | Fase 2a — Resiliência de providers | rate limiter por provider, retry/backoff, circuit breaker, cache com TTL por fonte e versão de parser, cliente HTTP com proteção SSRF |
| **P0** | Fase 2b — Entity extractors | email, telefone, URL, domínio, IP, CPF/CNPJ, cripto, Telegram, usernames sociais |
| **P0** | Fase 2c — SearchManager | Brave Search API, Bing, Mojeek/DuckDuckGo quando permitido; dedup entre motores; RAW SEARCH executável |
| **P1** | Fase 2d — Archives | Wayback CDX, Common Crawl index (dados históricos marcados `is_historical`) |
| **P1** | Persistência + Case | SQLAlchemy + PostgreSQL (SQLite em dev), Case, audit log, search history |
| **P1** | Fase 6 — Pivot + Correlation | PivotEngine com `visited_entities`, depth/budget; CorrelationEngine com pesos configuráveis e sinais negativos; score explicado |
| **P1** | Fase 3 — Telegram + GitHub | Telethon (sessão legítima), GitHub REST (perfil, repositórios, emails de commit públicos) |
| **P1** | API FastAPI + SSE | execução em jobs, progresso por provider |
| **P2** | Fase 4 — Brazil OSINT | dados.gov.br, PNCP, Portal da Transparência, DOU, CNPJ em fonte autorizada |
| **P2** | Fase 5 — Infraestrutura | RDAP, DNS, crt.sh, reverse DNS, ASN/BGP; VirusTotal/Shodan/Censys com chave |
| **P2** | Fase 7 — Timeline + Graph + UI | Next.js, grafo com filtros, path finder, "como chegamos aqui?" |
| **P3** | Fase 8 — Image intelligence | hashes, pHash/dHash, EXIF, OCR, QR |
| **P3** | Fase 9 — Credilink / pagos | WebService oficial autorizado apenas |
| **P3** | Fase 10 — Tor worker | worker isolado, modos PASSIVE/DEEP_SEARCH |
| **P3** | Fase 11 — Deep Sweep avançado, export HTML/GraphML/PDF, observabilidade | |

## 9. Próxima etapa recomendada

**Fase 2a + 2b: resiliência de providers e entity extractors**, antes do primeiro provider de rede.

Motivo: todo provider externo dependerá de rate limit, retry/backoff, cache e cliente HTTP seguro (SSRF), e
todo conteúdo coletado precisará passar por extractors para gerar entidades. Implementar isso uma vez, no
`BaseProvider`/`HTTPProvider`, evita duplicação em dezenas de providers e é pré-requisito do PivotEngine.
Em seguida, o SearchManager com Brave Search API (oficial, com chave) fornece o primeiro fluxo real
ponta a ponta usando as consultas que o QueryPlanner já gera.
