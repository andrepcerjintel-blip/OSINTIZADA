# Backlog do RINO

Itens futuros. O que já está implementado fica no [CHANGELOG](CHANGELOG.md).

## Identidade visual

- **Interface web ampliada**: a tela de pesquisa em `GET /` já inicia investigações e mostra resultados;
  faltam detalhe por entidade (evidências e "como chegamos aqui?"), grafo, timeline e correlações na tela.
- **Tema escuro completo no relatório HTML** (hoje só o cabeçalho usa a paleta grafite/azul elétrico).
- **Versão vetorial (SVG) da logo**, se o autor da arte fornecer; hoje só existe a PNG oficial.
- **Retirada dos nomes legados** (`osintizada` no pacote, prefixo Redis, métricas), com migração coordenada
  de chaves, dashboards e jobs enfileirados. Ver docs/ARCHITECTURE.md §11.

## IA

- **Validação ao vivo com um Llama real** (Ollama + modelo local) e ajuste de prompts por modelo.
- **Tool calling com allowlist** (fase futura): o modelo sugeriria ações de uma lista fechada, sempre
  validadas pelo Core, pelo QueryPlanner e pelos orçamentos. Nunca rede, shell ou banco livres.
- Tela de revisão em massa das sugestões de IA (aceitar/rejeitar em lote).

