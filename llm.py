"""Backends de LLM — o 'cérebro' que lê o pedido e decide qual tool chamar.

Dois modos, selecionáveis no config.yaml:
- anthropic: API da Anthropic (paga, mais confiável no roteamento).
- ollama: modelo local via Ollama (grátis, roda na sua máquina).

Ambos rodam o mesmo loop de tool-use; só muda o formato de fio. As tools
chegam num formato neutro ({name, description, input_schema}) e cada backend
converte pro seu protocolo. A execução da tool em si fica a cargo do
orquestrador, passado aqui como `dispatch`."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

import anthropic
import httpx

# (nome_prefixado, argumentos) -> texto do resultado da tool
Dispatch = Callable[[str, dict[str, Any]], Awaitable[str]]

_TOO_MANY_TURNS = "Muitas idas e vindas nessa tarefa — quebra em partes menores?"


def _short_error(exc: BaseException) -> str:
    """Extrai uma mensagem legível de exceções aninhadas/embrulhadas."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:  # noqa: F821 (builtin 3.11+)
        exc = exc.exceptions[0]
    text = str(exc).strip()
    return text or exc.__class__.__name__


class AnthropicBackend:
    """Cérebro via API da Anthropic. Cliente criado sob demanda: a chave só
    é exigida ao enviar o primeiro comando, não pra conectar nos MCP."""

    def __init__(
        self,
        api_key: str | None,
        model: str,
        max_tokens: int,
        max_tool_turns: int,
        system_prompt: str,
    ):
        self._api_key = api_key
        self._client: anthropic.Anthropic | None = None
        self.model = model
        self.max_tokens = max_tokens
        self.max_tool_turns = max_tool_turns
        self.system_prompt = system_prompt

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    async def run(
        self, user_text: str, tools: list[dict[str, Any]], dispatch: Dispatch
    ) -> str:
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_text}]

        for _ in range(self.max_tool_turns):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=self.system_prompt,
                    tools=tools,
                    messages=messages,
                )
            except anthropic.AuthenticationError:
                return (
                    "Erro de autenticação (401): a ANTHROPIC_API_KEY é inválida "
                    "ou está ausente. Confira a chave em console.anthropic.com e "
                    "defina-a num terminal novo."
                )
            except anthropic.APIStatusError as exc:
                return f"A API respondeu com erro {exc.status_code}: {_short_error(exc)}"
            except anthropic.APIConnectionError:
                return "Não consegui falar com a API da Anthropic — verifique a conexão."
            except anthropic.AnthropicError as exc:
                return f"Falha ao chamar a API: {_short_error(exc)}"

            if response.stop_reason != "tool_use":
                return "".join(
                    block.text for block in response.content if block.type == "text"
                ).strip()

            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result_text = await dispatch(block.name, block.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        return _TOO_MANY_TURNS


class OllamaBackend:
    """Cérebro local via Ollama (endpoint /api/chat). Grátis, roda na
    máquina. Precisa de um modelo com suporte a tool-calling (ex:
    qwen2.5, llama3.1) e do servidor de pé (`ollama serve`)."""

    def __init__(
        self,
        model: str,
        max_tool_turns: int,
        system_prompt: str,
        host: str = "http://localhost:11434",
        timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.model = model
        self.max_tool_turns = max_tool_turns
        self.system_prompt = system_prompt
        self.host = (host or "http://localhost:11434").rstrip("/")
        self.timeout = timeout
        self._transport = transport  # injetável para teste

    def _to_ollama_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema")
                    or {"type": "object", "properties": {}},
                },
            }
            for t in tools
        ]

    @staticmethod
    def _coerce_args(args: Any) -> dict[str, Any]:
        if isinstance(args, dict):
            return args
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}

    async def run(
        self, user_text: str, tools: list[dict[str, Any]], dispatch: Dispatch
    ) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_text},
        ]
        payload_tools = self._to_ollama_tools(tools)

        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport
        ) as http:
            for _ in range(self.max_tool_turns):
                try:
                    resp = await http.post(
                        f"{self.host}/api/chat",
                        json={
                            "model": self.model,
                            "messages": messages,
                            "tools": payload_tools,
                            "stream": False,
                        },
                    )
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    body = exc.response.text[:200]
                    return (
                        f"O Ollama respondeu com erro {exc.response.status_code}: {body}. "
                        f"O modelo '{self.model}' foi baixado? (ollama pull {self.model})"
                    )
                except httpx.HTTPError as exc:
                    return (
                        f"Não consegui falar com o Ollama em {self.host} "
                        f"({exc.__class__.__name__}). O servidor está rodando? "
                        f"Rode 'ollama serve' e 'ollama pull {self.model}'."
                    )

                message = resp.json().get("message") or {}
                tool_calls = message.get("tool_calls") or []

                if not tool_calls:
                    return (message.get("content") or "").strip()

                messages.append(message)  # turno do assistente com os tool_calls
                for call in tool_calls:
                    fn = call.get("function") or {}
                    name = fn.get("name", "")
                    args = self._coerce_args(fn.get("arguments"))
                    result_text = await dispatch(name, args)
                    messages.append(
                        {"role": "tool", "name": name, "content": result_text}
                    )

        return _TOO_MANY_TURNS


def make_backend(llm_config: dict[str, Any], system_prompt: str):
    """Constrói o backend a partir do bloco `llm` do config."""
    provider = (llm_config.get("provider") or "anthropic").lower()
    max_tool_turns = llm_config.get("max_tool_turns", 6)

    if provider == "anthropic":
        return AnthropicBackend(
            api_key=None,  # usa a variável de ambiente ANTHROPIC_API_KEY
            model=llm_config["model"],
            max_tokens=llm_config.get("max_tokens", 1024),
            max_tool_turns=max_tool_turns,
            system_prompt=system_prompt,
        )

    if provider == "ollama":
        ollama_cfg = llm_config.get("ollama") or {}
        return OllamaBackend(
            model=ollama_cfg.get("model", "qwen2.5:7b"),
            max_tool_turns=max_tool_turns,
            system_prompt=system_prompt,
            host=ollama_cfg.get("host", "http://localhost:11434"),
            timeout=ollama_cfg.get("timeout", 300.0),
        )

    raise ValueError(
        f"provider de LLM desconhecido: '{provider}'. Use 'anthropic' ou 'ollama'."
    )
