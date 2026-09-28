"""Query Planner: gera consultas priorizadas, justificadas e limitadas.

Cada consulta planejada carrega ``reason`` (por que existe), ``priority``,
``cost``, ``category`` e ``depth``. O planner deduplica e aplica limites; ele
nunca executa nada — a execução é responsabilidade do orquestrador/providers.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from osintizada.config import Settings, get_settings
from osintizada.core.enums import IdentifierType as T
from osintizada.core.enums import QueryCategory as C
from osintizada.core.enums import SearchMode
from osintizada.core.models import NormalizedIdentifier, new_id
from osintizada.core.query_builder import detect_operators, quote


class PlannedQuery(BaseModel):
    id: str = Field(default_factory=new_id)
    query: str
    identifier: str
    identifier_type: T
    category: C
    priority: int = Field(ge=0, le=100)
    cost: int = 1
    reason: str
    depth: int = 0
    operators: list[str] = Field(default_factory=list)
    target: str = "search_engine"

    @property
    def dedup_key(self) -> str:
        return " ".join(self.query.lower().split())


@dataclass(frozen=True)
class QueryTemplate:
    pattern: str  # {q} = valor entre aspas; {v} = valor cru
    category: C
    priority: int
    reason: str


def _t(pattern: str, category: C, priority: int, reason: str) -> QueryTemplate:
    return QueryTemplate(pattern, category, priority, reason)


# --- Blocos reutilizáveis de templates ----------------------------------------

_SOCIAL_SITES = [
    ("t.me", C.TELEGRAM, 80, "Referências públicas no Telegram"),
    ("telegram.me", C.TELEGRAM, 55, "Links alternativos do Telegram"),
    ("github.com", C.CODE, 75, "Perfis, repositórios e commits no GitHub"),
    ("reddit.com", C.SOCIAL, 65, "Posts e perfis no Reddit"),
    ("x.com", C.SOCIAL, 65, "Perfis e posts no X/Twitter"),
    ("twitter.com", C.SOCIAL, 45, "Conteúdo indexado sob domínio antigo do Twitter"),
    ("instagram.com", C.SOCIAL, 60, "Perfis no Instagram"),
    ("tiktok.com", C.SOCIAL, 55, "Perfis no TikTok"),
    ("youtube.com", C.SOCIAL, 50, "Canais e vídeos no YouTube"),
    ("facebook.com", C.SOCIAL, 50, "Perfis e páginas no Facebook"),
]

_CONTEXT_WORDS = [
    ("email", 70, "Possíveis emails associados"),
    ("telegram", 70, "Menções do identificador junto a Telegram"),
    ("discord", 60, "Menções junto a Discord"),
    ("github", 55, "Menções junto a GitHub"),
    ("forum", 55, "Presença em fóruns"),
    ("paste", 50, "Presença em sites de paste"),
]

_PASTE_SITES = [
    ("pastebin.com", 50, "Pastes públicos no Pastebin"),
    ("ghostbin.site", 30, "Pastes públicos no Ghostbin"),
    ("rentry.co", 30, "Notas públicas no Rentry"),
]

_DOCUMENT_TEMPLATES = [
    _t("{q} filetype:pdf", C.DOCUMENTS, 70, "Documentos PDF públicos que citam o identificador"),
    _t("{q} edital", C.DOCUMENTS, 50, "Editais públicos"),
    _t("{q} contrato", C.DOCUMENTS, 55, "Contratos públicos"),
    _t("{q} licitação", C.DOCUMENTS, 50, "Processos licitatórios"),
    _t("{q} termo", C.DOCUMENTS, 35, "Termos e aditivos"),
    _t('{q} "diário oficial"', C.GOVERNMENT, 65, "Publicações em diários oficiais"),
    _t("{q} portaria", C.GOVERNMENT, 45, "Portarias"),
    _t("{q} processo", C.DOCUMENTS, 45, "Processos administrativos/judiciais públicos"),
    _t("{q} ata", C.DOCUMENTS, 35, "Atas de reuniões e assembleias"),
    _t("{q} site:gov.br", C.GOVERNMENT, 60, "Menções em portais do governo brasileiro"),
    _t("{q} site:jus.br", C.GOVERNMENT, 55, "Menções em portais do Judiciário"),
    _t("{q} (filetype:xls OR filetype:xlsx OR filetype:csv)", C.DOCUMENTS, 40, "Planilhas públicas"),
]


def _username_templates() -> list[QueryTemplate]:
    items = [
        _t("{q}", C.EXACT, 100, "Busca exata do username"),
        _t('"@{v}"', C.EXACT, 85, "Busca do handle com @"),
    ]
    items += [_t(f"{{q}} {w}", C.CONTEXT, p, r) for w, p, r in _CONTEXT_WORDS]
    items += [_t(f"site:{s} {{q}}", c, p, f"{r} (username)") for s, c, p, r in _SOCIAL_SITES]
    items += [_t(f"site:{s} {{q}}", C.PASTE, p, r) for s, p, r in _PASTE_SITES]
    items += [_t("{q} -site:x.com -site:twitter.com -site:instagram.com", C.EXACT, 40,
                 "Resultados fora das grandes redes (fóruns, blogs, documentos)")]
    return items


TEMPLATES: dict[T, list[QueryTemplate]] = {
    T.USERNAME: _username_templates(),
    T.ALIAS: [
        _t("{q}", C.EXACT, 70, "Busca exata do alias"),
        _t("{q} apelido OR alias OR nickname", C.CONTEXT, 40, "Alias citado explicitamente como apelido"),
    ],
    T.EMAIL: [
        _t("{q}", C.EXACT, 100, "Busca exata do email"),
        *[_t(f"{{q}} {w}", C.CONTEXT, p - 5, r) for w, p, r in _CONTEXT_WORDS if w != "email"],
        _t("site:github.com {q}", C.CODE, 75, "Email exposto em commits/perfis GitHub"),
        _t("site:t.me {q}", C.TELEGRAM, 70, "Email citado em canais públicos do Telegram"),
        *[_t(f"site:{s} {{q}}", C.PASTE, p, r) for s, p, r in _PASTE_SITES],
        _t("{q} filetype:pdf", C.DOCUMENTS, 60, "Email em documentos PDF públicos"),
        _t("{q} (filetype:xls OR filetype:xlsx OR filetype:csv)", C.DOCUMENTS, 45, "Email em planilhas públicas"),
    ],
    T.PHONE: [
        _t("{q} telegram OR whatsapp", C.CONTEXT, 65, "Telefone associado a mensageiros"),
        _t("site:t.me {q}", C.TELEGRAM, 60, "Telefone citado em canais do Telegram"),
        _t("{q} filetype:pdf", C.DOCUMENTS, 55, "Telefone em documentos públicos"),
        _t('{q} "diário oficial"', C.GOVERNMENT, 45, "Telefone em publicações oficiais"),
    ],
    T.CPF: list(_DOCUMENT_TEMPLATES),
    T.CNPJ: [
        *_DOCUMENT_TEMPLATES,
        _t("{q} sócio OR sócios OR QSA", C.GOVERNMENT, 60, "Quadro societário citado publicamente"),
        _t("site:pncp.gov.br {q}", C.GOVERNMENT, 60, "Contratações no PNCP"),
        _t("site:portaldatransparencia.gov.br {q}", C.GOVERNMENT, 60, "Registros no Portal da Transparência"),
    ],
    T.FULL_NAME: [
        _t("{q}", C.EXACT, 90, "Busca exata do nome"),
        *_DOCUMENT_TEMPLATES,
        _t("site:linkedin.com/in {q}", C.SOCIAL, 55, "Perfis profissionais"),
        _t("site:escavador.com {q}", C.GOVERNMENT, 45, "Registros acadêmicos/processuais agregados"),
        _t("site:jusbrasil.com.br {q}", C.GOVERNMENT, 45, "Processos e diários indexados"),
        _t("site:t.me {q}", C.TELEGRAM, 40, "Nome citado em canais do Telegram"),
    ],
    T.DOMAIN: [
        _t("site:{v}", C.INFRASTRUCTURE, 90, "Páginas indexadas do domínio"),
        _t("site:*.{v} -site:www.{v}", C.INFRASTRUCTURE, 75, "Subdomínios indexados por mecanismos de busca"),
        _t("{q} -site:{v}", C.EXACT, 85, "Menções externas ao domínio"),
        _t("site:{v} filetype:pdf", C.DOCUMENTS, 65, "Documentos PDF hospedados no domínio"),
        _t("site:{v} (filetype:xls OR filetype:xlsx OR filetype:doc OR filetype:docx)", C.DOCUMENTS, 50,
           "Documentos Office hospedados no domínio"),
        _t("site:{v} inurl:admin OR inurl:login", C.INFRASTRUCTURE, 35, "Painéis públicos indexados"),
        _t("{q} whois OR registrant", C.INFRASTRUCTURE, 40, "Dados de registro citados publicamente"),
        _t("site:t.me {q}", C.TELEGRAM, 50, "Domínio citado em canais do Telegram"),
        _t("site:github.com {q}", C.CODE, 55, "Domínio citado em código público"),
        *[_t(f"site:{s} {{q}}", C.PASTE, p, r) for s, p, r in _PASTE_SITES],
        _t("{q} contrato OR licitação", C.DOCUMENTS, 35, "Domínio citado em contratações públicas"),
    ],
    T.SUBDOMAIN: [
        _t("site:{v}", C.INFRASTRUCTURE, 85, "Páginas indexadas do subdomínio"),
        _t("{q} -site:{v}", C.EXACT, 70, "Menções externas ao subdomínio"),
        _t("site:github.com {q}", C.CODE, 45, "Subdomínio citado em código público"),
    ],
    T.HOSTNAME: [_t("{q}", C.EXACT, 60, "Menções ao hostname")],
    T.URL: [
        _t("{q}", C.EXACT, 90, "Páginas que referenciam a URL"),
        _t("site:t.me {q}", C.TELEGRAM, 50, "URL compartilhada no Telegram"),
    ],
    T.TELEGRAM_LINK: [_t("{q}", C.EXACT, 85, "Páginas que referenciam o link do Telegram")],
    T.IPV4: [
        _t("{q}", C.EXACT, 90, "Menções públicas ao IP"),
        _t("{q} abuse OR malware OR phishing", C.INFRASTRUCTURE, 65, "Relatos de abuso associados ao IP"),
        _t("site:github.com {q}", C.CODE, 45, "IP em configurações/código público"),
        *[_t(f"site:{s} {{q}}", C.PASTE, p, r) for s, p, r in _PASTE_SITES],
    ],
    T.IPV6: [_t("{q}", C.EXACT, 90, "Menções públicas ao IPv6")],
    T.CIDR: [_t("{q}", C.EXACT, 70, "Menções públicas ao bloco de rede")],
    T.ASN: [
        _t("{q}", C.EXACT, 80, "Menções públicas ao ASN"),
        _t("{q} peering OR upstream", C.INFRASTRUCTURE, 45, "Relações de roteamento citadas publicamente"),
    ],
    T.ONION: [_t("{q}", C.EXACT, 80, "Menções ao endereço onion na clearnet")],
    T.TELEGRAM_USERNAME: [
        _t("{q}", C.EXACT, 90, "Busca exata do username Telegram"),
        _t('"t.me/{v}"', C.TELEGRAM, 95, "Links diretos para o perfil/canal"),
        _t("site:t.me {q}", C.TELEGRAM, 85, "Menções em canais públicos"),
        _t('"@{v}" telegram', C.TELEGRAM, 75, "Handle citado junto a Telegram"),
        _t("site:tgstat.com {q}", C.TELEGRAM, 50, "Estatísticas de canais públicos (TGStat)"),
    ],
    T.TELEGRAM_ID: [_t("{q} telegram", C.TELEGRAM, 60, "ID numérico citado junto a Telegram")],
    T.TWITTER_USERNAME: [
        _t('"@{v}"', C.EXACT, 85, "Handle citado na web"),
        _t("site:x.com {q}", C.SOCIAL, 80, "Perfil e posts no X"),
    ],
    T.INSTAGRAM_USERNAME: [
        _t('"@{v}"', C.EXACT, 80, "Handle citado na web"),
        _t("site:instagram.com {q}", C.SOCIAL, 80, "Perfil no Instagram"),
    ],
    T.TIKTOK_USERNAME: [_t('"@{v}" tiktok', C.SOCIAL, 80, "Handle citado junto a TikTok")],
    T.GITHUB_USERNAME: [
        _t('"github.com/{v}"', C.CODE, 90, "Links para o perfil GitHub"),
        _t("{q} github", C.CODE, 70, "Username citado junto a GitHub"),
    ],
    T.REDDIT_USERNAME: [_t('"u/{v}"', C.SOCIAL, 85, "Menções do usuário no Reddit")],
    T.YOUTUBE_CHANNEL: [_t("{q} youtube", C.SOCIAL, 75, "Canal citado na web")],
    T.FACEBOOK_PROFILE: [_t("{q} facebook", C.SOCIAL, 70, "Perfil citado na web")],
    T.BTC_ADDRESS: [
        _t("{q}", C.EXACT, 95, "Menções públicas ao endereço"),
        _t("{q} scam OR fraud OR golpe OR ransom", C.CRYPTO, 75, "Relatos de fraude associados"),
        _t("site:t.me {q}", C.TELEGRAM, 60, "Endereço divulgado no Telegram"),
        *[_t(f"site:{s} {{q}}", C.PASTE, p, r) for s, p, r in _PASTE_SITES],
    ],
    T.EVM_ADDRESS: [
        _t("{q}", C.EXACT, 95, "Menções públicas ao endereço"),
        _t("{q} scam OR fraud OR golpe OR phishing", C.CRYPTO, 75, "Relatos de fraude associados"),
        _t("site:t.me {q}", C.TELEGRAM, 60, "Endereço divulgado no Telegram"),
        _t("site:github.com {q}", C.CODE, 50, "Endereço em código/contratos públicos"),
    ],
    T.TX_HASH: [_t("{q}", C.EXACT, 85, "Menções públicas à transação")],
    T.MD5: [_t("{q}", C.EXACT, 85, "Menções públicas ao hash")],
    T.SHA1: [_t("{q}", C.EXACT, 85, "Menções públicas ao hash")],
    T.SHA256: [
        _t("{q}", C.EXACT, 90, "Menções públicas ao hash"),
        _t("{q} malware OR sample", C.INFRASTRUCTURE, 60, "Hash citado em análises de malware"),
    ],
    T.SHA512: [_t("{q}", C.EXACT, 85, "Menções públicas ao hash")],
    T.POSSIBLE_NAME: [_t("{q}", C.EXACT, 40, "Termo pode ser nome/sobrenome")],
    T.KEYWORD: [_t("{q}", C.EXACT, 60, "Busca exata do termo")],
}

# Tipos cujas variantes (formatos alternativos) devem virar buscas exatas extras.
_VARIANT_EXACT_TYPES = {T.PHONE: 90, T.CPF: 95, T.CNPJ: 95, T.USERNAME: 60, T.FULL_NAME: 50}


class QueryPlanner:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def plan(
        self,
        identifier: NormalizedIdentifier,
        mode: SearchMode = SearchMode.DEEP,
        depth: int = 0,
        max_queries: int | None = None,
        excluded_domains: list[str] | None = None,
    ) -> list[PlannedQuery]:
        profile = self.settings.mode(mode)
        limit = max_queries if max_queries is not None else profile.max_queries_per_identifier
        allowed = set(profile.query_categories) if profile.query_categories else None
        cost = self.settings.query_budget.costs.get("web", 1)
        depth_penalty = self.settings.search.depth_priority_penalty * depth

        candidates: list[PlannedQuery] = []

        def add(query: str, category: C, priority: int, reason: str) -> None:
            if allowed is not None and category not in allowed:
                return
            if excluded_domains:
                query = query + "".join(f" -site:{d}" for d in excluded_domains)
            candidates.append(
                PlannedQuery(
                    query=query,
                    identifier=identifier.value,
                    identifier_type=identifier.type,
                    category=category,
                    priority=max(0, min(100, priority - depth_penalty)),
                    cost=cost,
                    reason=reason,
                    depth=depth,
                    operators=detect_operators(query),
                )
            )

        value = identifier.value
        search_value = identifier.metadata.get("case_preserved", value)
        if identifier.type in (T.CPF, T.CNPJ):
            search_value = identifier.metadata.get("formatted", value)
        elif identifier.type == T.PHONE:
            search_value = identifier.variants[0] if identifier.variants else value

        for tpl in TEMPLATES.get(identifier.type, []):
            query = tpl.pattern.replace("{q}", quote(search_value)).replace("{v}", search_value)
            add(query, tpl.category, tpl.priority, tpl.reason)

        if identifier.type in _VARIANT_EXACT_TYPES:
            base_prio = _VARIANT_EXACT_TYPES[identifier.type]
            for i, variant in enumerate(identifier.variants):
                add(quote(variant), C.EXACT, base_prio - 5 * i, f"Busca exata da variante de formato '{variant}'")

        return self._finalize(candidates, limit)

    def plan_many(
        self, identifiers: list[NormalizedIdentifier], mode: SearchMode = SearchMode.DEEP, depth: int = 0
    ) -> list[PlannedQuery]:
        """Planeja para vários identificadores respeitando o limite global do modo."""
        profile = self.settings.mode(mode)
        merged: list[PlannedQuery] = []
        for ident in identifiers:
            merged.extend(self.plan(ident, mode=mode, depth=depth))
        return self._finalize(merged, profile.max_queries)

    @staticmethod
    def raw(query: str) -> PlannedQuery:
        """RAW SEARCH: consulta manual do investigador, registrada como tal."""
        text = query.strip()
        if not text:
            raise ValueError("Consulta vazia")
        return PlannedQuery(
            query=text,
            identifier=text,
            identifier_type=T.KEYWORD,
            category=C.RAW,
            priority=100,
            reason="Consulta manual (RAW SEARCH) enviada pelo investigador",
            operators=detect_operators(text),
        )

    @staticmethod
    def _finalize(candidates: list[PlannedQuery], limit: int) -> list[PlannedQuery]:
        seen: set[str] = set()
        unique: list[PlannedQuery] = []
        for q in sorted(candidates, key=lambda q: q.priority, reverse=True):
            if q.dedup_key in seen:
                continue
            seen.add(q.dedup_key)
            unique.append(q)
        return unique[:limit]
