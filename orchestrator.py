"""Orquestrador: liga o comando transcrito aos MCP servers do Blender e do
FreeCAD via API da Anthropic, roteando cada pedido para a ferramenta certa."""

from __future__ import annotations

import asyncio
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


# Sentinela na fila de pedidos = ordem de encerramento.
_SHUTDOWN = object()


def _short_error(exc: BaseException) -> str:
    """Extrai uma mensagem legível — o anyio embrulha falhas de conexão num
    ExceptionGroup ('unhandled errors in a TaskGroup') que esconde a causa."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    text = str(exc).strip()
    return text or exc.__class__.__name__


class MCPBridge:
    """Mantém uma sessão MCP viva com um app (Blender ou FreeCAD) e expõe
    suas tools já convertidas pro formato de tool-use da Anthropic.

    Todo o ciclo de vida do MCP (abrir stdio, inicializar sessão, chamar
    tools, fechar) roda dentro de UMA task dedicada (`_run`). Isso é
    obrigatório: os task groups / cancel scopes do anyio usados pelo
    stdio_client e pelo ClientSession ficam presos à task onde foram
    abertos — abrir numa task e fechar em outra dispara o famoso
    'Attempted to exit cancel scope in a different task'. As chamadas de
    fora chegam via fila e são executadas pela própria `_run`."""

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self.connected = False
        self.anthropic_tools: list[dict[str, Any]] = []
        # nome prefixado (ex: "blender_create_object") -> nome real na MCP
        self.tool_map: dict[str, str] = {}
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._error: Exception | None = None
        self._requests: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> None:
        """Sobe a task dedicada e espera ela ficar pronta (ou falhar). Em
        caso de falha, propaga o erro — o orquestrador decide como reagir."""
        self._task = asyncio.create_task(self._run())
        ready_wait = asyncio.ensure_future(self._ready.wait())
        try:
            await asyncio.wait(
                {ready_wait, self._task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if not ready_wait.done():
                ready_wait.cancel()
        if not self.connected:
            # _ready foi setado no finally de _run por causa de um erro.
            raise self._error or RuntimeError(
                f"MCP server '{self.config.name}' encerrou antes de ficar pronto"
            )

    async def _run(self) -> None:
        """Dona única da sessão MCP: abre, lista tools, atende pedidos da
        fila e fecha — tudo na mesma task."""
        try:
            params = StdioServerParameters(
                command=self.config.command, args=self.config.args
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await self._load_tools(session)
                    self.connected = True
                    self._ready.set()
                    await self._serve(session)
        except Exception as exc:
            self._error = exc
        finally:
            self._ready.set()  # destrava connect() mesmo em erro precoce
            self._drain_pending()

    async def _load_tools(self, session: ClientSession) -> None:
        tools_result = await session.list_tools()
        for tool in tools_result.tools:
            prefixed = f"{self.config.tool_prefix}{tool.name}"
            self.tool_map[prefixed] = tool.name
            # mcp >=2 usa input_schema; versões antigas, inputSchema.
            schema = getattr(tool, "input_schema", None)
            if schema is None:
                schema = getattr(tool, "inputSchema", None)
            self.anthropic_tools.append(
                {
                    "name": prefixed,
                    "description": tool.description or "",
                    "input_schema": schema or {"type": "object", "properties": {}},
                }
            )

    async def _serve(self, session: ClientSession) -> None:
        while True:
            item = await self._requests.get()
            if item is _SHUTDOWN:
                return
            prefixed_name, arguments, fut = item
            try:
                real_name = self.tool_map[prefixed_name]
                result = await session.call_tool(real_name, arguments)
                if not fut.done():
                    fut.set_result(self._parse_result(result))
            except Exception as exc:  # falha de uma tool não derruba a sessão
                if not fut.done():
                    fut.set_exception(exc)

    def _drain_pending(self) -> None:
        err = self._error or RuntimeError(
            f"MCP server '{self.config.name}' encerrado"
        )
        while not self._requests.empty():
            item = self._requests.get_nowait()
            if item is _SHUTDOWN:
                continue
            _, _, fut = item
            if not fut.done():
                fut.set_exception(err)

    @staticmethod
    def _parse_result(result: Any) -> str:
        parts = []
        for block in result.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
            else:
                parts.append(f"[{getattr(block, 'type', 'conteúdo')} recebido]")
        return "\n".join(parts) if parts else "(sem retorno)"

    async def call(self, prefixed_name: str, arguments: dict[str, Any]) -> str:
        if not self.connected:
            raise RuntimeError(f"MCP server '{self.config.name}' não conectado")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._requests.put((prefixed_name, arguments, fut))
        return await fut

    async def close(self) -> None:
        if self._task is None:
            return
        if not self._task.done():
            await self._requests.put(_SHUTDOWN)
        await self._task  # _run trata os próprios erros; nunca propaga


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
                print(f"[mcp] falhou: {bridge.config.name} — {_short_error(exc)}")
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
