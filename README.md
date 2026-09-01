# Copiloto Blender + FreeCAD

Push-to-talk. Segura a hotkey, fala o comando, solta — o pedido vai pro
Blender ou pro FreeCAD, o que estiver aberto e fizer sentido pro pedido.

## Pré-requisitos

1. **Blender** aberto, com o addon [blender-mcp](https://github.com/ahujasid/blender-mcp)
   instalado e o servidor iniciado (painel lateral, aba BlenderMCP →
   *Start MCP Server*, ou *Connect to Claude*).
2. **FreeCAD** aberto, com o workbench [freecad-mcp](https://github.com/neka-nat/freecad-mcp)
   instalado, workbench *MCP Addon* selecionado e *Start RPC Server* clicado.
3. `uv`/`uvx` instalado (usado pra rodar os dois servidores MCP sob demanda).
4. O "cérebro" (escolhido em `llm.provider` no `config.yaml`):
   - `anthropic` (padrão): variável de ambiente `ANTHROPIC_API_KEY` com
     créditos.
   - `ollama`: Ollama instalado e um modelo com tool-calling baixado —
     sem chave, sem custo. Ver *Cérebro: Anthropic ou Ollama* abaixo.
5. Python 3.11+.

Nem Blender nem FreeCAD rodam headless nesse modo — os dois precisam estar
de pé com a GUI aberta antes de você começar a falar, já que o copiloto não
sabe de antemão qual dos dois vai usar.

## Instalação

```bash
cd copiloto
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Uso

```bash
python main.py
```

Segure `ctrl+space` (ajustável em `config.yaml`), fale o comando, solte.
O texto transcrito aparece no terminal, seguido da confirmação do
copiloto.

### Modo texto (sem microfone)

Pra testar o orquestrador e os MCP servers sem depender de áudio nem da
hotkey, rode com `--text`:

```bash
python main.py --text
```

Aparece um prompt `>`; digite o comando e Enter. Vai pro mesmo
orquestrador (Blender/FreeCAD via API), sem carregar o modelo de STT.
Linha vazia ou `Ctrl+C` encerra. Útil pra depurar o roteamento e as tools
quando você não quer (ou não pode) falar.

### Um app ou os dois

A inicialização é tolerante: o copiloto conecta no que estiver de pé.
Se só o Blender estiver aberto, ele sobe com as tools do Blender e avisa
que o FreeCAD ficou de fora — e vice-versa. Não trava mais esperando os
dois. (A chave `ANTHROPIC_API_KEY` só é exigida na hora de mandar um
comando de fato, não pra conectar.)

### Conferir a conexão (`--dry-run`)

Pra confirmar rapidamente quais apps estão conectados e quais tools cada
um expôs, sem chamar a API:

```bash
python main.py --dry-run
```

Lista as ferramentas de cada servidor conectado e sai. Bom pra rodar
antes de começar, só pra ter certeza de que o Blender e/ou o FreeCAD
estão respondendo.

## Cérebro: Anthropic (pago) ou Ollama (local, grátis)

O "cérebro" que lê o pedido e decide qual ferramenta chamar é um backend
plugável, escolhido no `config.yaml` em `llm.provider`:

- `anthropic` — API da Anthropic. Mais confiável no roteamento, mas é
  paga (pay-as-you-go) e exige `ANTHROPIC_API_KEY`.
- `ollama` — modelo local via [Ollama](https://ollama.com). Grátis, roda
  na sua máquina, nenhum dado sai dela. Precisa de um modelo com suporte
  a *tool-calling* (ex: `qwen2.5`, `llama3.1`).

O resto do copiloto (voz→texto pelo faster-whisper, e o controle de
Blender/FreeCAD pelos MCP servers) já é 100% local e grátis nos dois
casos — só o cérebro muda.

### Usando o modo local (Ollama)

1. Instale o Ollama e deixe o servidor de pé (ele sobe sozinho após a
   instalação; pra garantir, `ollama serve`).
2. Baixe um modelo com tool-calling:
   ```bash
   ollama pull qwen2.5:7b
   ```
3. No `config.yaml`, aponte o provider pro Ollama:
   ```yaml
   llm:
     provider: "ollama"
     ollama:
       model: "qwen2.5:7b"
       host: "http://localhost:11434"
   ```
4. Rode normal (`python main.py`, `--text` ou `--dry-run`). Nenhuma chave
   de API é necessária.

Nota honesta: modelos locais pequenos escolhem a tool certa e montam os
argumentos com menos precisão que o Claude. Pedidos simples saem bem;
pedidos complexos podem exigir mais tentativas. Modelos maiores (se a GPU
aguentar) melhoram o roteamento.

## Ajustes rápidos

- **Hotkey**: campo `hotkey` em `config.yaml`, formato `<ctrl>+<space>`,
  `<alt>+<f1>` etc.
- **Modelo de STT**: `stt.model` — `tiny`/`base` são mais rápidos, `small`
  tem melhor precisão em português mantendo latência baixa em CPU.
- **Personalidade**: edite `persona.md` diretamente — é o system prompt
  puro, sem lógica escondida em código.
- **execute_code liberado ou restrito**: hoje o orquestrador expõe todas
  as tools de ambos os servidores, incluindo `execute_code` (Python
  arbitrário). Pra travar isso, filtre `anthropic_tools` em
  `orchestrator.py` na função `MCPBridge.connect` antes de adicionar a
  tool à lista.

## Limitações conhecidas

- Sem wake-word — é push-to-talk apenas, por decisão de projeto (privacidade
  e simplicidade).
- Sem headless: Blender e FreeCAD precisam da janela aberta.
- `execute_code` roda Python arbitrário dentro do app — trate como
  qualquer execução de código não sandboxado.
- Sem retorno em voz (TTS) ainda — a confirmação é só em texto no
  terminal. Dá pra plugar um TTS local depois em `main.py`, no ponto onde
  `reply` é impresso.
