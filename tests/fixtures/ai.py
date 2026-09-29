"""Servidores de IA simulados (sem rede): Ollama, servidor OpenAI-compatível e API da Anthropic."""

from __future__ import annotations

import json
import re
from collections.abc import Callable

import httpx

Responder = Callable[[str, str], str]  # (system, user) → conteúdo da resposta


def task_of(system: str) -> str:
    for key, name in (("Classifique", "CLASSIFY_CONTENT"), ("Extraia", "EXTRACT_ENTITIES"), ("Resuma", "SUMMARIZE"),
                      ("Traduza", "TRANSLATE"), ("Sugira", "GENERATE_QUERIES"), ("Objetivo do caso", "ASSESS_RELEVANCE")):
        if key in system:
            return name
    return "?"


def ids_in(user: str) -> list[str]:
    return re.findall(r'"id": "([0-9a-f]{32}(?:::\d+)?|t\d+)"', user)


def default_responder(system: str, user: str) -> str:
    """Respostas determinísticas por tarefa (inclui ids/valores inventados, para testar a validação)."""
    task, ids = task_of(system), ids_in(user)
    if task == "SUMMARIZE":
        return json.dumps({"summary": "O domínio aponta para infraestrutura observada.", "key_points": ["A", "B"],
                           "cited_ids": ids[:2] + ["ffffffffffffffffffffffffffffffff"]})
    if task == "GENERATE_QUERIES":
        return json.dumps({"queries": [{"query": '"example.com" site:github.com', "reason": "código público",
                                        "based_on": ids[:1] + ["id-inventado"]},
                                       {"query": "example.com", "reason": "repetida", "based_on": []}]})
    if task == "EXTRACT_ENTITIES":
        return json.dumps({"entities": [
            {"type": "EMAIL", "value": "contato@example.com", "confidence": 0.9, "context": "Fale com",
             "source_id": ids[0] if ids else None},
            {"type": "PERSON", "value": "Maria Silva", "confidence": 0.7, "context": "responsável",
             "source_id": ids[0] if ids else None},
            {"type": "EMAIL", "value": "inventado@naoexiste.com", "confidence": 0.9},
            {"type": "PHONE", "value": "contato", "confidence": 0.5, "source_id": ids[0] if ids else None}]})
    if task == "ASSESS_RELEVANCE":
        scores = [0.9, 0.5, 0.1]
        return json.dumps({"items": [{"id": i, "relevant": scores[n % 3] >= 0.5, "score": scores[n % 3],
                                      "reason": "cita o alvo"} for n, i in enumerate(ids)]
                          + [{"id": "id-inventado", "relevant": True, "score": 1.0, "reason": "x"}]})
    if task == "CLASSIFY_CONTENT":
        return json.dumps({"items": [{"id": i, "label": "infraestrutura", "confidence": 0.8, "reason": "dns"}
                                     for i in ids] + [{"id": ids[0] if ids else "t1", "label": "inventado",
                                                        "confidence": 1, "reason": "x"}]})
    if task == "TRANSLATE":
        return json.dumps({"items": [{"id": i, "source_language": "en", "translation": f"tradução de {i[:6]}"}
                                     for i in ids]})
    return "{}"


class FakeOllama:
    """/api/tags e /api/chat. Registra todas as requisições (para provar que nada foi baixado)."""

    def __init__(self, models=("llama3.1:8b",), responder: Responder = default_responder, down: bool = False,
                 timeout: bool = False, fail_first: int = 0) -> None:
        self.models, self.responder, self.down, self.timeout = list(models), responder, down, timeout
        self.fail_first = fail_first      # nº de respostas HTTP 503 antes de responder (erro transitório)
        self.requests: list[tuple[str, str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, body))
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if self.timeout and request.url.path == "/api/chat":
            raise httpx.ReadTimeout("timeout", request=request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        if request.url.path == "/api/chat":
            if self.fail_first > 0:
                self.fail_first -= 1
                return httpx.Response(503, json={"error": "sobrecarregado"})
            if body["model"] not in self.models and f"{body['model']}:latest" not in self.models:
                return httpx.Response(404, json={"error": "model not found"})
            system, user = body["messages"][0]["content"], body["messages"][1]["content"]
            return httpx.Response(200, json={"message": {"role": "assistant", "content": self.responder(system, user)},
                                             "done": True, "prompt_eval_count": 100, "eval_count": 20})
        return httpx.Response(404)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    @property
    def chat_calls(self) -> int:
        return sum(1 for _, path, _ in self.requests if path == "/api/chat")


class FakeOpenAICompatible:
    """/v1/models e /v1/chat/completions (llama.cpp, LM Studio, vLLM, OpenAI)."""

    def __init__(self, models=("local-model",), responder: Responder = default_responder) -> None:
        self.models, self.responder = list(models), responder
        self.requests: list[tuple[str, str, dict, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, body, dict(request.headers)))
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
        if request.url.path.startswith("/v1/models/"):
            model = request.url.path.rsplit("/", 1)[1]
            return httpx.Response(200 if model in self.models else 404, json={"id": model})
        if request.url.path == "/v1/chat/completions":
            if body["model"] not in self.models:
                return httpx.Response(404, json={"error": {"message": "model not found"}})
            content = self.responder(body["messages"][0]["content"], body["messages"][1]["content"])
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})
        return httpx.Response(404)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class FakeAnthropic:
    """API da Anthropic simulada para o SDK oficial (transporte httpx2)."""

    def __init__(self, responder: Responder = default_responder, refuse: bool = False) -> None:
        self.responder, self.refuse = responder, refuse
        self.requests: list[tuple[str, str, dict, dict]] = []

    def handler(self, request):
        import httpx2

        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, body, dict(request.headers)))
        if request.url.path.startswith("/v1/models/"):
            model = request.url.path.rsplit("/", 1)[1]
            return httpx2.Response(200, json={"id": model, "type": "model", "display_name": model,
                                              "created_at": "2026-01-01T00:00:00Z"})
        if request.url.path == "/v1/messages":
            if self.refuse:
                return httpx2.Response(200, json={
                    "id": "msg_r", "type": "message", "role": "assistant", "model": body["model"], "content": [],
                    "stop_reason": "refusal", "stop_sequence": None,
                    "stop_details": {"type": "refusal", "category": "cyber", "explanation": "x"},
                    "usage": {"input_tokens": 1, "output_tokens": 0}})
            text = self.responder(body["system"], body["messages"][0]["content"])
            return httpx2.Response(200, json={
                "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 10}})
        return httpx2.Response(404, json={"type": "error", "error": {"type": "not_found_error", "message": "x"}})

    def http_client(self):
        import anthropic
        import httpx2

        return anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self.handler))

    @property
    def message_calls(self) -> int:
        return sum(1 for _, path, _, _ in self.requests if path == "/v1/messages")
