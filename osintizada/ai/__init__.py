"""Camada de IA auxiliar do RINO (Llama local, Claude, OpenAI e outros futuros).

A IA INTERPRETA evidência; nunca a produz. O Core (investigação, evidências, correlação) não importa
este pacote: ele é usado por jobs/API de análise, e cada resultado vira uma anotação rastreável.
"""

from osintizada.ai.base import AIProvider
from osintizada.ai.models import AIMode, AIProviderStatus, AIResult, AIResultStatus, AITask
from osintizada.ai.router import AIRouter, build_ai_router, effective_ai_settings

__all__ = ["AIMode", "AIProvider", "AIProviderStatus", "AIResult", "AIResultStatus", "AIRouter", "AITask",
           "build_ai_router", "effective_ai_settings"]
