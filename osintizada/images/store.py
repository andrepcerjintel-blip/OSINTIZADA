"""ArtifactStore: guarda os BYTES ORIGINAIS de artefatos (ex.: avatares) endereçados por SHA256.

O arquivo original nunca é alterado; análises trabalham sobre cópias derivadas em memória.
O caminho é derivado exclusivamente do hash (sem nome vindo de fonte externa → sem path traversal).
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path_for(self, sha256: str) -> Path:
        if not _HEX64.match(sha256):
            raise ValueError("sha256 inválido")
        return self.root / sha256[:2] / sha256

    def put(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        path = self.path_for(sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return sha

    def get(self, sha256: str) -> bytes | None:
        path = self.path_for(sha256)
        return path.read_bytes() if path.exists() else None
