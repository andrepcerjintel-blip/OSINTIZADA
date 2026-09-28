# OSINTIZADA

Plataforma privada, modular e extensível de investigação OSINT — um **OSINT Investigation Orchestrator**.

A partir de um ou mais identificadores, o OSINTIZADA detecta o tipo da entrada, normaliza, planeja consultas
justificadas, seleciona fontes relevantes, coleta em paralelo, registra evidências com proveniência completa e
(nas próximas fases) gera pivôs, correlaciona entidades e monta timeline e grafo.

> **Princípio:** toda conclusão deve ser rastreável até a evidência que a originou.
> **Código produz evidência. IA interpreta evidência.**

## Estado atual — v0.1.0 (Fase 1: Core)

| Componente | Estado |
|---|---|
| Identifier Engine (multi-hipótese) | ✅ |
| Normalização (telefone, CPF/CNPJ, email, domínio, URL, IP, ASN, cripto, nomes) | ✅ |
| Modelos: Entity / Relationship / Evidence / ProviderResponse | ✅ |
| Query Planner + QueryBuilder (operadores, prioridade, custo, motivo) | ✅ |
| Evidence Engine (hash, fingerprint, deduplicação) | ✅ |
| Interface de providers + registry + healthcheck | ✅ |
| SourceOrchestrator (depth 0, paralelo, orçamento, cancelamento) | ✅ |
| Providers externos (search engines, Telegram, GitHub, …) | ⏳ Fase 2+ (aparecem como `NOT CONFIGURED`) |
| Pivot / Correlation / Timeline / Graph / Persistência / API / UI | ⏳ ver [roadmap](docs/ARCHITECTURE.md#8-roadmap) |

Nenhum resultado é simulado: fontes não integradas simplesmente não existem ou aparecem como `NOT CONFIGURED`.

## Instalação

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # opcional; .env nunca vai para o Git
```

## Uso (CLI)

```bash
# Hipóteses de tipo + normalização
osintizada detect darkwolf88
osintizada detect "(61) 99999-9999"

# Consultas planejadas (com prioridade e motivo)
osintizada plan fearless1999 --mode quick
osintizada plan 52998224725 --type cpf --mode deep_sweep --json

# RAW SEARCH (registrada como consulta manual)
osintizada raw '"usuario123" "proton.me"'

# Executar providers disponíveis (Fase 1: apenas derivação local)
osintizada run fearless1999@gmail.com https://github.com/torvalds

# Status das integrações
osintizada providers
```

Também disponível como `python -m osintizada ...`.

## Testes

```bash
pytest
```

## Documentação

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — diagnóstico, arquitetura alvo, gap analysis e roadmap
- [docs/PROVIDERS.md](docs/PROVIDERS.md) — como criar e registrar um provider
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — configuração e secrets
- [CHANGELOG.md](CHANGELOG.md)

## Uso responsável

O OSINTIZADA coleta apenas informação legitimamente acessível (fontes públicas, APIs autorizadas e serviços
contratados). Não implementa bypass de autenticação, exploração, quebra de senha ou evasão de controles de acesso.
