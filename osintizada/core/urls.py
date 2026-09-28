"""Utilitários de URL: canonicalização (deduplicação) e parsing de perfis sociais."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from osintizada.core.enums import IdentifierType

TRACKING_PARAMS_PREFIXES = ("utm_",)
TRACKING_PARAMS = frozenset(
    {"fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid", "mc_cid", "mc_eid", "ref_src", "si"}
)
_DEFAULT_PORTS = {"http": 80, "https": 443}


def canonical_url(url: str) -> str:
    """Forma canônica de uma URL para deduplicação.

    - scheme/host em lowercase; remove porta padrão e ``www.``;
    - remove fragmento e parâmetros de rastreamento; ordena a query;
    - remove barra final (exceto na raiz).

    A URL original deve sempre ser preservada separadamente pelo chamador.
    """
    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    if parts.port and _DEFAULT_PORTS.get(scheme) != parts.port:
        netloc = f"{host}:{parts.port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith(TRACKING_PARAMS_PREFIXES)
    )
    # http e https apontam para o mesmo recurso para fins de deduplicação.
    if scheme in ("http", "https"):
        scheme = "https"
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


@dataclass(frozen=True)
class SocialHandle:
    platform: str
    identifier_type: IdentifierType
    handle: str


# host -> (plataforma, tipo, regex do caminho capturando o handle)
_SOCIAL_PATTERNS: list[tuple[tuple[str, ...], str, IdentifierType, re.Pattern[str]]] = [
    (("t.me", "telegram.me", "telegram.dog"), "telegram", IdentifierType.TELEGRAM_USERNAME,
     re.compile(r"^/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})(?:/\d+)?/?$")),
    (("twitter.com", "x.com", "mobile.twitter.com"), "twitter", IdentifierType.TWITTER_USERNAME,
     re.compile(r"^/([A-Za-z0-9_]{1,15})/?(?:status/\d+/?)?$")),
    (("instagram.com",), "instagram", IdentifierType.INSTAGRAM_USERNAME,
     re.compile(r"^/([A-Za-z0-9_.]{1,30})/?$")),
    (("tiktok.com",), "tiktok", IdentifierType.TIKTOK_USERNAME,
     re.compile(r"^/@([A-Za-z0-9_.]{2,24})/?")),
    (("github.com",), "github", IdentifierType.GITHUB_USERNAME,
     re.compile(r"^/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))(?:/[^/]+)?/?$")),
    (("reddit.com", "old.reddit.com"), "reddit", IdentifierType.REDDIT_USERNAME,
     re.compile(r"^/(?:u|user)/([A-Za-z0-9_-]{3,20})/?")),
    (("youtube.com", "m.youtube.com"), "youtube", IdentifierType.YOUTUBE_CHANNEL,
     re.compile(r"^/(@[A-Za-z0-9_.-]{3,30}|channel/UC[A-Za-z0-9_-]{22}|c/[^/]+|user/[^/]+)/?")),
    (("facebook.com", "m.facebook.com", "fb.com"), "facebook", IdentifierType.FACEBOOK_PROFILE,
     re.compile(r"^/((?:profile\.php\?id=)?[A-Za-z0-9.]{5,50})/?$")),
]

# Caminhos reservados que não são perfis.
_RESERVED = {
    "telegram": {"joinchat", "addstickers", "share", "proxy", "c", "iv"},
    "twitter": {"home", "search", "explore", "i", "intent", "share", "hashtag", "settings", "login"},
    "instagram": {"p", "reel", "reels", "explore", "stories", "accounts", "tv"},
    "github": {"orgs", "topics", "search", "marketplace", "features", "settings", "login", "about",
               "pricing", "sponsors", "collections", "trending", "apps", "notifications"},
    "facebook": {"groups", "pages", "events", "watch", "marketplace", "login", "sharer", "share"},
}


def parse_social_url(url: str) -> SocialHandle | None:
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path
    if parts.query and "profile.php" in path:
        path = f"{path}?{parts.query}"
    for hosts, platform, id_type, pattern in _SOCIAL_PATTERNS:
        if host not in hosts:
            continue
        match = pattern.match(path)
        if not match:
            return None
        handle = match.group(1)
        if handle.split("/")[0].lower() in _RESERVED.get(platform, set()):
            return None
        return SocialHandle(platform=platform, identifier_type=id_type, handle=handle)
    return None
