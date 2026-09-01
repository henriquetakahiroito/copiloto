"""Ponto de entrada do copiloto: escuta a hotkey de push-to-talk, transcreve
e manda o comando pro orquestrador. Ctrl+C encerra."""

import asyncio
from pathlib import Path

import yaml
from pynput import keyboard

from audio import PushToTalkRecorder, Transcriber
from orchestrator import MCPServerConfig, Orchestrator

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def parse_hotkey(spec: str) -> set:
    """Converte algo como '<ctrl>+<space>' num conjunto de teclas do pynput."""
    keys = set()
    for token in spec.split("+"):
        token = token.strip()
        if token.startswith("<") and token.endswith(">"):
            keys.add(getattr(keyboard.Key, token[1:-1]))
        else:
            keys.add(keyboard.KeyCode.from_char(token))
    return keys


class PushToTalkApp:
    def __init__(self, config: dict):
        self.config = config
        self.hotkey = parse_hotkey(config["hotkey"])
        self.pressed: set = set()
        self.recorder = PushToTalkRecorder(
            sample_rate=config["audio"]["sample_rate"],
            device=config["audio"]["device"],
        )
        self.transcriber = Transcriber(
            model_size=config["stt"]["model"],
            device=config["stt"]["device"],
            compute_type=config["stt"]["compute_type"],
            language=config["stt"]["language"],
        )
        mcp_configs = [
            MCPServerConfig(
                name=name,
                command=cfg["command"],
                args=cfg["args"],
                tool_prefix=cfg["tool_prefix"],
            )
            for name, cfg in config["mcp_servers"].items()
        ]
        persona_path = Path(__file__).parent / config["persona"]["system_prompt_file"]
        self.orchestrator = Orchestrator(
            api_key=None,  # usa a variável de ambiente ANTHROPIC_API_KEY
            model=config["llm"]["model"],
            max_tokens=config["llm"]["max_tokens"],
            max_tool_turns=config["llm"]["max_tool_turns"],
            persona_path=persona_path,
            mcp_configs=mcp_configs,
        )
        self._recording = False
        self._loop: asyncio.AbstractEventLoop | None = None

    def _hotkey_complete(self) -> bool:
        return self.hotkey.issubset(self.pressed)

    def on_press(self, key):
        self.pressed.add(key)
        if self._hotkey_complete() and not self._recording:
            self._recording = True
            print("[gravando] segure e fale...")
            self.recorder.start()

    def on_release(self, key):
        self.pressed.discard(key)
        if self._recording and not self._hotkey_complete():
            self._recording = False
            audio = self.recorder.stop()
            print("[transcrevendo...]")
            if self._loop is not None:
                asyncio.run_coroutine_threadsafe(self._process(audio), self._loop)

    async def _process(self, audio) -> None:
        text = self.transcriber.transcribe(audio, self.config["audio"]["sample_rate"])
        if not text:
            print("[nada entendido]")
            return
        print(f"> {text}")
        reply = await self.orchestrator.handle_command(text)
        print(f"< {reply}")

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self.orchestrator.start()
        listener = keyboard.Listener(on_press=self.on_press, on_release=self.on_release)
        listener.start()
        print("Copiloto pronto. Segure a hotkey e fale. Ctrl+C para sair.")
        try:
            while True:
                await asyncio.sleep(0.5)
        finally:
            listener.stop()
            await self.orchestrator.stop()


def main() -> None:
    config = load_config()
    app = PushToTalkApp(config)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        print("\nEncerrando.")


if __name__ == "__main__":
    main()
