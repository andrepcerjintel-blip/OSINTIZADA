"""PrivacyGate: redação de segredos (antes de QUALQUER IA) e decisão sobre o que pode ir à nuvem.

1. ``redact`` — remove de todo conteúdo enviado a qualquer modelo (inclusive o local): API keys, tokens,
   cookies, strings de sessão, senhas, cabeçalhos Authorization, JWT, chaves privadas.
2. ``cloud_decision`` — decide se aquele material pode sair do ambiente local. Categorias
   (``ai.privacy.never_send_to_cloud``):
     * restricted_sources         — itens de providers listados em ``ai.privacy.restricted_sources``;
     * credentials / raw_auth_tokens — o material continha credenciais/tokens (mesmo já redigidos,
       o conjunto não sai da máquina);
     * local_files_marked_private — itens marcados ``private`` (entidade/evidência/Case).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from osintizada.core.secrets import sanitize_payload

REDACTED = "[REDACTED]"

_EXTRA_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("raw_auth_tokens", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),       # JWT
    ("credentials", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("credentials", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),                                        # AWS
    ("raw_auth_tokens", re.compile(r"\b(?:sk-(?:ant-)?|ghp_|gho_|github_pat_|xox[baprs]-|glpat-)[A-Za-z0-9_-]{10,}")),
    ("raw_auth_tokens", re.compile(r"(?i)\b(?:cookie|set-cookie)\s*:\s*[^\n]+")),
    ("raw_auth_tokens", re.compile(r"(?i)\b(?:session(?:_?string)?|sessionid|sid)\s*[=:]\s*\S{12,}")),
    ("credentials", re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|senha|authorization|bearer)"
                               r"\s*[=:]\s*(?:bearer\s+)?)(\S+)")),
    ("raw_auth_tokens", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("raw_auth_tokens", re.compile(r"(?:[?&])(?:key|apikey|api_key|token|access_token)=[^&\s]+")),
]


@dataclass
class Redaction:
    payload: dict[str, Any]
    categories: set[str] = field(default_factory=set)
    count: int = 0


@dataclass
class CloudDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


class PrivacyGate:
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def _redact_text(self, text: str, found: Redaction) -> str:
        for category, pattern in _EXTRA_PATTERNS:
            def repl(m, category=category):
                found.categories.add(category)
                found.count += 1
                return (m.group(1) + REDACTED) if m.re.groups else REDACTED
            text = pattern.sub(repl, text)
        return text

    def redact(self, payload: dict[str, Any]) -> Redaction:
        """Cópia do payload sem segredos (estrutura preservada; chaves com nome de credencial esvaziadas)."""
        found = Redaction(payload={})

        def walk(value: Any) -> Any:
            if isinstance(value, str):
                return self._redact_text(value, found)
            if isinstance(value, dict):
                return {k: walk(v) for k, v in value.items()}
            if isinstance(value, list | tuple):
                return [walk(v) for v in value]
            return value

        before = sanitize_payload(payload)   # chaves "password", "token"… (mesma regra do resto do RINO)
        if before != payload:
            found.categories.add("credentials")
            found.count += 1
        found.payload = walk(before)
        return found

    def cloud_decision(self, meta: dict[str, Any], redaction: Redaction) -> CloudDecision:
        never = set(self.cfg.never_send_to_cloud)
        reasons = []
        restricted = set(self.cfg.restricted_sources)
        if "restricted_sources" in never and restricted & set(meta.get("sources") or []):
            reasons.append(f"fontes restritas no material: {sorted(restricted & set(meta['sources']))}")
        if "local_files_marked_private" in never and meta.get("private"):
            reasons.append("material marcado como privado")
        for category in ("credentials", "raw_auth_tokens"):
            if category in never and category in redaction.categories:
                reasons.append(f"material continha {category} (redigidos; o conjunto não sai da máquina)")
        return CloudDecision(allowed=not reasons, reasons=reasons)
