"""Métricas internas sem dependência externa, exportáveis em formato Prometheus.

Contadores e histogramas vivem em memória do processo e, quando um Redis é
registrado (``metrics.bind_redis``), os contadores também são incrementados no Redis
para agregação entre processos (API + workers). Métricas de jobs são calculadas do banco
no momento da coleta (ver ``/metrics``).
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict

log = logging.getLogger("osintizada.metrics")

DEFAULT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300, 900, 1800)


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[str, float] = defaultdict(float)
        self.histograms: dict[str, dict] = {}
        self._redis = None
        self._prefix = "osintizada:metrics"

    def bind_redis(self, client, prefix: str = "osintizada") -> None:
        self._redis = client
        self._prefix = f"{prefix}:metrics"

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = _key(name, labels)
        with self._lock:
            self.counters[key] += value
        if self._redis is not None:
            try:
                self._redis.hincrbyfloat(self._prefix, key, value)
            except Exception:  # noqa: BLE001 - métrica nunca derruba a aplicação
                pass

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = _key(name, labels)
        with self._lock:
            hist = self.histograms.setdefault(key, {"count": 0, "sum": 0.0,
                                                    "buckets": {b: 0 for b in DEFAULT_BUCKETS}})
            hist["count"] += 1
            hist["sum"] += value
            for bound in DEFAULT_BUCKETS:
                if value <= bound:
                    hist["buckets"][bound] += 1

    def shared_counters(self) -> dict[str, float]:
        if self._redis is None:
            return dict(self.counters)
        try:
            raw = self._redis.hgetall(self._prefix)
            return {k.decode() if isinstance(k, bytes) else k: float(v) for k, v in raw.items()}
        except Exception:  # noqa: BLE001
            return dict(self.counters)

    def render(self, extra_gauges: dict[str, float] | None = None) -> str:
        """Formato de exposição Prometheus 0.0.4: uma família por nome, com ``# TYPE``.

        Contadores recebem o sufixo ``_total``; gauges (estado atual, ex.: ``jobs{status="RUNNING"}``)
        nunca compartilham nome com contadores.
        """
        families: dict[str, tuple[str, list[str]]] = {}

        def add(name: str, kind: str, line: str) -> None:
            families.setdefault(name, (kind, []))[1].append(line)

        for key, value in sorted(self.shared_counters().items()):
            name, labels = _split(key)
            name = name if name.endswith("_total") else f"{name}_total"
            add(name, "counter", f"osintizada_{name}{_labels(labels)} {value}")
        for key, hist in sorted(self.histograms.items()):
            name, labels = _split(key)
            for bound, count in hist["buckets"].items():
                add(name, "histogram", f"osintizada_{name}_bucket{_labels(labels, le=str(bound))} {count}")
            add(name, "histogram", f"osintizada_{name}_bucket{_labels(labels, le='+Inf')} {hist['count']}")
            add(name, "histogram", f"osintizada_{name}_sum{_labels(labels)} {hist['sum']}")
            add(name, "histogram", f"osintizada_{name}_count{_labels(labels)} {hist['count']}")
        for key, value in sorted((extra_gauges or {}).items()):
            name, labels = _split(key)
            add(name, "gauge", f"osintizada_{name}{_labels(labels)} {value}")
        lines = []
        for name, (kind, rows) in families.items():
            lines.append(f"# TYPE osintizada_{name} {kind}")
            lines.extend(rows)
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self.counters.clear()
            self.histograms.clear()


def _key(name: str, labels: dict[str, str]) -> str:
    if not labels:
        return name
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return f"{name}{{{inner}}}"


def _split(key: str) -> tuple[str, str]:
    if "{" in key:
        name, rest = key.split("{", 1)
        return name, rest.rstrip("}")
    return key, ""


def _labels(existing: str, **extra: str) -> str:
    parts = [existing] if existing else []
    parts += [f'{k}="{v}"' for k, v in extra.items()]
    return "{" + ",".join(parts) + "}" if parts else ""


metrics = Metrics()
