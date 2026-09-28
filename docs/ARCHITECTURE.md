# Arquitetura do OSINTIZADA

## 1. Histórico

| Versão | Fase | Entrega principal |
|---|---|---|
| 0.1.0 | 1 — Core | identificadores, normalização, modelos, planner, evidência, contrato de providers |
| 0.2.0 | 2a–2c | resiliência (rate limit, retry, breaker, cache), SSRF, extractors, search engines |
| 0.3.0 | 3 — Ciclo investigativo real | PSL, persistência, Case, providers de infraestrutura, Pivot/Correlation, API |
| 0.4.0 | 4 — Resiliência, jobs, segurança | jobs persistentes + worker + recuperação, Redis, SSRF com pinning, avatar, timeline, exports |

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

## 3.1 Execução por jobs (v0.4)

```
API ── POST /investigate ─► JobService ─► [DB] Job PENDING (commit) ─► RQ.enqueue(job_id) ─► [DB] QUEUED
 │                                              ▲ publicação falhou? continua PENDING (outbox)
 │  202 {case_id, job_id, status}               │
 │                                   JobRecoveryService (API e worker na subida + a cada N s, com lock)
 │                                     · RUNNING sem heartbeat → INTERRUPTED → RETRYING | FAILED (dead letter)
 │                                     · PENDING antigo / RETRYING vencido → publica
 │                                     · QUEUED sem mensagem na fila → republica
 ▼
Worker (N processos) ─► claim atômico no banco (execution_token) ─► lock do Case (Redis, TTL+token)
   ─► JobAttempt ─► heartbeat (banco + lock + Redis) ─► InvestigationService.run(control)
        · checkpoint por rodada (condicionado ao token) · cancelamento cooperativo
        · consultas já concluídas no Case → ALREADY_EXECUTED (reaproveita entidades como pivôs)
   ─► finish COMPLETED | RETRYING (backoff × 2^(n-1)) | FAILED   (tudo condicionado ao token)
```

**Escolha da fila: RQ.** Comparação feita para este projeto:

| | RQ | Dramatiq | Celery |
|---|---|---|---|
| Dependências | só Redis | Redis/RabbitMQ | broker + backend, muitas opções |
| Serialização | `JSONSerializer` nativo | JSON | JSON/pickle (config) |
| Superfície | pequena, fácil de auditar | média | grande |
| Worker síncrono em processo | `SimpleWorker` (sem fork) | threads | prefork |
| Garantia de entrega | at-most-once por padrão | ack após execução | ack configurável |

O estado de verdade fica no banco (Job, tentativas, checkpoints). A fila só transporta `job_id`, então a
garantia que falta ao RQ (redelivery se o worker morrer) é coberta pelo reconciliador, que olha o banco,
não a fila. Por isso a entrega é *at-least-once* com consumidores idempotentes. Com Celery ou Dramatiq o
reconciliador continuaria necessário: nenhum dos dois sabe o que é um checkpoint de investigação. O RQ foi
escolhido pela menor superfície com a mesma garantia final. A interface `JobQueue` isola a escolha.

**Idempotência e posse.** O claim é `UPDATE jobs SET status=RUNNING, execution_token=:novo … WHERE id=:id
AND status IN (QUEUED, RETRYING, PENDING) AND NOT cancel_requested`, e só quem afeta 1 linha executa. Toda
escrita posterior (heartbeat, checkpoint, finish) exige o mesmo token. Se o reconciliador interromper um
worker lento, o token muda e o resultado tardio é descartado. Entidades e evidências já são deduplicadas por
fingerprint, então repetir uma rodada não duplica dados.

**Lock do Case.** `SET key token NX PX ttl`; renovação e liberação por script Lua que compara o token. Um
worker nunca libera lock alheio. O TTL padrão é `stale_after_seconds`: se um worker morre, seu lock expira no
mesmo prazo em que o job é considerado abandonado. Se o lock estiver ocupado, o job volta para `RETRYING`
sem consumir tentativa (`release_claim`).

**Checkpoint e retomada.** Após cada rodada (`INITIAL_PROVIDERS_COMPLETED`, `PIVOT_DEPTH_n_COMPLETED`,
`CORRELATION_COMPLETED`) são salvos a profundidade, a fronteira de pivôs, os conjuntos visitado/agendado,
contadores e orçamentos. A nova tentativa reconstrói o estado e continua da fronteira. Consultas que já
tinham terminado (inclusive as feitas antes da queda, sem checkpoint) são reconhecidas por
`search_executions` e não voltam a ser feitas.

**Status do Case** é derivado dos Jobs de investigação: ativo → `RUNNING`; último `COMPLETED`/`FAILED` → idem;
cancelado/interrompido → `OPEN`.

## 3.2 Redis: o que guarda (e o que não guarda)

| Uso | Chave | Perda do Redis |
|---|---|---|
| Fila RQ | `rq:queue:osintizada` | reconciliador republica pelo banco |
| Cache de providers | `osintizada:provider:<p>:<parser>:<sha256>` (JSON, TTL por provider) | só custo de nova consulta |
| Lock do Case / reconciliador | `osintizada:lock:case:<id>`, `osintizada:lock:reconciler` | expira; claim no banco impede execução dupla |
| Heartbeat de worker | `osintizada:worker:<id>` (TTL 3× intervalo) | `/health` mostra `OFFLINE` até o próximo heartbeat |
| Progresso ao vivo / cancelamento | `osintizada:job:<id>:progress`, `:cancel` | banco tem progresso e `cancel_requested` |
| Métricas agregadas | `osintizada:metrics` (hash) | contadores recomeçam |

Nenhuma evidência, entidade ou estado de job existe só no Redis. Não há pickle: o cache e a fila usam JSON.
Com o Redis fora, a investigação continua sem cache, novos jobs ficam `PENDING` e a API avisa
`QUEUE_UNAVAILABLE`. O Core (`investigation/`, `core/`) não importa Redis: `infrastructure/` implementa as
interfaces `CacheBackend`, `JobQueue` e `ExecutionControl`.

## 3.3 SSRF e DNS rebinding

`resolve_and_validate()` resolve o host **uma vez** e valida **todos** os endereços (se um for proibido, a
URL é recusada inteira). A conexão é feita no IP validado (`pinned_ip`), com `Host` e SNI do nome original,
então uma segunda resolução maliciosa não tem efeito. Cada redirect passa pela mesma validação. Bloqueios:
todo IP não global da stdlib (privado, loopback, link-local, CGNAT, multicast, reservado), metadata cloud
(`169.254.169.254`, `fd00:ec2::254`, `100.100.100.200`), IPv4 embutido em IPv6 (mapped, 6to4, Teredo,
NAT64), redes extras configuráveis, portas fora de `allowed_ports`, credenciais na URL e esquemas que não sejam
http/https. Com proxy HTTP o destino ainda é o IP validado (CONNECT ao IP). Com proxy que resolve DNS
(`socks5h`), `proxy_policy: deny` recusa URLs não confiáveis. Hosts fixos de providers (RDAP/RIRs, crt.sh,
Wayback) são `trusted_hosts`.

## 3.4 Imagens e correlação visual

`analyze_image()` aplica limite de bytes e pixels (proteção contra decompression bomb), formatos permitidos,
rotação EXIF e achatamento de alfa. Depois calcula SHA256, pHash (DCT 32×32 → bloco 8×8) e dHash.
`EXACT_IMAGE_MATCH` exige mesmo SHA256. `PERCEPTUAL_VERY_SIMILAR` exige pHash ≤ 6 e dHash ≤ 12.
`PERCEPTUAL_SIMILAR` exige pHash ≤ 12. Imagens `LOW_IDENTITY_VALUE` (marcadas, em lista de hashes conhecidos
ou usadas por ≥ 3 contas no Case) contam 25% do peso. Avatar nunca é sinal forte: sozinho não chega a
`SAME_AS`. Os bytes ficam em `ArtifactStore` (endereçado por hash); no banco ficam hash e metadados.

## 3.5 Timeline e exports

A timeline usa o momento do fato (`observed_at`: emissão de certificado, captura do Wayback, publicação,
observação de conta ou avatar). O horário da coleta só aparece com `include_collection_only=true`. Regras por
tipo são registráveis (`register_event_rule`). Exports (JSON, CSV em zip, HTML autocontido) rodam como job
e incluem Case, entidades, evidências, relações, correlações, conflitos, timeline e auditoria. O CSV
neutraliza fórmulas (`=`, `+`, `-`, `@`).

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
    tables.py                    schema SQLAlchemy (15 tabelas)
    session.py                   Database (SQLite dev com WAL / PostgreSQL prod), upgrade Alembic
    migrations/                  Alembic (0001_initial, 0002_jobs)
  repositories/                  Case, Investigation, Entity, Evidence, Relationship, Search, Audit,
                                 Pivot, Correlation, Conflict, Job (única camada que toca o banco)
  investigation/
    service.py                   InvestigationService: ciclo multi-profundidade persistente, checkpoints
    control.py                   ExecutionControl (interface: cancelamento, progresso, checkpoint)
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
  jobs/
    service.py                   JobService: submissão (outbox), cancel, retry, describe, status do Case
    worker.py                    JobRunner (claim, lock, tentativa, handlers) + run_worker (RQ SimpleWorker)
    recovery.py                  JobRecoveryService (reconciliador)
    control.py / heartbeat.py    JobExecutionControl, heartbeat de job e de worker
  infrastructure/                redis_client, redis_cache (RedisCacheBackend), locks, queue (RQJobQueue)
  images/                        hashing (SHA256/pHash/dHash), store (ArtifactStore), fetch, results
  timeline/ · exports/           TimelineService, ExportService (JSON/CSV/HTML)
  bootstrap.py                   validação de subida (banco, migrações, Redis)
  resilience/ · net/ (SSRF com pinning) · extractors/ · observability/ (logs, métricas)
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
| `jobs` | case, job_type (INVESTIGATION/EXPORT), status, attempt/max_attempts, worker_id, execution_token, heartbeat_at, next_attempt_at, cancel_requested, stage, progress, checkpoint, request, result, erro, retry_of | — |
| `job_attempts` | cada tentativa: worker, token, status final (COMPLETED/FAILED/INTERRUPTED/…), erro, início/fim | (job, attempt) |
| `job_events` | eventos ordenados por id (fonte do SSE): JOB_*, PROVIDER_STARTED/FINISHED, ENTITY_CREATED, PIVOT_CREATED | — |

**Deduplicação em dois níveis**
- Entidade: `sha256(TIPO:valor_canônico)`. O mesmo email achado por três fontes é **uma** entidade com três evidências.
- Evidência: `sha256(entidade|URL canônica)`, ou `sha256(entidade|hash do conteúdo)` quando não há URL. A mesma página vista por vários buscadores vira uma evidência com `sightings`, e fontes diferentes geram evidências diferentes.

## 6. Decisões de design

| Decisão | Motivo |
|---|---|
| SQLAlchemy síncrono + SQLite/PostgreSQL | sem infraestrutura obrigatória em dev; PostgreSQL por `DATABASE_URL`; escrita por rodada é curta |
| `InvestigationService.start()` separado de `run()` | a API cria o Job; o worker chama `run()` com um `ExecutionControl` (cancelamento, progresso, checkpoint) |
| Banco como fonte da verdade, Redis efêmero | perder o Redis nunca perde evidência nem estado de job |
| Claim/heartbeat/finish condicionados ao token | entrega duplicada e worker "zumbi" não corrompem resultado |
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
| 12–14 | Paralelismo, rate limit, cache | IMPLEMENTADO | cache em memória ou Redis compartilhado |
| 15 | Telegram | PARCIAL | resolve username/ID e busca pública; coleta de mensagens pendente |
| 16–17 | Social / GitHub | PARCIAL | parsing de perfis; providers pendentes |
| 18–19 | Brasil / documentos | PARCIAL | consultas geradas; providers gov.br pendentes |
| 20 | Archive | IMPLEMENTADO | Wayback CDX, sempre `HISTORICAL_DATA`; Common Crawl pendente |
| 21–22 | Infraestrutura / subdomínios | IMPLEMENTADO | DNS, RDAP, CT, Team Cymru; VirusTotal/Shodan pendentes |
| 23 | Tor | AUSENTE | NOT_IMPLEMENTED |
| 24–25 | Imagens | PARCIAL | SHA256/pHash/dHash, avatar Telegram; busca reversa pendente |
| 26 | Credilink | AUSENTE | NOT_IMPLEMENTED (depende de WebService contratado) |
| 27 | Extractors | PARCIAL | Name/Location na camada de IA |
| 28, 24–27 (fase) | PivotEngine | IMPLEMENTADO | prioridade, visited/scheduled, depth, orçamentos |
| 29–30 | Entidades / relações | IMPLEMENTADO | persistidas com evidência |
| 31–32 | Correlation + score explicável | IMPLEMENTADO | vizinhança, atributos e avatar (EXACT/PERCEPTUAL, LOW_IDENTITY_VALUE) |
| 33–34 | Evidence / raw evidence | IMPLEMENTADO | raw sanitizado no banco; arquivos/screenshots pendentes |
| 35 | Timeline | IMPLEMENTADO | momento do fato, regras por tipo, filtros |
| 36–37 | Graph / path finder | PARCIAL | `investigative_path` na API; grafo visual pendente |
| 38 | Case | IMPLEMENTADO | notas manuais e exports pendentes |
| 39 | Modos | IMPLEMENTADO | QUICK/DEEP/INVESTIGATION/DEEP_SWEEP com orçamentos |
| 40–42 | UI | AUSENTE | API e CLI apenas (decisão desta fase) |
| 43–44 | Resumo com IA | AUSENTE | — |
| 45–47 | Falsos positivos / contradições / duplicatas | IMPLEMENTADO | — |
| 48 | Database | IMPLEMENTADO | SQLite dev, PostgreSQL prod, Alembic |
| 49 | Audit log | IMPLEMENTADO | sanitizado |
| 50 | Segurança | IMPLEMENTADO | SSRF com pinning, secrets, token da API, limites de imagem |
| 51–52 | Integrações / healthcheck | IMPLEMENTADO | `/providers`, `/providers/health` |
| 53 | Configuração | IMPLEMENTADO | — |
| 57 | Export | IMPLEMENTADO | JSON/CSV/HTML como job; GraphML pendente |
| 58 | Histórico de buscas | IMPLEMENTADO | `search_executions` |
| 62 | Observabilidade | IMPLEMENTADO | logs com case_id/job_id/provider, `/metrics`, `/health`, SSE |
| 63 | Testes | IMPLEMENTADO | 400 testes, rede e Redis simulados; jobs também em PostgreSQL |
| 68–70 | RAW / multi-input / seeds | IMPLEMENTADO | — |
| 76 | "Como chegamos aqui?" | IMPLEMENTADO | `GET /cases/{id}/entities/{entity_id}` |
| 78 | Controle humano | IMPLEMENTADO | bloqueio de valores/providers, limites, cancelamento e retry via API |

## 8. Riscos e limitações conhecidas

- **Validação ao vivo parcial.** DNS e Team Cymru foram validados contra a internet real. RDAP, crt.sh e
  Wayback só foram testados com respostas no formato real das APIs, porque a rede do ambiente de
  desenvolvimento bloqueia esses hosts.
- **Durabilidade da fila.** Sem AOF, reiniciar o Redis perde as mensagens. O banco continua com os Jobs e o
  reconciliador republica `QUEUED` antigos (padrão: 300 s). O compose liga `appendonly yes`.
- **RQ não reentrega sozinho.** Se o worker morrer, a recuperação depende do reconciliador (heartbeat
  expirado em `stale_after_seconds`, padrão 90 s). Uma rodada interrompida é refeita, mas as consultas já
  concluídas nela não se repetem.
- **Lock expira se o heartbeat parar.** Um worker congelado (sem heartbeat) por mais que o TTL perde o lock.
  Nesse caso o token no banco também já foi trocado e suas escritas são rejeitadas.
- **Cancelamento cooperativo.** Ele é verificado entre consultas e rodadas. Uma consulta em andamento termina
  antes (limitada pelo timeout do provider).
- **Imagens.** pHash/dHash detectam recompressão, redimensionamento e pequenas edições, mas não recortes
  grandes, espelhamento ou rotação arbitrária. Só o avatar do Telegram é coletado automaticamente.
- **Docker.** O `docker-compose.yml` foi validado com `docker compose config`. A imagem não foi construída
  neste ambiente, porque não há daemon Docker disponível.
- **`display_name` de Telegram.** Mudanças legítimas ao longo do tempo aparecem como conflito. Isso é
  intencional (registro histórico).

## 9. Roadmap

| Prioridade | Etapa | Entregas |
|---|---|---|
| ✅ | Fases 1–4 | ver histórico |
| **P1** | GitHub + Telegram (mensagens) | perfis, commits e emails públicos; busca em canais públicos |
| **P1** | Brasil OSINT | CNPJ (fonte autorizada), PNCP, Portal da Transparência, DOU |
| **P1** | UI | Next.js consumindo SSE, abas de resultado, grafo, timeline |
| **P2** | Infra paga | VirusTotal, Shodan, Censys, SecurityTrails (NOT_CONFIGURED sem chave) |
| **P2** | Imagens | busca reversa autorizada, avatares de outras plataformas |
| **P3** | Credilink, Tor worker | conforme prompt mestre |

## 10. Próxima etapa recomendada

**UI de acompanhamento + providers sociais (GitHub/Telegram público).** A execução agora é durável e
observável (SSE, `/health`, `/metrics`). O próximo ganho vem de ampliar as fontes com identidade forte e dar
ao investigador uma tela para revisar correlações e marcar falsos positivos.
