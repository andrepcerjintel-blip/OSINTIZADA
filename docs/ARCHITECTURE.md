# Arquitetura do OSINTIZADA

## 1. Histórico

| Versão | Fase | Entrega principal |
|---|---|---|
| 0.1.0 | 1 — Core | identificadores, normalização, modelos, planner, evidência, contrato de providers |
| 0.2.0 | 2a–2c | resiliência (rate limit, retry, breaker, cache), SSRF, extractors, search engines |
| 0.3.0 | 3 — Ciclo investigativo real | PSL, persistência, Case, providers de infraestrutura, Pivot/Correlation, API |

O repositório estava vazio antes da v0.1.0. Não havia código legado.

## 2. Auditoria antes da v0.3.0

| Situação | Itens |
|---|---|
| **EXISTENTE** | Identifier Engine, normalizadores, modelos Pydantic, QueryPlanner, EvidenceEngine (memória), BaseProvider com rate limit, retry, circuit breaker, cache e concorrência por provider **já funcionais**, SafeHTTPClient, 13 extractors, Brave e Google CSE (exigem chave), SourceOrchestrator (depth 0), CLI, 189 testes |
| **REUTILIZÁVEL** | tudo o que está acima. Os fingerprints (`evidence_fingerprint`), o contrato `ProviderResult`/`ProviderResponse` e o SearchManager foram estendidos, não reescritos |
| **PRECISAVA SER ALTERADO** | eTLD+1 por lista manual → PSL; `ProviderResult` sem relações entre itens nem atributos; retry sem 429 e tratando qualquer 5xx como transitório; orquestrador sem orçamentos entre rodadas; concorrência global inexistente |
| **PRECISAVA SER CRIADO** | persistência, Case, SearchExecution, AuditLog, repositories, migrações, providers de infraestrutura, Telegram, PivotEngine, CorrelationEngine, contradições, API, logs estruturados |

Correção sobre as limitações apontadas: na v0.2 o rate limit, o cache e a concorrência por provider já tinham
efeito. Faltavam o teto **global** de concorrência, a declaração `requests_per_second`/`concurrency` e o
retry de 429, que foram entregues nesta fase.

## 3. Fluxo implementado (v0.3)

```
INPUT ─► CASE ─► SEED (case_inputs, origin=SEED, USER_INPUT)
   │
   └─► IDENTIFIER ENGINE ─► QUERY PLANNER ─► SOURCE ORCHESTRATOR
                                                 │  seleção por tipo · tier do modo · credenciais
                                                 │  concorrência global + por provider · rate limit
                                                 │  retry/backoff · circuit breaker · cache · orçamento
                                                 ▼
                                   PROVIDERS REAIS (DNS, RDAP, CT, Wayback, Search, Telegram, local)
                                                 ▼
                          SearchExecution ─► Evidence ─► Entity (fingerprint) ─► Relationship (+evidência)
                                                 ▼
                                   PIVOT ENGINE (prioridade, visited/scheduled, max_depth, orçamentos)
                                                 │ próxima profundidade
                                                 ▼
                          CORRELATION ENGINE (score explicável) + CONFLITOS ─► PERSISTÊNCIA ─► API
```

## 4. Estrutura

```
osintizada/
  config.py                      configuração central: modos, providers, pivôs, correlação, banco
  cli.py                         detect, plan, raw, run, search, extract, investigate, cases, case, serve, db
  core/
    domains.py                   DomainParser (Public Suffix List oficial, ICANN + PRIVATE)
    canonical.py                 canonicalização e fingerprint de entidades
    identifiers.py / normalization.py / validators.py / urls.py
    models.py                    Entity, Relationship, Evidence, ProviderResult(=ProviderItem), ProviderResponse
    enums.py                     vocabulários: status, eventos de audit, source_type, temporalidade…
    evidence.py                  hash e fingerprint de evidência
    query_builder.py / query_planner.py / secrets.py
  db/
    tables.py                    schema SQLAlchemy (12 tabelas)
    session.py                   Database (SQLite dev / PostgreSQL prod), upgrade Alembic
    migrations/                  Alembic (0001_initial)
  repositories/                  Case, Investigation, Entity, Evidence, Relationship, Search, Audit,
                                 Pivot, Correlation, Conflict (única camada que toca o banco)
  investigation/
    service.py                   InvestigationService: ciclo multi-profundidade persistente
    pivot.py                     PivotEngine
    correlation.py               CorrelationEngine + detecção de contradições
  orchestration/                 SourceOrchestrator (rodada) + SearchManager
  providers/
    base/                        BaseProvider (contrato e resiliência) + registry plugin-like
    infrastructure/              dns.py, rdap.py, crtsh.py
    archive/                     wayback.py
    search/                      base.py (SearchEngineProvider), brave.py, google_cse.py
    telegram/                    telethon_provider.py
    local/                       identifier_analysis.py
  resilience/ · net/ · extractors/ · observability/
  api/app.py                     FastAPI
```

## 5. Modelo de dados

| Tabela | Conteúdo | Unicidade |
|---|---|---|
| `cases` | id, name, description, status (OPEN/RUNNING/COMPLETED/FAILED/ARCHIVED), metadata | — |
| `investigations` | cada execução: modo, max_depth, pedido, resumo | — |
| `case_inputs` | **seeds**: input bruto, tipo, valor normalizado, `source_type=USER_INPUT`, hipóteses de detecção | — |
| `entities` | type, canonical_value, display_value, origin, confidence, depth, metadata (atributos append-only) | (case, fingerprint) |
| `evidence` | provider, source_type, source_name/url, query, raw_data sanitizado, observed_at ≠ collected_at, temporality, content_hash | (case, fingerprint) |
| `relationships` + `relationship_evidence` | relação com motivo; N:N com evidências (≥ 1 obrigatória) | (case, origem, tipo, destino) |
| `search_executions` | provider, query, status (SUCCESS/EMPTY/FAILED/…), error_code, cache_hit, duration_ms, depth, reason | — |
| `pivots` | origem, alvo, depth, motivo, provider, confiança, prioridade, status | — |
| `correlations` | par, score, nível, sinais positivos/negativos, evidence_ids | (case, par) |
| `conflicts` | entidade, atributo, observações com evidência (CONFLICTING_EVIDENCE) | (case, entidade, atributo) |
| `audit_log` | timestamp, case, evento, componente, mensagem e metadata sanitizadas | — |

**Deduplicação em dois níveis**
- Entidade: `sha256(TIPO:valor_canônico)`. O mesmo email achado por três fontes é **uma** entidade com três evidências.
- Evidência: `sha256(entidade|URL canônica)`, ou `sha256(entidade|hash do conteúdo)` quando não há URL. A mesma página vista por vários buscadores vira uma evidência com `sightings`, e fontes diferentes geram evidências diferentes.

## 6. Decisões de design

| Decisão | Motivo |
|---|---|
| SQLAlchemy síncrono + SQLite/PostgreSQL | sem infraestrutura obrigatória em dev; PostgreSQL por `DATABASE_URL`; escrita por rodada é curta |
| `InvestigationService.start()` separado de `run()` | a API agenda e responde `RUNNING`; trocar BackgroundTasks por fila (RQ/Celery) não muda contratos |
| `ProviderResult.source_entity` + `relation_direction` | permite relações como NETWORK→REGISTERED_TO→ORG vindas de uma consulta de IP, sem lógica de provider no Core |
| Atributos append-only com evidência | contradições nunca são sobrescritas: viram `ConflictRecord` |
| IP→ASN via Team Cymru (DNS) | fonte pública, rápida e sem HTTP; complementa o RDAP |
| Username do Telegram ligado ao ID por `USES_USERNAME` datado | o ID é estável e o username é mutável; nunca tratar username como identidade |
| SAME_AS exige sinal forte | username, nome ou local sozinhos chegam no máximo a POSSIBLY_SAME_AS |
| Orçamentos terminam com `BUDGET_EXHAUSTED` | controle, não erro; registrado no audit e no resumo |

## 7. Gap analysis (itens do prompt mestre)

| # | Item | Estado | Observação |
|---|---|---|---|
| 3–4 | Detecção e normalização | IMPLEMENTADO | domínios via PSL oficial |
| 5 | SourceOrchestrator | IMPLEMENTADO | seleção por tipo, tier e credenciais; orçamentos; concorrência global |
| 6–7, 17 | Contrato de providers | IMPLEMENTADO | `ProviderItem` com relações, atributos e `source_type` |
| 8 | SearchManager | PARCIAL | Brave e Google CSE (exigem chave) |
| 9–10 | Query planner / expansion | IMPLEMENTADO | expansão via PivotEngine (penalidade de prioridade por profundidade) |
| 11 | Deep Sweep | PARCIAL | modo configurado; sem Tor |
| 12–14 | Paralelismo, rate limit, cache | IMPLEMENTADO | cache em memória; Redis pendente |
| 15 | Telegram | PARCIAL | resolve username/ID e busca pública; coleta de mensagens pendente |
| 16–17 | Social / GitHub | PARCIAL | parsing de perfis; providers pendentes |
| 18–19 | Brasil / documentos | PARCIAL | consultas geradas; providers gov.br pendentes |
| 20 | Archive | IMPLEMENTADO | Wayback CDX, sempre `HISTORICAL_DATA`; Common Crawl pendente |
| 21–22 | Infraestrutura / subdomínios | IMPLEMENTADO | DNS, RDAP, CT, Team Cymru; VirusTotal/Shodan pendentes |
| 23 | Tor | AUSENTE | NOT_IMPLEMENTED |
| 24–25 | Imagens | AUSENTE | NOT_IMPLEMENTED |
| 26 | Credilink | AUSENTE | NOT_IMPLEMENTED (depende de WebService contratado) |
| 27 | Extractors | PARCIAL | Name/Location na camada de IA |
| 28, 24–27 (fase) | PivotEngine | IMPLEMENTADO | prioridade, visited/scheduled, depth, orçamentos |
| 29–30 | Entidades / relações | IMPLEMENTADO | persistidas com evidência |
| 31–32 | Correlation + score explicável | IMPLEMENTADO (inicial) | sinais por vizinhança e atributos; avatar hash pendente |
| 33–34 | Evidence / raw evidence | IMPLEMENTADO | raw sanitizado no banco; arquivos/screenshots pendentes |
| 35 | Timeline | PARCIAL | `observed_at`/`collected_at` e temporalidade persistidos; engine pendente |
| 36–37 | Graph / path finder | PARCIAL | `investigative_path` na API; grafo visual pendente |
| 38 | Case | IMPLEMENTADO | notas manuais e exports pendentes |
| 39 | Modos | IMPLEMENTADO | QUICK/DEEP/INVESTIGATION/DEEP_SWEEP com orçamentos |
| 40–42 | UI | AUSENTE | API e CLI apenas (decisão desta fase) |
| 43–44 | Resumo com IA | AUSENTE | — |
| 45–47 | Falsos positivos / contradições / duplicatas | IMPLEMENTADO | — |
| 48 | Database | IMPLEMENTADO | SQLite dev, PostgreSQL prod, Alembic |
| 49 | Audit log | IMPLEMENTADO | sanitizado |
| 50 | Segurança | PARCIAL | SSRF, secrets, token da API; upload/MIME pendentes |
| 51–52 | Integrações / healthcheck | IMPLEMENTADO | `/providers`, `/providers/health` |
| 53 | Configuração | IMPLEMENTADO | — |
| 57 | Export | AUSENTE | JSON via API |
| 58 | Histórico de buscas | IMPLEMENTADO | `search_executions` |
| 62 | Observabilidade | PARCIAL | logs JSON com case_id; métricas em memória |
| 63 | Testes | IMPLEMENTADO | 297 testes, rede sempre simulada |
| 68–70 | RAW / multi-input / seeds | IMPLEMENTADO | — |
| 76 | "Como chegamos aqui?" | IMPLEMENTADO | `GET /cases/{id}/entities/{entity_id}` |
| 78 | Controle humano | PARCIAL | bloqueio de valores/providers, limites e cancelamento; pausa via API pendente |

## 8. Riscos e limitações conhecidas

- **Validação ao vivo parcial.** DNS e Team Cymru foram validados contra a internet real. RDAP, crt.sh e
  Wayback só foram testados com respostas no formato real das APIs, porque a rede do ambiente de
  desenvolvimento bloqueia esses hosts. A primeira execução com rede aberta deve ser acompanhada
  (`osintizada providers --health`).
- **Execução em processo.** A investigação da API roda no próprio processo (BackgroundTasks). Se o processo
  reiniciar, o Case fica em `RUNNING`. Falta um worker persistente e a recuperação de jobs.
- **Cache em memória por processo.** A interface `CacheBackend` está pronta para Redis.
- **DNS rebinding.** A validação SSRF resolve o host antes da conexão. Pinning do IP validado está pendente.
- **Correlação inicial.** Ela usa vizinhança no grafo e atributos. Não há comparação de avatar nem de
  estilo de escrita.
- **`display_name` de Telegram.** Mudanças legítimas ao longo do tempo aparecem como conflito. Isso é
  intencional (registro histórico).

## 9. Roadmap

| Prioridade | Etapa | Entregas |
|---|---|---|
| ✅ | Fases 1, 2a–2c, 3 | ver histórico |
| **P0** | Worker de jobs + recuperação | fila (RQ/Redis), retomada de Case `RUNNING`, cancelamento via API |
| **P0** | Cache Redis + métricas | `CacheBackend` Redis, contadores por provider/case, endpoint `/metrics` |
| **P1** | Timeline + export | Timeline Engine (`observed_at`), export JSON/CSV/GraphML/HTML |
| **P1** | GitHub + Telegram (mensagens) | perfis, commits e emails públicos; busca em canais públicos |
| **P1** | Brasil OSINT | CNPJ (fonte autorizada), PNCP, Portal da Transparência, DOU |
| **P2** | Infra paga | VirusTotal, Shodan, Censys, SecurityTrails (NOT_CONFIGURED sem chave) |
| **P2** | UI | Next.js: execução ao vivo (SSE), abas de resultado, grafo |
| **P3** | Imagens, Credilink, Tor worker | conforme prompt mestre |

## 10. Próxima etapa recomendada

**Worker persistente de jobs + cache Redis.** Com o ciclo investigativo real funcionando, o maior risco
operacional é perder uma investigação longa (DEEP_SWEEP) quando o processo reinicia, e repetir consultas
externas entre processos. Os contratos (`start()`/`run()`, `CacheBackend`) já estão preparados para isso.
