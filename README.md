# OSINTIZADA

Plataforma privada, modular e extensível de investigação OSINT: um **OSINT Investigation Orchestrator**.

A partir de um ou mais identificadores, o OSINTIZADA abre um **Case**, registra os inputs como **seeds**,
consulta fontes reais, transforma cada resultado em **evidência**, consolida **entidades** sem duplicatas,
gera **pivôs** até a profundidade configurada, **correlaciona** entidades com score explicável e persiste
tudo com auditoria.

> **Princípio:** toda conclusão deve ser rastreável até a evidência que a originou.
> **Código produz evidência. IA interpreta evidência.**

## Estado atual (v0.3.0)

| Componente | Estado |
|---|---|
| Identifier Engine, normalização, Public Suffix List oficial | ✅ |
| Case, seeds, entidades, evidências, relações, buscas, audit log, pivôs, correlações, conflitos (SQLite/PostgreSQL + Alembic) | ✅ |
| Resiliência: rate limit (antes da requisição), retry/backoff, circuit breaker, cache, concorrência global e por provider | ✅ |
| **DNS** (A/AAAA/MX/NS/TXT/CNAME/PTR + IP→ASN Team Cymru) | ✅ sem chave |
| **RDAP** (IP, ASN, domínio) | ✅ sem chave |
| **Certificate Transparency** (crt.sh) | ✅ sem chave |
| **Internet Archive / Wayback** (sempre dado histórico) | ✅ sem chave |
| Search: Brave, Google Programmable Search | ✅ exigem chave |
| Telegram (Telethon, sessão legítima) | ✅ exige credenciais (senão `NOT_CONFIGURED`) |
| Pivot Engine (prioridade, profundidade, orçamentos, anti-loop) | ✅ |
| Correlation Engine (determinístico, explicável) + contradições | ✅ inicial |
| API FastAPI + CLI + logs JSON estruturados | ✅ |
| UI, timeline, export, imagens, Tor, Credilink | ⏳ ver [roadmap](docs/ARCHITECTURE.md#9-roadmap) |

Nenhum resultado é simulado. Provider sem credencial aparece como `NOT_CONFIGURED`, falha aparece como
`FAILED`/`TIMEOUT`/`RATE_LIMITED` com código, e ausência de resultado aparece como `EMPTY`.

## Instalação

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                 # + ".[telegram]" e/ou ".[postgres]" se for usar
cp .env.example .env                    # opcional; .env nunca vai para o Git
osintizada db upgrade                   # cria/atualiza o banco (SQLite em data/ por padrão)
```

## Investigação (CLI)

```bash
# Case novo, ciclo completo: seed → providers → evidência → entidades → pivôs → correlação
osintizada investigate example.com --mode deep
osintizada investigate contato@empresa.com.br @usuario --mode quick --block gmail.com
osintizada investigate 8.8.8.8 --max-depth 1 --json

osintizada cases                        # lista Cases
osintizada case <case_id>               # entidades, relações e buscas de um Case
osintizada providers --health           # status das integrações (pode consumir quota)
```

Ferramentas auxiliares (sem persistência): `detect`, `plan`, `raw`, `run`, `search`, `extract`.

## API

```bash
osintizada serve                        # http://127.0.0.1:8000/docs
# fora de localhost é obrigatório: OSINTIZADA_API_TOKEN=... osintizada serve --host 0.0.0.0
```

```bash
curl -X POST localhost:8000/cases -H 'content-type: application/json' -d '{"name": "Caso 1"}'
curl -X POST localhost:8000/cases/<id>/investigate -H 'content-type: application/json' \
     -d '{"inputs": ["example.com"], "mode": "deep", "max_depth": 2}'
# → {"case_id": "...", "investigation_id": "...", "status": "RUNNING"}
```

| Rota | Conteúdo |
|---|---|
| `POST /cases`, `GET /cases`, `GET /cases/{id}` | Cases (com seeds, execuções e contagens) |
| `POST /cases/{id}/investigate` | inicia investigação (background) |
| `GET /cases/{id}/entities[?type=]` | entidades com nº de evidências |
| `GET /cases/{id}/entities/{entity_id}` | evidências, relações e **"como chegamos aqui?"** |
| `GET /cases/{id}/evidence`, `/relationships`, `/searches`, `/audit` | proveniência completa |
| `GET /cases/{id}/pivots`, `/correlations`, `/conflicts` | decisões de pivô, scores explicáveis, contradições |
| `GET /providers`, `GET /providers/health` | integrações (credenciais mascaradas), HEALTHY/DEGRADED/UNAVAILABLE/NOT_CONFIGURED |

## Testes

```bash
pytest          # 297 testes; providers externos testados com respostas simuladas (sem internet)
```

## Documentação

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): auditoria, fluxo, modelo de dados, gap analysis e roadmap
- [docs/PROVIDERS.md](docs/PROVIDERS.md): contrato, providers existentes e como criar um novo
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md): modos, orçamentos, pivôs, correlação, banco e secrets
- [CHANGELOG.md](CHANGELOG.md)

## Uso responsável

O OSINTIZADA coleta apenas informação legitimamente acessível: fontes públicas, APIs autorizadas e
serviços contratados. Não implementa bypass de autenticação, exploração, quebra de senha ou evasão de
controles de acesso. Correlações são hipóteses com evidência. `SAME_AS` exige sinal forte, e homônimos
nunca são ligados só pelo nome.
