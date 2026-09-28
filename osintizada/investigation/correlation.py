"""CorrelationEngine inicial + detecção de contradições.

Princípios:
  * pontuação 100% determinística e configurável (``correlation.weights``) — sem LLM;
  * todo sinal carrega detalhe e evidências; o score é sempre explicável;
  * SAME_AS exige score alto **e** ao menos um sinal forte (Telegram ID, telefone, email);
    sinais fracos (username, nome, local) nunca bastam sozinhos → no máximo POSSIBLY_SAME_AS;
  * contradições são registradas (CONFLICTING_EVIDENCE), nunca sobrescritas.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from osintizada.config import Settings, get_settings
from osintizada.core.canonical import handle_of
from osintizada.core.enums import CorrelationLevel, EntityType, RelationType
from osintizada.images.hashing import ImageMatchLevel, compare
from osintizada.investigation.pivot import EntitySnapshot

IDENTITY_TYPES = frozenset({EntityType.SOCIAL_ACCOUNT, EntityType.TELEGRAM_USER, EntityType.USERNAME,
                            EntityType.PERSON, EntityType.EMAIL})
_CORRELATION_RELATIONS = {RelationType.SAME_AS.value, RelationType.POSSIBLY_SAME_AS.value}


@dataclass
class RelationView:
    id: str
    source_id: str
    target_id: str
    type: str
    evidence_ids: list[str]


@dataclass
class Signal:
    name: str
    weight: int
    detail: str
    evidence_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"signal": self.name, "weight": self.weight, "detail": self.detail, "evidence_ids": self.evidence_ids}


@dataclass
class CorrelationResult:
    entity_a_id: str
    entity_b_id: str
    score: int
    level: CorrelationLevel
    positive_signals: list[Signal]
    negative_signals: list[Signal]
    evidence_ids: list[str]

    @property
    def relation_type(self) -> RelationType | None:
        if self.level == CorrelationLevel.HIGH_CONFIDENCE:
            return RelationType.SAME_AS
        if self.level == CorrelationLevel.POSSIBLE:
            return RelationType.POSSIBLY_SAME_AS
        return None

    def explanation(self) -> str:
        pos = ", ".join(f"+{s.weight} {s.name}" for s in self.positive_signals) or "nenhum"
        neg = ", ".join(f"{s.weight} {s.name}" for s in self.negative_signals) or "nenhum"
        return f"Score {self.score}/100 ({self.level.value}). Positivos: {pos}. Contrários: {neg}."


def is_rare_handle(handle: str) -> bool:
    """Heurística simples: handles longos ou com dígitos/separadores são menos prováveis de coincidir."""
    return len(handle) >= 10 or (len(handle) >= 6 and any(c.isdigit() for c in handle)) or \
        (len(handle) >= 6 and any(c in "_." for c in handle))


def _norm(value) -> str | None:
    if value is None or isinstance(value, (list, dict)):
        return None
    text = " ".join(str(value).split()).casefold()
    return text or None


class CorrelationEngine:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.cfg = self.settings.correlation

    def _signal_for_neighbor(self, neighbor: EntitySnapshot) -> str | None:
        if neighbor.type == EntityType.EMAIL:
            return "same_email"
        if neighbor.type == EntityType.PHONE:
            return "same_phone"
        if neighbor.type in (EntityType.TELEGRAM_USER, EntityType.TELEGRAM_CHANNEL, EntityType.TELEGRAM_GROUP) \
                and neighbor.canonical_value.lstrip("-").isdigit():
            return "same_telegram_id"
        if neighbor.type == EntityType.DOMAIN:
            return "same_domain"
        if neighbor.type == EntityType.IMAGE:
            return "same_exact_avatar"  # mesma entidade IMAGE = mesmo SHA256
        return None

    def low_identity_images(self, entities: list[EntitySnapshot], relations: list[RelationView],
                            flags: dict[str, dict] | None = None) -> dict[str, str]:
        """IMAGE com baixo valor de identidade → motivo. Manual (flag), lista conhecida ou reuso amplo."""
        flags = flags or {}
        images = {e.id: e for e in entities if e.type == EntityType.IMAGE}
        identity_ids = {e.id for e in entities if e.type in IDENTITY_TYPES
                        or e.type in (EntityType.TELEGRAM_CHANNEL, EntityType.TELEGRAM_GROUP)}
        users: dict[str, set[str]] = defaultdict(set)
        for rel in relations:
            if rel.target_id in images and rel.source_id in identity_ids:
                users[rel.target_id].add(rel.source_id)
        known = {h.lower() for h in self.settings.images.known_generic_sha256}
        result: dict[str, str] = {}
        for image_id, image in images.items():
            flag = (flags.get(image_id) or {}).get("low_identity_value")
            if flag:
                result[image_id] = flag.get("reason", "marcada manualmente") if isinstance(flag, dict) else "manual"
            elif image.canonical_value.removeprefix("sha256:") in known:
                result[image_id] = "avatar genérico conhecido"
            elif len(users[image_id]) >= self.settings.images.generic_reuse_threshold:
                result[image_id] = f"reutilizada por {len(users[image_id])} contas neste Case"
        return result

    def _avatar_signals(self, entities, relations, attributes, low_identity) -> dict[tuple[str, str], list[Signal]]:
        """Sinais perceptuais entre donos de imagens DIFERENTES mas visualmente semelhantes."""
        cfg, weights = self.settings.images, self.cfg.weights
        by_id = {e.id: e for e in entities}
        owners: dict[str, dict[str, list[str]]] = defaultdict(dict)  # imagem → dono → evidências
        for rel in relations:
            target = by_id.get(rel.target_id)
            source = by_id.get(rel.source_id)
            if target is not None and source is not None and target.type == EntityType.IMAGE \
                    and source.type != EntityType.IMAGE:
                owners[target.id][source.id] = rel.evidence_ids
        hashes = {}
        for image_id in owners:
            attrs = attributes.get(image_id, {})
            ph = next((o["value"] for o in attrs.get("phash", [])), None)
            dh = next((o["value"] for o in attrs.get("dhash", [])), None)
            if ph and dh:
                hashes[image_id] = {"phash": ph, "dhash": dh}
        out: dict[tuple[str, str], list[Signal]] = defaultdict(list)
        for img_a, img_b in combinations(sorted(hashes), 2):
            match = compare(hashes[img_a], hashes[img_b], cfg.phash_very_similar, cfg.phash_similar, cfg.dhash_confirm)
            if match.level == ImageMatchLevel.PERCEPTUAL_VERY_SIMILAR:
                name = "very_similar_avatar"
            elif match.level == ImageMatchLevel.PERCEPTUAL_SIMILAR:
                name = "similar_avatar"
            else:
                continue
            weight = weights.get(name, 0)
            detail = f"{match.level.value} (pHash {match.phash_distance}, dHash {match.dhash_distance})"
            if img_a in low_identity or img_b in low_identity:
                weight = int(round(weight * self.cfg.low_identity_avatar_factor))
                detail += " — LOW_IDENTITY_VALUE"
            for owner_a, ev_a in owners[img_a].items():
                for owner_b, ev_b in owners[img_b].items():
                    if owner_a != owner_b:
                        out[tuple(sorted((owner_a, owner_b)))].append(
                            Signal(name, weight, detail, sorted(set(ev_a) | set(ev_b))))
        return out

    def correlate(
        self,
        entities: list[EntitySnapshot],
        relations: list[RelationView],
        attributes: dict[str, dict[str, list[dict]]] | None = None,
        entity_evidence: dict[str, list[str]] | None = None,
        flags: dict[str, dict] | None = None,
    ) -> list[CorrelationResult]:
        attributes = attributes or {}
        entity_evidence = entity_evidence or {}
        by_id = {e.id: e for e in entities}
        weights = self.cfg.weights
        low_identity = self.low_identity_images(entities, relations, flags)
        avatar_signals = self._avatar_signals(entities, relations, attributes, low_identity)

        # Grafo não direcionado, ignorando relações criadas pela própria correlação.
        neighbors: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        direct: set[tuple[str, str]] = set()
        for rel in relations:
            if rel.type in _CORRELATION_RELATIONS:
                continue
            neighbors[rel.source_id][rel.target_id].extend(rel.evidence_ids)
            neighbors[rel.target_id][rel.source_id].extend(rel.evidence_ids)
            direct.add(tuple(sorted((rel.source_id, rel.target_id))))

        identities = sorted((e for e in entities if e.type in IDENTITY_TYPES), key=lambda e: e.id)
        buckets: dict[str, set[str]] = defaultdict(set)
        for ent in identities:
            for nid in neighbors.get(ent.id, {}):
                nb = by_id.get(nid)
                if nb is not None and self._signal_for_neighbor(nb):
                    buckets[f"n:{nid}"].add(ent.id)
            if handle := handle_of(ent.type, ent.canonical_value):
                buckets[f"h:{handle}"].add(ent.id)

        pairs: set[tuple[str, str]] = set()
        for members in buckets.values():
            pairs.update(tuple(sorted(p)) for p in combinations(sorted(members), 2))
        pairs.update(p for p in avatar_signals if all(by_id[x].type in IDENTITY_TYPES
                                                        or by_id[x].type in (EntityType.TELEGRAM_CHANNEL,
                                                                             EntityType.TELEGRAM_GROUP)
                                                        for x in p))

        results: list[CorrelationResult] = []
        for a_id, b_id in sorted(pairs):
            if (a_id, b_id) in direct:
                continue  # já ligadas diretamente por evidência; nada a inferir
            a, b = by_id[a_id], by_id[b_id]
            positives: dict[str, Signal] = {}
            negatives: list[Signal] = []

            shared = set(neighbors.get(a_id, {})) & set(neighbors.get(b_id, {}))
            for nid in sorted(shared):
                nb = by_id.get(nid)
                name = self._signal_for_neighbor(nb) if nb else None
                if not name:
                    continue
                evs = sorted(set(neighbors[a_id][nid]) | set(neighbors[b_id][nid]))
                weight = weights.get(name, 0)
                label = f"ambos ligados a {nb.type.value} {nb.canonical_value}"
                if name == "same_exact_avatar" and nid in low_identity:
                    weight = int(round(weight * self.cfg.low_identity_avatar_factor))
                    label += f" — LOW_IDENTITY_VALUE ({low_identity[nid]})"
                sig = positives.setdefault(name, Signal(name, weight, "", []))
                sig.weight = max(sig.weight, weight) if sig.detail else weight
                sig.detail = (sig.detail + "; " if sig.detail else "") + label
                sig.evidence_ids = sorted(set(sig.evidence_ids) | set(evs))
            perceptual = avatar_signals.get((a_id, b_id), [])
            if perceptual and "same_exact_avatar" not in positives:
                # Um único sinal de avatar por par (o mais forte): evidência redundante não soma.
                best = max(perceptual, key=lambda sg: (sg.weight, sg.name))
                positives[best.name] = best

            ha, hb = handle_of(a.type, a.canonical_value), handle_of(b.type, b.canonical_value)
            if ha and ha == hb:
                name = "same_username_rare" if is_rare_handle(ha) else "same_username_common"
                positives[name] = Signal(name, weights.get(name, 0), f"mesmo username '{ha}' em {a.canonical_value} e "
                                         f"{b.canonical_value}",
                                         sorted(set(entity_evidence.get(a_id, [])) | set(entity_evidence.get(b_id, []))))

            attrs_a, attrs_b = attributes.get(a_id, {}), attributes.get(b_id, {})
            self._compare_attr(attrs_a, attrs_b, "display_name", "same_name", None, positives, negatives, weights)
            self._compare_attr(attrs_a, attrs_b, "country", "same_location", "conflicting_country", positives,
                               negatives, weights)
            self._compare_attr(attrs_a, attrs_b, "location", "same_location", "conflicting_location", positives,
                               negatives, weights)

            if not positives:
                continue
            pos = sorted(positives.values(), key=lambda s: (-s.weight, s.name))
            score = max(0, min(100, sum(s.weight for s in pos) + sum(s.weight for s in negatives)))
            strong = any(s.name in self.cfg.strong_signals for s in pos)
            if score >= self.cfg.same_as_threshold and strong:
                level = CorrelationLevel.HIGH_CONFIDENCE
            elif score >= self.cfg.possible_threshold:
                level = CorrelationLevel.POSSIBLE
            elif score >= self.cfg.weak_threshold:
                level = CorrelationLevel.WEAK
            else:
                level = CorrelationLevel.UNRELATED
            evidence = sorted({e for s in pos + negatives for e in s.evidence_ids})
            results.append(CorrelationResult(a_id, b_id, score, level, pos, negatives, evidence))
        return results

    @staticmethod
    def _compare_attr(attrs_a, attrs_b, key, positive_name, negative_name, positives, negatives, weights) -> None:
        va = {_norm(o.get("value")): o for o in attrs_a.get(key, []) if _norm(o.get("value"))}
        vb = {_norm(o.get("value")): o for o in attrs_b.get(key, []) if _norm(o.get("value"))}
        if not va or not vb:
            return
        common = set(va) & set(vb)
        if common:
            evs = sorted({va[c]["evidence_id"] for c in common} | {vb[c]["evidence_id"] for c in common})
            if positive_name not in positives:
                positives[positive_name] = Signal(positive_name, weights.get(positive_name, 0),
                                                  f"mesmo {key}: {', '.join(sorted(common))}", evs)
        elif negative_name:
            evs = sorted({o["evidence_id"] for o in va.values()} | {o["evidence_id"] for o in vb.values()})
            negatives.append(Signal(negative_name, weights.get(negative_name, 0),
                                    f"{key} divergente: {sorted(va)} × {sorted(vb)}", evs))


def detect_conflicts(attributes: dict[str, dict[str, list[dict]]], keys: list[str]) -> list[tuple[str, str, list[dict]]]:
    """Atributos da MESMA entidade com valores diferentes em fontes diferentes → (entity_id, atributo, observações)."""
    conflicts = []
    for entity_id in sorted(attributes):
        for key in keys:
            observations = attributes[entity_id].get(key, [])
            distinct = {_norm(o.get("value")) for o in observations} - {None}
            if len(distinct) > 1:
                conflicts.append((entity_id, key, observations))
    return conflicts
