"""Download seguro de imagens (avatares) de URLs descobertas.

Usa o SafeHTTPClient central (SSRF + pinning + limite de tamanho) e valida o conteúdo
pela decodificação real (não confia no Content-Type).
"""

from __future__ import annotations

from osintizada.images.hashing import ImageError, ImageFingerprint, analyze_image
from osintizada.net.http_client import SafeHTTPClient


async def fetch_image(client: SafeHTTPClient, url: str, max_bytes: int, max_pixels: int
                      ) -> tuple[bytes, ImageFingerprint, str]:
    result = await client.get(url, headers={"Accept": "image/*"})
    if result.status_code != 200:
        raise ImageError(f"HTTP {result.status_code} ao baixar imagem")
    ctype = result.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype and not ctype.startswith("image/"):
        raise ImageError(f"Conteúdo não é imagem ({ctype})")
    fingerprint = analyze_image(result.content, max_bytes=max_bytes, max_pixels=max_pixels)
    return result.content, fingerprint, result.url
