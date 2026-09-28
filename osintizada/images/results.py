"""Construção do ProviderResult de uma imagem (entidade IMAGE com proveniência completa)."""

from __future__ import annotations

from datetime import datetime

from osintizada.core.enums import EntityType, RelationType
from osintizada.core.models import EntityRef, ProviderResult
from osintizada.images.hashing import ImageFingerprint


def image_entity_value(sha256: str) -> str:
    return f"sha256:{sha256}"


def image_result(fp: ImageFingerprint, *, owner: EntityRef | None, source_url: str | None, source_name: str,
                 observed_at: datetime | None, role: str = "avatar", classification=None,
                 extra_raw: dict | None = None) -> ProviderResult:
    """IMAGE ligada ao perfil por ``USES`` (perfil → imagem). Registra perfil, URL, hash e hashes perceptuais."""
    kwargs = {"classification": classification} if classification is not None else {}
    return ProviderResult(
        type=EntityType.IMAGE, value=image_entity_value(fp.sha256), source_url=source_url, source_name=source_name,
        observed_at=observed_at, confidence=0.95,
        raw={"image": fp.as_dict(), "role": role, "owner": owner.model_dump(mode="json") if owner else None,
             **(extra_raw or {})},
        attributes={"sha256": fp.sha256, "phash": fp.phash, "dhash": fp.dhash, "mime": fp.mime,
                    "width": fp.width, "height": fp.height, "role": role},
        relation_to_query=RelationType.USES, source_entity=owner,
        relation_reason=f"Imagem de {role} do perfil (sha256 {fp.sha256[:12]}…, pHash {fp.phash})",
        **kwargs,
    )
