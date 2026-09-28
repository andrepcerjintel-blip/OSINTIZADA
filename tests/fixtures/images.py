"""Gerador determinístico de avatares sintéticos para testes."""

from __future__ import annotations

import io
import random

from PIL import Image, ImageDraw


def avatar(seed: int, size: int = 256) -> Image.Image:
    rnd = random.Random(seed)
    img = Image.new("RGB", (size, size), (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)))
    draw = ImageDraw.Draw(img)
    for _ in range(12):
        x0, y0 = rnd.randint(0, size - 40), rnd.randint(0, size - 40)
        draw.ellipse([x0, y0, x0 + rnd.randint(20, 120), y0 + rnd.randint(20, 120)],
                     fill=(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)))
    return img


def encode(img: Image.Image, fmt: str = "PNG", **kwargs) -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt, **kwargs)
    return buf.getvalue()
