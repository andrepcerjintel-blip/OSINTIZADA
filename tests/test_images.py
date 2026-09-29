import hashlib

import pytest
from PIL import Image

from osintizada.config import Settings
from osintizada.core.enums import CorrelationLevel, EntityType, IdentifierType, RelationType
from osintizada.core.normalization import normalize
from osintizada.images.hashing import ImageError, ImageMatchLevel, analyze_image, compare, hamming
from osintizada.images.store import ArtifactStore
from osintizada.investigation.correlation import CorrelationEngine, RelationView
from osintizada.investigation.pivot import EntitySnapshot
from osintizada.providers.telegram.telethon_provider import TelegramProvider
from osintizada.resilience import ProviderRuntime
from tests.fixtures.images import avatar, encode

BASE = avatar(1)
PNG = encode(BASE)


def level(a: bytes, b: bytes) -> ImageMatchLevel:
    return compare(analyze_image(a), analyze_image(b)).level


def test_identical_file_is_exact_match():
    fp = analyze_image(PNG)
    assert fp.sha256 == hashlib.sha256(PNG).hexdigest() and fp.mime == "image/png"
    assert level(PNG, PNG) == ImageMatchLevel.EXACT_IMAGE_MATCH


@pytest.mark.parametrize("variant", [
    encode(BASE, "JPEG", quality=30),                  # recomprimida
    encode(BASE.resize((96, 96))),                     # redimensionada
    encode(BASE.resize((400, 400)), "WEBP", quality=60),
])
def test_recompressed_or_resized_is_perceptual_very_similar(variant):
    assert level(PNG, variant) == ImageMatchLevel.PERCEPTUAL_VERY_SIMILAR  # nunca "mesmo arquivo"


def test_mildly_edited_is_at_most_similar():
    edited = encode(BASE.crop((12, 12, 244, 244)))
    assert level(PNG, edited) in (ImageMatchLevel.PERCEPTUAL_SIMILAR, ImageMatchLevel.PERCEPTUAL_VERY_SIMILAR)


def test_distinct_images_are_different():
    for seed in range(2, 15):
        assert level(PNG, encode(avatar(seed))) == ImageMatchLevel.DIFFERENT


def test_exif_orientation_is_normalized_without_touching_original():
    rotated = BASE.rotate(90, expand=True)  # pixels girados…
    exif = Image.Exif()
    exif[0x0112] = 6                         # …e a tag manda girar de volta na exibição
    data = encode(rotated, "JPEG", quality=90, exif=exif.tobytes())
    fp = analyze_image(data)
    assert fp.exif_orientation_applied is True
    assert hamming(fp.phash, analyze_image(PNG).phash) <= 6
    assert fp.sha256 == hashlib.sha256(data).hexdigest()  # hash exato é do arquivo original


def test_invalid_and_oversized_images_are_rejected():
    with pytest.raises(ImageError):
        analyze_image(b"not an image")
    with pytest.raises(ImageError):
        analyze_image(PNG, max_pixels=100)  # proteção contra decompression bomb
    with pytest.raises(ImageError):
        analyze_image(PNG, max_bytes=10)
    with pytest.raises(ImageError, match="Formato"):
        analyze_image(encode(BASE, "TIFF"))


def test_artifact_store_keeps_original_bytes(tmp_path):
    store = ArtifactStore(tmp_path)
    sha = store.put(PNG)
    assert store.get(sha) == PNG and store.path_for(sha).parent.name == sha[:2]
    with pytest.raises(ValueError):
        store.path_for("../../etc/passwd")


# --- correlação por avatar --------------------------------------------------------------


def snap(etype, value, id):
    from osintizada.core.canonical import entity_fingerprint_hash

    return EntitySnapshot(id, etype, value, None, 1, 0.9, "DISCOVERED", entity_fingerprint_hash(etype, value))


def img_entity(id, data):
    fp = analyze_image(data)
    attrs = {"phash": [{"value": fp.phash, "evidence_id": f"ev-{id}"}],
             "dhash": [{"value": fp.dhash, "evidence_id": f"ev-{id}"}]}
    return snap(EntityType.IMAGE, f"sha256:{fp.sha256}", id), attrs


def uses(owner, image, ev):
    return RelationView(f"r-{owner}-{image}", owner, image, RelationType.USES.value, [ev])


def test_exact_avatar_alone_never_confirms_identity():
    a = snap(EntityType.TELEGRAM_USER, "1001", "A")
    b = snap(EntityType.TELEGRAM_USER, "1002", "B")
    image, attrs = img_entity("I", PNG)
    [res] = CorrelationEngine(Settings()).correlate([a, b, image], [uses("A", "I", "e1"), uses("B", "I", "e2")],
                                                    {"I": attrs})
    assert [s.name for s in res.positive_signals] == ["same_exact_avatar"]
    assert res.level == CorrelationLevel.WEAK and res.relation_type is None  # avatar sozinho: nunca SAME_AS


def test_avatar_plus_rare_username_is_at_most_possible():
    s = Settings()
    a = snap(EntityType.SOCIAL_ACCOUNT, "github:darkwolf_1988", "A")
    b = snap(EntityType.SOCIAL_ACCOUNT, "twitter:darkwolf_1988", "B")
    image, attrs = img_entity("I", PNG)
    [res] = CorrelationEngine(s).correlate([a, b, image], [uses("A", "I", "e1"), uses("B", "I", "e2")], {"I": attrs})
    assert {x.name for x in res.positive_signals} == {"same_username_rare", "same_exact_avatar"}
    assert res.level == CorrelationLevel.POSSIBLE and res.relation_type == RelationType.POSSIBLY_SAME_AS


def test_perceptual_avatar_signal_weaker_than_exact():
    s = Settings()
    a = snap(EntityType.TELEGRAM_USER, "1001", "A")
    b = snap(EntityType.TELEGRAM_USER, "1002", "B")
    img1, attrs1 = img_entity("I1", PNG)
    img2, attrs2 = img_entity("I2", encode(BASE, "JPEG", quality=40))
    [res] = CorrelationEngine(s).correlate([a, b, img1, img2], [uses("A", "I1", "e1"), uses("B", "I2", "e2")],
                                           {"I1": attrs1, "I2": attrs2})
    [signal] = res.positive_signals
    assert signal.name == "very_similar_avatar" and signal.evidence_ids == ["e1", "e2"]
    assert signal.weight < s.correlation.weights["same_exact_avatar"]
    img3, attrs3 = img_entity("I3", encode(avatar(7)))
    assert CorrelationEngine(s).correlate([a, b, img1, img3], [uses("A", "I1", "e1"), uses("B", "I3", "e3")],
                                          {"I1": attrs1, "I3": attrs3}) == []


def test_generic_avatar_reused_widely_has_low_identity_value():
    s = Settings()
    owners = [snap(EntityType.TELEGRAM_USER, str(1000 + i), f"U{i}") for i in range(3)]
    image, attrs = img_entity("I", PNG)
    rels = [uses(o.id, "I", f"e{i}") for i, o in enumerate(owners)]
    engine = CorrelationEngine(s)
    assert "reutilizada por 3" in engine.low_identity_images(owners + [image], rels)["I"]
    results = engine.correlate(owners + [image], rels, {"I": attrs})
    assert results and all(r.positive_signals[0].weight == 5 for r in results)  # 20 × 0.25
    assert all("LOW_IDENTITY_VALUE" in r.positive_signals[0].detail for r in results)


def test_manual_low_identity_flag_and_known_generic_hash():
    s = Settings()
    a, b = snap(EntityType.TELEGRAM_USER, "1", "A"), snap(EntityType.TELEGRAM_USER, "2", "B")
    image, attrs = img_entity("I", PNG)
    rels = [uses("A", "I", "e1"), uses("B", "I", "e2")]
    flags = {"I": {"low_identity_value": {"reason": "logo de empresa"}}}
    [res] = CorrelationEngine(s).correlate([a, b, image], rels, {"I": attrs}, flags=flags)
    assert res.positive_signals[0].weight == 5 and "logo de empresa" in res.positive_signals[0].detail
    s.images.known_generic_sha256 = [image.canonical_value.removeprefix("sha256:")]
    assert CorrelationEngine(s).low_identity_images([a, b, image], rels) == {"I": "avatar genérico conhecido"}


# --- proveniência do avatar no Telegram ---------------------------------------------------


class Photo:
    photo_id = 555


class User:
    def __init__(self, id, username):
        self.id, self.username, self.first_name, self.photo = id, username, "Nome", Photo()


class AvatarClient:
    def __init__(self, entities, photo: bytes):
        self.entities, self.photo = entities, photo

    async def get_entity(self, key):
        if key in self.entities:
            return self.entities[key]
        raise ValueError("No user")

    async def download_profile_photo(self, entity, file=bytes):
        return self.photo

    async def disconnect(self):
        return None


async def test_telegram_avatar_provenance(tmp_path):
    settings = Settings()
    settings.jobs.artifact_dir = str(tmp_path)
    rt = ProviderRuntime()
    rt.telegram_client_factory = lambda: AvatarClient({"alvo_2024": User(777, "alvo_2024")}, PNG)
    resp = await TelegramProvider(settings, rt).search(normalize("@alvo_2024", IdentifierType.TELEGRAM_USERNAME))
    image = next(r for r in resp.results if r.type == EntityType.IMAGE)
    fp = analyze_image(PNG)
    assert image.value == f"sha256:{fp.sha256}" and image.attributes["phash"] == fp.phash
    assert image.source_entity.value == "777" and image.relation_to_query == RelationType.USES
    assert image.raw["telegram_photo_id"] == 555 and image.observed_at is not None
    assert ArtifactStore(tmp_path).get(fp.sha256) == PNG


async def test_avatar_correlation_persisted_end_to_end(tmp_path, monkeypatch):
    """Dois IDs Telegram distintos com o MESMO avatar: correlação WEAK registrada, sem SAME_AS."""
    from osintizada.repositories import CorrelationRepository, EntityRepository, RelationshipRepository
    from tests.fixtures.environment import build_service

    for var in ("BRAVE_SEARCH_API_KEY",):
        monkeypatch.delenv(var, raising=False)
    settings = Settings()
    settings.jobs.artifact_dir = str(tmp_path)
    service, db, runtime = build_service(settings)
    runtime.telegram_client_factory = lambda: AvatarClient(
        {"perfil_um": User(1001, "perfil_um"), "perfil_dois": User(1002, "perfil_dois")}, PNG)
    from osintizada.investigation.service import InvestigationRequest

    case_id = service.create_case("avatar")
    await service.investigate(case_id, InvestigationRequest(inputs=["@perfil_um", "@perfil_dois"], mode="quick",
                                                            max_depth=0))
    with db.session() as s:
        images = EntityRepository(s).list(case_id, EntityType.IMAGE)
        assert len(images) == 1  # mesmo SHA256 → UMA entidade IMAGE, duas evidências de uso
        [corr] = CorrelationRepository(s).list(case_id)
        assert corr.level == "WEAK" and corr.positive_signals[0]["signal"] == "same_exact_avatar"
        assert corr.relationship_id is None
        rel_types = {r.relationship_type for r in RelationshipRepository(s).list(case_id)}
        assert "SAME_AS" not in rel_types and "POSSIBLY_SAME_AS" not in rel_types
