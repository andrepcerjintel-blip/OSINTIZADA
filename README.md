<p align="center">
  <img src="assets/logo-rino.png" alt="RINO" width="280">
</p>

# RINO — Plataforma de Investigação OSINT

Plataforma privada, modular e extensível de investigação OSINT: orquestra fontes, pivôs, evidências e
correlações de uma investigação de ponta a ponta.

A partir de um ou mais identificadores, o RINO abre um **Case**, registra os inputs como **seeds**,
consulta fontes reais, transforma cada resultado em **evidência**, consolida **entidades** sem duplicatas,
gera **pivôs** até a profundidade configurada, **correlaciona** entidades com score explicável e persiste
tudo com auditoria.

> **Princípio:** toda conclusão deve ser rastreável até a evidência que a originou.
> **Código produz evidência. IA interpreta evidência.**

## Principais capacidades

- **Investigação persistente por Case**: seeds, entidades sem duplicatas, evidências com proveniência,
  relações, pivôs com profundidade e orçamento, correlação explicável e contradições registradas.
- **Execução resiliente**: a API só enfileira; workers separados executam com heartbeat, lock por Case,
  checkpoints, retomada após queda, cancelamento e dead letter.
- **Fontes reais**: DNS, RDAP, Certificate Transparency, Wayback (sem chave); Brave/Google e Telegram com
  credenciais (senão `NOT_CONFIGURED`).
- **Segurança**: SSRF com IP pinning (anti DNS rebinding), secrets nunca expostos, token na API.
- **Análise**: correlação visual de avatares (SHA256/pHash/dHash), timeline pelo momento do fato, exports
  JSON/CSV/HTML.

## Uso rápido: pesquisa pelo navegador (inclusive Windows, sem Redis)

```bash
rino serve
```

Abra **http://127.0.0.1:8000**, digite os alvos (domínio, IP, e-mail, usuário…), escolha o modo e clique
em **Investigar**. A pesquisa roda no próprio servidor: o progresso aparece ao vivo, e depois dá para
filtrar as entidades e gerar o relatório HTML, JSON ou CSV. Sem `REDIS_URL`, o servidor usa o **executor
embutido** (fila no banco). Se o servidor for encerrado no meio de uma pesquisa, ela é retomada do último
checkpoint quando ele voltar. Para vários workers em paralelo, use Redis + `rino worker` (abaixo).

## IA local (Llama) — opcional

O RINO pode usar um **Llama local** (via Ollama) para resumir, triar, extrair, traduzir e sugerir consultas
sobre as evidências. A IA **interpreta** evidências e não as produz. Tudo o que ela sugere fica marcado
(DERIVED / AI_SUGGESTED) para revisão, e nada é executado sozinho. O padrão é `LOCAL_ONLY`: nada sai da máquina.

1. Instale o Ollama por conta própria (https://ollama.com) e deixe-o rodando.
2. Baixe um modelo que caiba na sua máquina: `ollama pull <modelo>` (com pouca VRAM, use um menor ou quantizado).
3. `RINO_AI_LLAMA_ENABLED=true`, `RINO_AI_LLAMA_BASE_URL=http://127.0.0.1:11434`, `RINO_AI_LLAMA_MODEL=<modelo>`.
4. `rino doctor` deve mostrar `LLAMA READY`.
5. Na tela, use os botões de "Análise por IA" nos resultados de um Case.

O RINO nunca baixa modelos sozinho. Claude e OpenAI são opcionais (modo `HYBRID` ou `CLOUD`). Guia completo,
modos, privacidade e solução de problemas: [docs/AI.md](docs/AI.md).

## Estado atual (v0.7.0)

| Componente | Estado |
|---|---|
| Identifier Engine, normalização, Public Suffix List oficial | ✅ |
| Case, seeds, entidades, evidências, relações, buscas, audit log, pivôs, correlações, conflitos (SQLite/PostgreSQL + Alembic) | ✅ |
| **Jobs persistentes**: fila RQ/Redis, worker separado, heartbeat, lock por Case, checkpoints, retomada, cancelamento, dead letter | ✅ |
| Redis como coordenação efêmera: fila, locks, cache compartilhado, heartbeats, progresso, métricas (o banco é a fonte da verdade) | ✅ |
| Resiliência: rate limit (antes da requisição), retry/backoff, circuit breaker, cache, concorrência global e por provider | ✅ |
| SSRF com IP pinning (anti DNS rebinding), revalidação de redirect, política de portas e proxy, `.onion` só via Tor | ✅ |
| Rate limit por provider compartilhado entre processos (Redis) + cooldown de 429 compartilhado | ✅ |
| **DNS**, **RDAP**, **Certificate Transparency**, **Wayback** | ✅ sem chave |
| Search: Brave, Google Programmable Search | ✅ exigem chave (senão `NOT_CONFIGURED` + variáveis faltantes) |
| Telegram (Telethon, sessão legítima; avatar com hash) | ✅ exige credenciais (senão `NOT_CONFIGURED`) |
| Pivot Engine, Correlation Engine (inclui avatar SHA256/pHash/dHash) + contradições | ✅ |
| Timeline (momento do fato) e exports JSON/CSV/HTML | ✅ |
| API FastAPI (SSE, `/health`, `/metrics`) + CLI + logs JSON com `case_id`/`job_id` | ✅ |
| Tela de pesquisa no navegador + executor embutido (sem Redis) | ✅ |
| IA auxiliar: Llama local (Ollama), Claude, OpenAI; AIRouter com LOCAL_ONLY/HYBRID/CLOUD, PrivacyGate, cache | ✅ (Llama real: não validado ao vivo) |
| UI, Tor, Credilink, GitHub, Brasil OSINT | ⏳ ver [roadmap](docs/ARCHITECTURE.md#9-roadmap) |

Nenhum resultado é simulado. Provider sem credencial aparece como `NOT_CONFIGURED`, falha aparece como
`FAILED`/`TIMEOUT`/`RATE_LIMITED` com código, e ausência de resultado aparece como `EMPTY`. Sem Redis a API
não finge ter fila: o Job fica `PENDING` com o aviso `QUEUE_UNAVAILABLE`; sem worker, `WORKER_UNAVAILABLE`.

## Instalação

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                 # + ".[telegram]" e/ou ".[postgres]" se for usar
cp .env.example .env                    # opcional; .env nunca vai para o Git
rino db upgrade                         # cria/atualiza o banco (SQLite em data/ por padrão)
```

### Docker (api + worker + redis + postgres)

```bash
cp .env.example .env    # defina POSTGRES_PASSWORD, REDIS_PASSWORD e RINO_API_TOKEN (o compose exige)
docker compose up -d --build
docker compose up -d --scale worker=3
```

## Investigação (CLI)

```bash
# Case novo, ciclo completo: seed → providers → evidência → entidades → pivôs → correlação
rino investigate example.com --mode deep
rino investigate contato@empresa.com.br @usuario --mode quick --block gmail.com
rino investigate 8.8.8.8 --max-depth 1 --json

rino cases                              # lista Cases
rino case <case_id>                     # entidades, relações e buscas de um Case
rino providers --health                 # status das integrações (pode consumir quota)
```

Ferramentas auxiliares (sem persistência): `detect`, `plan`, `raw`, `run`, `search`, `extract`.

## API e worker

```bash
export REDIS_URL=redis://127.0.0.1:6379/0
rino serve                              # http://127.0.0.1:8000 (tela inicial) e /docs (só cria e enfileira jobs)
rino worker                             # executa os jobs; rode quantos quiser
# fora de localhost é obrigatório: RINO_API_TOKEN=... rino serve --host 0.0.0.0
```

```bash
curl -X POST localhost:8000/cases -H 'content-type: application/json' -d '{"name": "Caso 1"}'
curl -X POST localhost:8000/cases/<id>/investigate -H 'content-type: application/json' \
     -d '{"inputs": ["example.com"], "mode": "deep", "max_depth": 2}'
# → 202 {"case_id": "...", "job_id": "...", "status": "QUEUED", "warnings": []}
curl -N localhost:8000/jobs/<job_id>/events   # progresso ao vivo (SSE)
```

| Rota | Conteúdo |
|---|---|
| `GET /` | tela de pesquisa RINO: nova investigação, progresso ao vivo, resultados, relatórios |
| `GET /docs`, `GET /redoc`, `GET /openapi.json` | documentação da **RINO API** |
| `GET /health`, `GET /metrics` | api, database, redis, worker (`ONLINE`/`STALE`/`OFFLINE`), fila; métricas Prometheus |
| `POST /cases`, `GET /cases`, `GET /cases/{id}` | Cases (com seeds, execuções e contagens) |
| `POST /cases/{id}/investigate` | cria e enfileira um Job de investigação |
| `GET /cases/{id}/jobs`, `GET /jobs/{id}` | status, estágio, progresso, tentativas, erro |
| `GET /jobs/{id}/events` | SSE (retoma com `Last-Event-ID`) |
| `POST /jobs/{id}/cancel`, `POST /jobs/{id}/retry` | cancelamento cooperativo; retry manual a partir do checkpoint |
| `POST /cases/{id}/exports`, `GET /jobs/{id}/download` | export JSON/CSV/HTML como job |
| `GET /cases/{id}/timeline` | eventos pelo momento do fato (`entity_type`, `provider`, `date_from`, `date_to`) |
| `GET /cases/{id}/entities[?type=]`, `/entities/{entity_id}` | entidades, evidências, relações e **"como chegamos aqui?"** |
| `POST /cases/{id}/entities/{entity_id}/flags` | marca imagem como `LOW_IDENTITY_VALUE` |
| `GET /cases/{id}/evidence`, `/relationships`, `/searches`, `/audit` | proveniência completa |
| `GET /cases/{id}/pivots`, `/correlations`, `/conflicts` | decisões de pivô, scores explicáveis, contradições |
| `GET /providers`, `/providers/health`, `POST /providers/{name}/validate-credentials` | integrações (variáveis faltantes, nunca valores) |

CLI: `rino doctor` (diagnóstico, inclusive IA e hardware), `rino ai status|run|task`, `rino worker [--burst]`, `rino worker-status [--local] [--require-online]`, `rino reconcile`.

## Variáveis de ambiente (principais)

| Variável | Uso |
|---|---|
| `DATABASE_URL` | banco (padrão: SQLite em `data/`; produção: PostgreSQL) |
| `REDIS_URL` (+ `REDIS_CACHE_URL` opcional) | fila, locks, cache compartilhado, heartbeats |
| `RINO_API_TOKEN` | exige `Authorization: Bearer` na API (obrigatório fora de localhost) |
| `RINO_CONFIG` | arquivo de configuração (padrão `config/rino.yaml`) |
| `RINO_AI_MODE`, `RINO_AI_LLAMA_*`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | IA auxiliar (opcional; [docs/AI.md](docs/AI.md)) |
| `RINO_EXECUTOR` | `auto` (padrão), `redis` (workers separados) ou `embedded` (o servidor executa) |
| `BRAVE_SEARCH_API_KEY`, `GOOGLE_CSE_API_KEY`/`GOOGLE_CSE_CX` | buscadores |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION` | Telegram |

Lista completa em [docs/CONFIGURATION.md](docs/CONFIGURATION.md). O prefixo legado `OSINTIZADA_` continua
aceito (o `RINO_` tem precedência).

## Arquitetura resumida

```
Cliente ─► RINO API (FastAPI) ─► JobService ─► Banco (fonte da verdade) + fila Redis
                                                      │
                                   Worker(s) ◄────────┘  heartbeat · lock por Case · checkpoints
                                      │
                         Investigation Engine ─► SourceOrchestrator ─► Providers (DNS, RDAP, CT, …)
                                      │
                      Evidência ─► Entidades ─► Pivôs ─► Correlação ─► Timeline / Exports
```

Detalhes em [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Testes

```bash
pytest          # 467 testes; rede e Redis simulados (fakeredis + RQ real), nenhuma chamada externa
# mesmos testes de jobs contra PostgreSQL real (banco descartável — o schema é recriado):
RINO_TEST_DATABASE_URL=postgresql+psycopg://user@host/banco_teste pytest tests/test_jobs.py
```

## Documentação

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): fluxo, jobs e recuperação, Redis, SSRF, modelo de dados, roadmap
- [docs/AI.md](docs/AI.md): IA auxiliar (Llama/Ollama, modos, privacidade, tarefas, troubleshooting)
- [docs/PROVIDERS.md](docs/PROVIDERS.md): contrato, providers existentes e como criar um novo
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md): modos, orçamentos, pivôs, correlação, banco e secrets
- [BACKLOG.md](BACKLOG.md): itens futuros
- [CHANGELOG.md](CHANGELOG.md)

## Identidade do projeto

O projeto se chama **RINO** (antes OSINTIZADA). A logo oficial fica em
[`assets/logo-rino.png`](assets/logo-rino.png); versões redimensionadas para a API e os relatórios ficam em
`osintizada/assets/`. A identidade visual usa grafite/preto com azul elétrico como destaque. A logo aparece
na tela inicial da API, na documentação Swagger/ReDoc e no cabeçalho do relatório HTML.

Por compatibilidade, alguns nomes internos continuam com o nome legado: o pacote Python `osintizada`, o
comando alias `osintizada`, o prefixo das chaves Redis e das métricas (`osintizada_*`), o nome da fila e o
banco padrão `data/osintizada.db`. Ver [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#11-identidade-rino-e-nomes-legados).

## Uso responsável

O RINO coleta apenas informação legitimamente acessível: fontes públicas, APIs autorizadas e
serviços contratados. Não implementa bypass de autenticação, exploração, quebra de senha ou evasão de
controles de acesso. Correlações são hipóteses com evidência. `SAME_AS` exige sinal forte, e homônimos
nunca são ligados só pelo nome.
