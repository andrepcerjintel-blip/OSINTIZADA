# Backlog do RINO

Itens futuros. O que já está implementado fica no [CHANGELOG](CHANGELOG.md).

## Identidade visual

- **Interface web ampliada**: a tela de pesquisa em `GET /` já inicia investigações e mostra resultados;
  faltam detalhe por entidade (evidências e "como chegamos aqui?"), grafo, timeline e correlações na tela.
- **Tema escuro completo no relatório HTML** (hoje só o cabeçalho usa a paleta grafite/azul elétrico).
- **Versão vetorial (SVG) da logo**, se o autor da arte fornecer; hoje só existe a PNG oficial.
- **Retirada dos nomes legados** (`osintizada` no pacote, prefixo Redis, métricas), com migração coordenada
  de chaves, dashboards e jobs enfileirados. Ver docs/ARCHITECTURE.md §11.
