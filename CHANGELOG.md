# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).

## [0.1.0] — 2026-09-28 — Fase 1: Core

### Adicionado
- **Identifier Engine** (`osintizada/core/identifiers.py`): detecção de ~35 tipos de identificador com
  múltiplas hipóteses ordenadas por confiança e motivo explícito (identidade, internet, redes sociais,
  Telegram, cripto, hashes, onion). Validação de DV de CPF/CNPJ e checksum Bitcoin (base58check, bech32/bech32m).
- **Normalização** (`osintizada/core/normalization.py`): valor original sempre preservado, forma canônica
  para deduplicação e variantes apenas para pesquisa (telefone BR/E.164, CPF/CNPJ formatado/dígitos,
  raiz de CNPJ, email com forma canônica Gmail, domínios com punycode, URLs canônicas, IP/CIDR/ASN).
- **Modelos de domínio** (`osintizada/core/models.py`): `Entity` (com origem SEED/DISCOVERED/DERIVED/MANUAL),
  `Relationship` (exige motivo e evidência), `Evidence`, `ProviderResult`, `ProviderResponse`.
- **Enumerações** controladas: entidades, relações, status de provider (SUCCESS/NO_RESULTS/FAILED/
  RATE_LIMITED/AUTH_REQUIRED/NOT_CONFIGURED/TIMEOUT/SKIPPED/CANCELLED), classificação de dados, tiers, modos.
- **Query Planner** + `QueryBuilder`: consultas por tipo com `reason`, prioridade, custo, categoria, profundidade,
  operadores detectados, deduplicação, limites por modo, exclusão de domínios e RAW SEARCH.
- **Evidence Engine**: hash SHA-256 do conteúdo bruto, fingerprint independente de provider, deduplicação por URL
  canônica (N motores → 1 evidência com `duplicate_sightings`).
- **Interface de providers**: `BaseProvider` (template method com timeout, mapeamento de erros, sanitização)
  e especializações `LocalProvider`, `APIProvider`, `HTTPProvider`, `BrowserProvider`, `TorProvider`, `PaidProvider`;
  `ProviderRegistry` plugin-like; healthcheck com credenciais mascaradas.
- **SourceOrchestrator**: seleção de fontes justificada, execução paralela com concorrência limitada, orçamento
  de custo, bloqueio/whitelist de providers, cancelamento e search log.
- **Provider local** `local.identifier_analysis`: derivações estruturais (domínio do email, local-part como
  hipótese de username, domínio/subdomínio de URL, perfil social de URL), todas marcadas como DERIVED.
- **Configuração central** (`config/osintizada.yaml` + `osintizada/config.py`) com perfis de modo.
- **Secrets**: leitura apenas de variáveis de ambiente, mascaramento e filtro de logging que remove credenciais.
- **CLI**: `detect`, `plan`, `raw`, `run`, `providers`.
- 111 testes automatizados.
