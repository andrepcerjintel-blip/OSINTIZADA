"""Controle de execução: interface do Core para cooperar com quem o executa.

O Core (InvestigationService) NÃO conhece Redis, fila ou worker. Ele apenas chama esta
interface; o worker fornece uma implementação concreta (``osintizada.jobs.control``).
Sem worker (CLI/testes), ``NullControl`` mantém o comportamento síncrono original.
"""

from __future__ import annotations

from typing import Any


class InvestigationCancelled(Exception):
    """Cancelamento cooperativo solicitado (ou posse do job perdida)."""


class ExecutionControl:
    def is_cancelled(self) -> bool:
        return False

    def check_cancelled(self) -> None:
        if self.is_cancelled():
            raise InvestigationCancelled("Cancelamento solicitado")

    def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Evento de progresso (PROVIDER_STARTED, ENTITY_CREATED, …)."""

    def progress(self, stage: str, **fields: Any) -> None:
        """Estado aproximado de progresso (não finge precisão)."""

    def checkpoint(self, name: str, state: dict[str, Any]) -> None:
        """Persiste estado suficiente para retomar o trabalho pendente."""

    def load_checkpoint(self) -> dict[str, Any] | None:
        return None


class NullControl(ExecutionControl):
    """Execução direta (sem job): nada a coordenar."""
