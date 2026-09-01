"""Orquestrador: liga o comando transcrito aos MCP servers do Blender e do
FreeCAD via API da Anthropic, roteando cada pedido para a ferramenta certa."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass
class MCPServerConfig:
    name: str
    command: str
    args: list[str]
    tool_prefix: str


class MCPBridge:
    """Mantém uma sessão MCP viva com um app (Blender ou FreeCAD) e expõe
    suas tools já convertidas pro formato de tool-use da Anthropic."""

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._session: ClientSession | None = None
        self._stack = contextlib.AsyncExitStack()
        self.connected = False
        self.anthropic_tools: list[dict[str, Any]] = []
        # nome prefixado (ex: "blender_create_object") -> nome real na MCP
        self.tool_map: dict[str, str] = {}

    async def connect(self, timeout: float = 60.0) -> None:
        """Conecta ao MCP server com timeout. Se falhar, limpa o próprio
        stack e propaga o erro — o orquestrador decide como reagir."""
        try:
            await asyncio.wait_for(self._connect(), timeout)
            self.connected = True
        except BaseException:
            with contextlib.suppress(Exception):
                await self._stack.aclose()
            raise

    async def _connect(self) -> None:
        params = StdioServerParameters(command=self.config.command, args=self.config.args)
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

        tools_result = await self._session.list_tools()
        for tool in tools_result.tools:
            prefixed = f"{self.config.tool_prefix}{tool.name}"
            self.tool_map[prefixed] = tool.name
            self.anthropic_tools.append(
                {
                    "name": prefixed,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema,
                }
            )

    async def call(self, prefixed_name: str, arguments: dict[str, Any]) -> str:
        if self._session is None:
            raise RuntimeError(f"MCP server '{self.config.name}' não conectado")
        real_name = self.tool_map[prefixed_name]
        result = await self._session.call_tool(real_name, arguments)
        parts = []
        for block in result.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
            else:
                parts.append(f"[{getattr(block, 'type', 'conteúdo')} recebido]")
        return "\n".join(parts) if parts else "(sem retorno)"

    async def close(self) -> None:
        await self._stack.aclose()


class Orchestrator:
    def __init__(
        self,
        api_key: str | None,
        model: str,
        max_tokens: int,
        max_tool_turns: int,
        persona_path: Path,
        mcp_configs: list[MCPServerConfig],
    ):
        self._api_key = api_key
        self._client: anthropic.Anthropic | None = None
        self.model = model
        self.max_tokens = max_tokens
        self.max_tool_turns = max_tool_turns
        self.system_prompt = persona_path.read_text(encoding="utf-8")
        self.bridges = [MCPBridge(cfg) for cfg in mcp_configs]

    @property
    def client(self) -> anthropic.Anthropic:
        """Cliente Anthropic criado sob demanda — assim start()/dry-run
        funcionam sem ANTHROPIC_API_KEY; a chave só é exigida ao enviar
        um comando de fato."""
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    async def start(self) -> None:
        """Conecta no que estiver disponível. Um app fora do ar não impede
        o outro nem trava a inicialização."""
        for bridge in self.bridges:
            try:
                await bridge.connect()
                print(
                    f"[mcp] conectado: {bridge.config.name} "
                    f"({len(bridge.anthropic_tools)} tools)"
                )
            except Exception as exc:
                print(f"[mcp] falhou: {bridge.config.name} — {exc}")
        if not any(b.connected for b in self.bridges):
            print(
                "[mcp] nenhum servidor conectado — os comandos não terão "
                "ferramentas disponíveis. Abra Blender/FreeCAD com os MCP "
                "servers ligados."
            )

    async def stop(self) -> None:
        for bridge in self.bridges:
            await bridge.close()

    def _all_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for bridge in self.bridges:
            if bridge.connected:
                tools.extend(bridge.anthropic_tools)
        return tools

    async def _dispatch_tool(self, name: str, arguments: dict[str, Any]) -> str:
        for bridge in self.bridges:
            if name in bridge.tool_map:
                return await bridge.call(name, arguments)
        return f"erro: tool '{name}' não encontrada em nenhum MCP server conectado"

    async def handle_command(self, text: str) -> str:
        """Envia o comando transcrito pro modelo, executa as tools necessárias
        no app certo e devolve a resposta final (tom já vem do system prompt)."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": text}]
        tools = self._all_tools()

        for _ in range(self.max_tool_turns):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=self.system_prompt,
                tools=tools,
                messages=messages,
            )

            if response.stop_reason != "tool_use":
                return "".join(
                    block.text for block in response.content if block.type == "text"
                ).strip()

            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result_text = await self._dispatch_tool(block.name, block.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        return "Muitas idas e vindas nessa tarefa — quebra em partes menores?"
