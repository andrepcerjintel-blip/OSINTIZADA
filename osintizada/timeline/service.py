"""TimelineService — eventos cronológicos a partir das evidências.

Regra central: a data de um evento é a data DO FATO (``observed_at`` — quando o conteúdo
existiu/foi publicado/o certificado foi emitido), NUNCA a data da coleta. Evidências sem
data de fato só aparecem com ``include_collection_only=True``, rotuladas como
``COLLECTED`` e ``time_basis=collected_at`` — nunca misturadas como se fossem fatos.

Tipos de evento extensíveis: ``register_event_rule(func)``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from pydantic import BaseModel

from osintizada.db import Database
from osintizada.repositories import EntityRepository, EvidenceRepository

EventRule = Callable[[dict, dict], str | None]  # (evidência, entidade) → tipo de evento ou None
_RULES: list[EventRule] = []


def register_event_rule(rule: EventRule) -> EventRule:
    _RULES.append(rule)
    return rule


@register_event_rule
def _certificate(ev: dict, ent: dict) -> str | None:
    if ev["provider"] == "infra.crtsh" and ent["type"] in ("SUBDOMAIN", "DOMAIN"):
        return "CERTIFICATE_ISSUED"
    return None


@register_event_rule
def _archived(ev: dict, ent: dict) -> str | None:
    return "PAGE_ARCHIVED" if ev["source_type"] == "ARCHIVE" and ent["type"] == "URL" else None


@register_event_rule
def _published(ev: dict, ent: dict) -> str | None:
    return "DOCUMENT_PUBLISHED" if ev["provider"].startswith("search.") and ent["type"] == "URL" else None


@register_event_rule
def _account(ev: dict, ent: dict) -> str | None:
    if ent["type"] in ("SOCIAL_ACCOUNT", "TELEGRAM_USER", "TELEGRAM_CHANNEL", "TELEGRAM_GROUP", "USERNAME"):
        return "ACCOUNT_OBSERVED"
    return "AVATAR_OBSERVED" if ent["type"] == "IMAGE" else None


@register_event_rule
def _domain(ev: dict, ent: dict) -> str | None:
    return "DOMAIN_OBSERVED" if ent["type"] in ("DOMAIN", "SUBDOMAIN") else None


@register_event_rule
def _relationship(ev: dict, ent: dict) -> str | None:
    return "RELATIONSHIP_OBSERVED" if (ev.get("meta") or {}).get("relation_reason") else None


class TimelineEvent(BaseModel):
    id: str
    event_type: str
    event_time: str | None
    time_basis: str             # observed_at | published_at | collected_at (somente coleta)
    collected_at: str
    temporality: str            # CURRENT_DATA | HISTORICAL_DATA
    entity_id: str
    entity_type: str
    entity_value: str
    provider: str
    source_url: str | None
    description: str
    evidence_id: str


def _parse(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class TimelineService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def build(self, case_id: str, entity_type: str | None = None, provider: str | None = None,
              date_from: str | datetime | None = None, date_to: str | datetime | None = None,
              include_collection_only: bool = False) -> list[TimelineEvent]:
        start, end = _parse(date_from), _parse(date_to)
        with self.db.session() as s:
            entities = {e.id: e for e in EntityRepository(s).list(case_id)}
            evidence = EvidenceRepository(s).list(case_id)
            events: list[TimelineEvent] = []
            for ev in evidence:
                ent = entities.get(ev.entity_id)
                if ent is None or (entity_type and ent.type != entity_type) or (provider and ev.provider != provider):
                    continue
                observed = _parse(ev.observed_at)
                collected = _parse(ev.collected_at)
                ev_dict = {"provider": ev.provider, "source_type": ev.source_type, "meta": ev.meta or {}}
                ent_dict = {"type": ent.type}
                if observed is None:
                    if not include_collection_only:
                        continue
                    kind, basis, when = "COLLECTED", "collected_at", collected
                else:
                    kind = next((k for rule in _RULES if (k := rule(ev_dict, ent_dict))), "ENTITY_OBSERVED")
                    basis = "published_at" if kind == "DOCUMENT_PUBLISHED" else "observed_at"
                    when = observed
                if (start and when < start) or (end and when > end):
                    continue
                reason = (ev.meta or {}).get("relation_reason")
                events.append(TimelineEvent(
                    id=ev.id, event_type=kind, event_time=when.isoformat() if when else None, time_basis=basis,
                    collected_at=collected.isoformat() if collected else "", temporality=ev.temporality,
                    entity_id=ent.id, entity_type=ent.type, entity_value=ent.canonical_value, provider=ev.provider,
                    source_url=ev.source_url, evidence_id=ev.id,
                    description=reason or f"{ent.type} {ent.canonical_value} ({ev.source_name or ev.provider})"))
        events.sort(key=lambda e: (e.event_time or "", e.id))
        return events
