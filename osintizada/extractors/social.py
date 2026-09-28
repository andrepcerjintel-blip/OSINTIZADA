"""Extractors sociais: Telegram, perfis em URLs e menções @handle."""

from __future__ import annotations

import re
from collections.abc import Iterable

from osintizada.core.enums import EntityType, IdentifierType
from osintizada.core.models import social_account_value
from osintizada.core.urls import parse_social_url
from osintizada.extractors.base import BaseExtractor, Extraction
from osintizada.extractors.network import clean_url

_TG_LINK = re.compile(
    r"(?<![\w.])(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?:s/)?"
    r"(\+[\w-]{10,}|joinchat/[\w-]{10,}|[A-Za-z][A-Za-z0-9_]{3,31})(?:/(\d+))?",
    re.I,
)
_TG_CONTEXT_HANDLE = re.compile(r"(?i)\b(?:telegram|tg|telegrama)\b\s*[:\-–]?\s*@([A-Za-z][A-Za-z0-9_]{4,31})\b")
_TG_RESERVED = {"joinchat", "addstickers", "share", "proxy", "c", "iv", "socks", "setlanguage", "addtheme"}


class TelegramExtractor(BaseExtractor):
    name = "telegram"
    produces = frozenset({EntityType.TELEGRAM_USER, EntityType.TELEGRAM_GROUP, EntityType.URL})
    priority = 8
    consumes_span = True

    def extract(self, text: str) -> Iterable[Extraction]:
        for m in _TG_LINK.finditer(text):
            target, message_id = m.group(1), m.group(2)
            if target.startswith("+") or target.lower().startswith("joinchat/"):
                invite = target.split("/", 1)[-1]
                yield self.make(text, m.start(), m.end(), EntityType.TELEGRAM_GROUP, f"invite:{invite}", 0.8,
                                IdentifierType.TELEGRAM_LINK, invite_link=True, url=m.group(0))
                continue
            if target.lower() in _TG_RESERVED:
                continue
            # Username Telegram é mutável: registrado como handle observado, nunca como identidade.
            yield self.make(text, m.start(), m.end(), EntityType.TELEGRAM_USER, target.lower(), 0.9,
                            IdentifierType.TELEGRAM_USERNAME, handle=target, message_id=message_id,
                            url=m.group(0))
        for m in _TG_CONTEXT_HANDLE.finditer(text):
            yield self.make(text, m.start(1) - 1, m.end(1), EntityType.TELEGRAM_USER, m.group(1).lower(), 0.75,
                            IdentifierType.TELEGRAM_USERNAME, handle=m.group(1), context_keyword=True)


_SOCIAL_URL = re.compile(
    r"(?<![\w.])(?:https?://)?(?:www\.|m\.|mobile\.|old\.)?"
    r"(?:twitter\.com|x\.com|instagram\.com|tiktok\.com|github\.com|reddit\.com|youtube\.com|facebook\.com|fb\.com)"
    r"/[^\s<>\"'`]+",
    re.I,
)


class SocialProfileExtractor(BaseExtractor):
    name = "social_profile"
    produces = frozenset({EntityType.SOCIAL_ACCOUNT})
    priority = 9

    def extract(self, text: str) -> Iterable[Extraction]:
        for m in _SOCIAL_URL.finditer(text):
            url = clean_url(m.group(0))
            social = parse_social_url(url)
            if social is None or social.platform == "telegram":
                continue
            yield self.make(text, m.start(), m.start() + len(url), EntityType.SOCIAL_ACCOUNT,
                            social_account_value(social.platform, social.handle), 0.85, social.identifier_type,
                            platform=social.platform, handle=social.handle, url=url)


_MENTION = re.compile(r"(?<![\w@./])@([A-Za-z0-9_](?:[A-Za-z0-9_.]{0,30}[A-Za-z0-9_])?)(?![\w@])")


class MentionExtractor(BaseExtractor):
    """Menções ``@handle`` genéricas (plataforma desconhecida) → USERNAME de baixa confiança."""

    name = "mention"
    produces = frozenset({EntityType.USERNAME})
    priority = 50
    respect_consumed = True

    def extract(self, text: str) -> Iterable[Extraction]:
        for m in _MENTION.finditer(text):
            handle = m.group(1)
            if handle.isdigit() or len(handle) < 2:
                continue
            yield self.make(text, m.start(), m.end(), EntityType.USERNAME, handle, 0.5, IdentifierType.USERNAME,
                            platform_unknown=True)
