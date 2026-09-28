"""Construtor de consultas com operadores de busca.

Mantém a montagem de operadores (``site:``, ``filetype:`` etc.) em um único
lugar para que cada SearchProvider possa, futuramente, traduzir/remover
operadores que o seu mecanismo não suporta.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SUPPORTED_OPERATORS = ("site", "filetype", "inurl", "intitle", "exclude", "or", "quote")

_OPERATOR_RE = re.compile(r"(?<![\w-])(-?)(site|filetype|inurl|intitle):", re.I)


def quote(term: str) -> str:
    """Envolve o termo em aspas, escapando aspas internas."""
    return '"' + term.replace('"', "") + '"'


def detect_operators(query: str) -> list[str]:
    ops: set[str] = set()
    for neg, name in _OPERATOR_RE.findall(query):
        ops.add(name.lower())
        if neg:
            ops.add("exclude")
    if '"' in query:
        ops.add("quote")
    if re.search(r"\sOR\s", query):
        ops.add("or")
    if re.search(r"(?:^|\s)-[\w\"]", query):
        ops.add("exclude")
    return sorted(ops)


@dataclass
class QueryBuilder:
    _parts: list[str] = field(default_factory=list)

    def exact(self, term: str) -> QueryBuilder:
        self._parts.append(quote(term))
        return self

    def word(self, term: str) -> QueryBuilder:
        self._parts.append(term)
        return self

    def site(self, domain: str) -> QueryBuilder:
        self._parts.append(f"site:{domain}")
        return self

    def filetype(self, ext: str) -> QueryBuilder:
        self._parts.append(f"filetype:{ext}")
        return self

    def inurl(self, term: str) -> QueryBuilder:
        self._parts.append(f"inurl:{term}")
        return self

    def intitle(self, term: str) -> QueryBuilder:
        self._parts.append(f"intitle:{quote(term) if ' ' in term else term}")
        return self

    def exclude(self, term: str) -> QueryBuilder:
        self._parts.append(f"-{term}")
        return self

    def exclude_site(self, domain: str) -> QueryBuilder:
        self._parts.append(f"-site:{domain}")
        return self

    def any_of(self, *terms: str) -> QueryBuilder:
        self._parts.append("(" + " OR ".join(terms) + ")")
        return self

    def build(self) -> str:
        return " ".join(self._parts)
