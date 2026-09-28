"""Hashes de imagem para correlação de avatares.

* ``sha256`` — dos BYTES ORIGINAIS: identifica o MESMO ARQUIVO (EXACT_IMAGE_MATCH);
* ``phash`` — hash perceptual por DCT (64 bits): robusto a recompressão, resize e leve ajuste de cor;
* ``dhash`` — hash de diferenças (64 bits): confirma o pHash (dois algoritmos concordando reduz falso positivo).

Normalização antes dos hashes perceptuais (em uma CÓPIA derivada; os bytes originais e a
Evidence nunca são alterados): orientação EXIF corrigida, transparência achatada sobre
branco, escala de cinza, redimensionamento fixo do algoritmo. Metadata não entra no hash visual.
"""

from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import asdict, dataclass
from enum import Enum

import numpy as np
from PIL import Image, ImageOps

ALLOWED_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp",
                   "BMP": "image/bmp"}


class ImageError(ValueError):
    """Imagem inválida, grande demais ou formato não suportado."""


class ImageMatchLevel(str, Enum):
    EXACT_IMAGE_MATCH = "EXACT_IMAGE_MATCH"                  # mesmo arquivo (SHA256)
    PERCEPTUAL_VERY_SIMILAR = "PERCEPTUAL_VERY_SIMILAR"      # visualmente quase idêntica
    PERCEPTUAL_SIMILAR = "PERCEPTUAL_SIMILAR"                # semelhante (sinal fraco)
    DIFFERENT = "DIFFERENT"


@dataclass(frozen=True)
class ImageFingerprint:
    sha256: str
    phash: str
    dhash: str
    width: int
    height: int
    mime: str
    size_bytes: int
    exif_orientation_applied: bool

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ImageMatch:
    level: ImageMatchLevel
    phash_distance: int
    dhash_distance: int


def _dct_matrix(n: int) -> np.ndarray:
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    matrix = np.cos(np.pi * (2 * i + 1) * k / (2 * n)) * np.sqrt(2 / n)
    matrix[0, :] = np.sqrt(1 / n)
    return matrix


_DCT32 = _dct_matrix(32)


def _bits_to_hex(bits: np.ndarray) -> str:
    value = 0
    for bit in bits.flatten():
        value = (value << 1) | int(bool(bit))
    return f"{value:016x}"


def _normalized_gray(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(background, img)
    return img.convert("L")


def phash(gray: Image.Image) -> str:
    pixels = np.asarray(gray.resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float64)
    dct = _DCT32 @ pixels @ _DCT32.T
    low = dct[:8, :8]
    median = np.median(low.flatten()[1:])  # exclui o componente DC (brilho médio)
    return _bits_to_hex(low > median)


def dhash(gray: Image.Image) -> str:
    pixels = np.asarray(gray.resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
    return _bits_to_hex(pixels[:, 1:] > pixels[:, :-1])


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def analyze_image(data: bytes, max_bytes: int = 5 * 1024 * 1024, max_pixels: int = 40_000_000) -> ImageFingerprint:
    if not data:
        raise ImageError("Imagem vazia")
    if len(data) > max_bytes:
        raise ImageError(f"Imagem maior que {max_bytes} bytes")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(data))
            fmt = img.format or ""
            if fmt not in ALLOWED_FORMATS:
                raise ImageError(f"Formato não suportado: {fmt or 'desconhecido'}")
            if img.width * img.height > max_pixels:
                raise ImageError("Imagem com pixels demais (possível decompression bomb)")
            img.load()
    except ImageError:
        raise
    except Exception as exc:  # noqa: BLE001 - bytes arbitrários de fonte externa
        raise ImageError(f"Imagem inválida: {type(exc).__name__}") from exc
    orientation = (img.getexif() or {}).get(0x0112, 1)
    gray = _normalized_gray(img)
    return ImageFingerprint(
        sha256=hashlib.sha256(data).hexdigest(), phash=phash(gray), dhash=dhash(gray), width=gray.width,
        height=gray.height, mime=ALLOWED_FORMATS[fmt], size_bytes=len(data),
        exif_orientation_applied=orientation not in (None, 1),
    )


def compare(a: dict | ImageFingerprint, b: dict | ImageFingerprint, very_similar: int = 6, similar: int = 12,
            dhash_confirm: int = 12) -> ImageMatch:
    a = a.as_dict() if isinstance(a, ImageFingerprint) else a
    b = b.as_dict() if isinstance(b, ImageFingerprint) else b
    p = hamming(a["phash"], b["phash"])
    d = hamming(a["dhash"], b["dhash"])
    if a.get("sha256") and a.get("sha256") == b.get("sha256"):
        return ImageMatch(ImageMatchLevel.EXACT_IMAGE_MATCH, p, d)
    if p <= very_similar and d <= dhash_confirm:
        return ImageMatch(ImageMatchLevel.PERCEPTUAL_VERY_SIMILAR, p, d)
    if p <= similar:
        return ImageMatch(ImageMatchLevel.PERCEPTUAL_SIMILAR, p, d)
    return ImageMatch(ImageMatchLevel.DIFFERENT, p, d)
