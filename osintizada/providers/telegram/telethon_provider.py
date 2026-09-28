"""TelegramProvider — API oficial do Telegram via Telethon (MTProto).

Credenciais (variáveis de ambiente, nunca no código):
  TELEGRAM_API_ID, TELEGRAM_API_HASH  — https://my.telegram.org
  TELEGRAM_SESSION                    — StringSession de uma conta já autenticada

Sem credenciais (ou sem o pacote ``telethon``) o provider fica NOT_CONFIGURED.
Somente dados que a sessão pode ver legitimamente. Regras de identidade:
  * o **Telegram ID** é o identificador estável — vira a entidade principal;
  * o **username** é mutável — relacionado ao ID por ``USES_USERNAME`` com a data
    da observação, nunca tratado como identidade permanente.
"""

from __future__ import annotations

import importlib.util
from typing import Any

from osintizada.core.enums import (
    DataClassification,
    EntityType,
    IdentifierType,
    RelationType,
    SourceAccess,
    SourceTier,
)
from osintizada.core.models import EntityRef, NormalizedIdentifier, ProviderResult, utcnow
from osintizada.core.secrets import get_secret
from osintizada.images.hashing import ImageError, analyze_image
from osintizada.images.results import image_result
from osintizada.images.store import ArtifactStore
from osintizada.providers.base import (
    APIProvider,
    AuthRequiredError,
    ProviderNotConfigured,
    RateLimitedError,
    register_provider,
)

SECRETS = ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION")


def telethon_available() -> bool:
    return importlib.util.find_spec("telethon") is not None


def peer_kind(entity: Any) -> EntityType:
    kind = type(entity).__name__
    if kind == "Channel":
        return EntityType.TELEGRAM_GROUP if getattr(entity, "megagroup", False) else EntityType.TELEGRAM_CHANNEL
    if kind in ("Chat", "ChatForbidden", "ChannelForbidden"):
        return EntityType.TELEGRAM_GROUP
    return EntityType.TELEGRAM_USER


def entity_usernames(entity: Any) -> list[str]:
    names = []
    if getattr(entity, "username", None):
        names.append(entity.username)
    for extra in getattr(entity, "usernames", None) or []:  # usernames colecionáveis (Fragment)
        value = getattr(extra, "username", None)
        if value and value not in names:
            names.append(value)
    return names


def entity_attributes(entity: Any) -> dict[str, Any]:
    first, last = getattr(entity, "first_name", None), getattr(entity, "last_name", None)
    display = getattr(entity, "title", None) or " ".join(p for p in (first, last) if p) or None
    attrs = {
        "telegram_id": getattr(entity, "id", None),
        "peer_type": type(entity).__name__,
        "display_name": display,
        "first_name": first,
        "last_name": last,
        "usernames": entity_usernames(entity) or None,
        "is_bot": getattr(entity, "bot", None),
        "verified": getattr(entity, "verified", None),
        "scam": getattr(entity, "scam", None),
        "fake": getattr(entity, "fake", None),
        "participants_count": getattr(entity, "participants_count", None),
    }
    return {k: v for k, v in attrs.items() if v not in (None, False, "")}


@register_provider
class TelegramProvider(APIProvider):
    name = "social.telegram"
    display_name = "Telegram (API oficial)"
    description = "Resolve usernames/IDs públicos com uma sessão Telegram legítima (Telethon)."
    source_tag = "TELEGRAM"
    tier = SourceTier.TIER_2
    access = SourceAccess.AUTHENTICATED_SOURCE
    classification = DataClassification.AUTHENTICATED
    requires_auth = True
    required_secrets = SECRETS
    supported_identifiers = frozenset({IdentifierType.TELEGRAM_USERNAME, IdentifierType.TELEGRAM_ID,
                                       IdentifierType.TELEGRAM_LINK, IdentifierType.USERNAME})
    default_concurrency = 1
    default_rate_limit_per_minute = 20
    default_cache_ttl_seconds = 86400
    default_timeout_seconds = 30.0
    parser_version = "1"

    def is_configured(self) -> bool:
        if self.runtime.telegram_client_factory is not None:
            return True
        return super().is_configured() and telethon_available()

    def not_configured_reason(self) -> str:
        missing = [v for v in SECRETS if not get_secret(v)]
        if missing:
            return "Credenciais Telegram ausentes: " + ", ".join(missing)
        return "Pacote 'telethon' não instalado (pip install 'osintizada[telegram]')"

    # --- cliente -----------------------------------------------------------------------

    async def _client(self):
        if self.runtime.telegram_client_factory is not None:
            return self.runtime.telegram_client_factory()
        if not telethon_available():
            raise ProviderNotConfigured(self.not_configured_reason())
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        try:
            api_id = int(get_secret("TELEGRAM_API_ID") or "")
        except ValueError as exc:
            raise ProviderNotConfigured("TELEGRAM_API_ID deve ser numérico") from exc
        client = TelegramClient(StringSession(get_secret("TELEGRAM_SESSION")), api_id, get_secret("TELEGRAM_API_HASH"))
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise AuthRequiredError("Sessão Telegram não autorizada (gere TELEGRAM_SESSION novamente)")
        return client

    async def _healthcheck(self) -> str:
        client = await self._client()
        try:
            me = await client.get_me()
        finally:
            result = client.disconnect()
            if hasattr(result, "__await__"):
                await result
        return f"sessão autorizada (id {getattr(me, 'id', '?')})"

    # --- operações ---------------------------------------------------------------------

    async def resolve_username(self, client, username: str):
        """Resolve @username → entidade; ``None`` se não existe/ocupado."""
        try:
            return await client.get_entity(username.lstrip("@"))
        except ValueError:  # Telethon: "No user has ... as username"
            return None
        except Exception as exc:
            self._raise_mapped(exc)
            if type(exc).__name__ in ("UsernameNotOccupiedError", "UsernameInvalidError"):
                return None
            raise

    async def get_entity(self, client, telegram_id: int):
        try:
            return await client.get_entity(telegram_id)
        except ValueError:  # ID não visível para esta sessão
            return None
        except Exception as exc:
            self._raise_mapped(exc)
            raise

    async def search_public(self, client, query: str, limit: int = 20) -> list:
        """Busca global de usuários/canais públicos (contacts.Search)."""
        from telethon.tl.functions.contacts import SearchRequest

        try:
            found = await client(SearchRequest(q=query, limit=limit))
        except Exception as exc:
            self._raise_mapped(exc)
            raise
        return list(getattr(found, "users", [])) + list(getattr(found, "chats", []))

    @staticmethod
    def _raise_mapped(exc: Exception) -> None:
        name = type(exc).__name__
        if name in ("FloodWaitError", "FloodPremiumWaitError"):
            raise RateLimitedError(f"Telegram FloodWait de {getattr(exc, 'seconds', '?')}s",
                                   retry_after=float(getattr(exc, "seconds", 60)), code="FLOOD_WAIT") from exc
        if name in ("AuthKeyUnregisteredError", "AuthKeyError", "SessionRevokedError", "SessionExpiredError",
                    "UserDeactivatedError", "SessionPasswordNeededError"):
            raise AuthRequiredError(f"Sessão Telegram inválida ({name})") from exc

    async def _search(self, identifier: NormalizedIdentifier, query: str | None) -> list[ProviderResult]:
        client = await self._client()
        try:
            if identifier.type == IdentifierType.TELEGRAM_ID:
                raw_id = identifier.metadata.get("bare_id") or identifier.value
                entity = await self.get_entity(client, int(raw_id))
                queried_handle = None
            else:
                handle = identifier.value
                if identifier.type == IdentifierType.TELEGRAM_LINK:
                    handle = identifier.metadata.get("handle") or handle.rstrip("/").rsplit("/", 1)[-1]
                queried_handle = handle.lstrip("@").lower()
                entity = await self.resolve_username(client, queried_handle)
        finally:
            disconnect = getattr(client, "disconnect", None)
            if disconnect is not None:
                result = disconnect()
                if hasattr(result, "__await__"):
                    await result
        if entity is None:
            return []
        results = self.entity_to_results(entity, identifier, queried_handle)
        if avatar := await self._avatar(client, entity, results[0] if results else None):
            results.append(avatar)
        return results

    async def _avatar(self, client, entity, main: ProviderResult | None) -> ProviderResult | None:
        """Foto de perfil pública (se houver): bytes originais no ArtifactStore, hashes na evidência."""
        download = getattr(client, "download_profile_photo", None)
        if download is None or main is None or not getattr(entity, "photo", None):
            return None
        try:
            data = await download(entity, file=bytes)
        except Exception as exc:  # noqa: BLE001 - avatar é complementar; nunca derruba a consulta
            self.runtime.count("telegram_avatar_errors")
            _ = exc
            return None
        if not data:
            return None
        cfg = self.settings.images
        try:
            fp = analyze_image(bytes(data), max_bytes=cfg.max_bytes, max_pixels=cfg.max_pixels)
        except ImageError:
            return None
        ArtifactStore(self.settings.jobs.artifact_dir).put(bytes(data))
        photo_id = getattr(getattr(entity, "photo", None), "photo_id", None)
        return image_result(fp, owner=EntityRef(type=main.type, value=main.value), source_url=None,
                            source_name=self.display_name, observed_at=utcnow(), role="avatar",
                            classification=self.classification,
                            extra_raw={"telegram_photo_id": photo_id, "telegram_id": getattr(entity, "id", None)})

    def entity_to_results(self, entity: Any, identifier: NormalizedIdentifier,
                          queried_handle: str | None) -> list[ProviderResult]:
        observed = utcnow()
        kind = peer_kind(entity)
        # Consulta por ID: mantém o mesmo valor da entidade consultada (ex.: -100… de canais).
        telegram_id = identifier.value if queried_handle is None else str(entity.id)
        attrs = entity_attributes(entity)
        raw = {"telegram_id": entity.id, "peer_type": type(entity).__name__,
               "usernames": entity_usernames(entity), "observed_at": observed.isoformat(), "source": "Telegram API"}
        results: list[ProviderResult] = []
        if queried_handle is not None:
            # O ID (estável) usa o username (mutável) consultado — observação datada.
            results.append(ProviderResult(
                type=kind, value=telegram_id, source_name=self.display_name, confidence=0.95, raw=raw,
                observed_at=observed, attributes=attrs, classification=self.classification,
                relation_to_query=RelationType.USES_USERNAME, relation_direction="reverse",
                relation_reason=(f"API do Telegram resolveu @{queried_handle} para o ID {telegram_id} em "
                                 f"{observed.date()} (usernames são mutáveis)"),
            ))
        else:
            # Consulta por ID: se o peer é canal/grupo (tipo diferente do seed), liga as duas entidades.
            same_peer = kind != EntityType.TELEGRAM_USER
            results.append(ProviderResult(
                type=kind, value=telegram_id, source_name=self.display_name, confidence=0.95, raw=raw,
                observed_at=observed, attributes=attrs, classification=self.classification,
                relation_to_query=RelationType.SAME_AS if same_peer else None,
                relation_reason=(f"A API do Telegram identifica o ID {telegram_id} como {type(entity).__name__}"
                                 if same_peer else None),
            ))
        for username in entity_usernames(entity):
            if username.lower() == queried_handle:
                continue
            results.append(ProviderResult(
                type=EntityType.TELEGRAM_USER, value=username.lower(), source_name=self.display_name,
                confidence=0.95, raw=raw, observed_at=observed, classification=self.classification,
                attributes={"username": username},
                relation_to_query=RelationType.USES_USERNAME,
                relation_reason=f"ID {telegram_id} usava @{username} em {observed.date()}",
                **({"source_entity": {"type": kind, "value": telegram_id}} if queried_handle else {}),
            ))
        return results
