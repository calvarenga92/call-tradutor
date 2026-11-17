# call-tradutor

Tradução de call em tempo real, 100% local e gratuita. Funciona com qualquer app de chamada (Meet, Zoom, Teams, Discord, WhatsApp desktop…), porque trabalha no nível do áudio do sistema:

- **Ouvir:** o que a outra pessoa fala, em qualquer idioma (detectado automaticamente), vira legenda em português numa janela sempre no topo e, opcionalmente, dublagem em português no seu fone.
- **Falar:** você fala português e a outra pessoa ouve uma voz em inglês, entregue por um microfone virtual.

```
saída de áudio (loopback) ─► VAD ─► Whisper (auto) ─► LLM local ─► legenda PT (+ voz PT no fone)
seu microfone             ─► VAD ─► Whisper (pt) ─► LLM local ─► Kokoro (voz EN) ─► CABLE Input ─► app da call
```

| Etapa | Ferramenta |
|---|---|
| Captura de áudio | `soundcard` (WASAPI / CoreAudio / PulseAudio) |
| Detecção de fala | Silero VAD (streaming) |
| Transcrição | `faster-whisper` large-v3-turbo |
| Tradução | Ollama (modelo local, com as últimas falas como contexto) |
| Voz | Kokoro (ONNX) |
| Janela | PySide6 |

Latência medida numa RTX 3060 12 GB: legenda ~1,3 s após o fim da frase; voz ~1,6 s.

## Instalação (Windows + GPU NVIDIA)

1. Python 3.11+ e [Ollama](https://ollama.com):
   ```
   winget install Ollama.Ollama
   ollama pull qwen3:8b
   ```
2. Dependências:
   ```
   python -m venv .venv
   .venv\Scripts\pip install -e .[gpu]
   ```
3. Modelos de voz (Kokoro) em `models/`:
   ```
   curl -L -o models/kokoro-v1.0.onnx https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
   curl -L -o models/voices-v1.0.bin  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
   ```
4. Para a parte de voz: [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) (instalar como administrador e reiniciar).

Sem GPU NVIDIA (ex.: Mac): `pip install -e .[cpu]` e no `config.toml` use `device = "cpu"` e `compute_type = "int8"`. Fica mais lento.
No Mac, o loopback precisa do [BlackHole](https://github.com/ExistentialAudio/BlackHole); no Linux, um null-sink do PulseAudio.

## Uso

```
.venv\Scripts\python -m call_tradutor              # legenda + voz
.venv\Scripts\python -m call_tradutor --so-legenda # só legenda
.venv\Scripts\python scripts\listar_dispositivos.py
```

No app da call:
- **Microfone:** `CABLE Output (VB-Audio Virtual Cable)`. A pessoa só ouve a voz traduzida.
- **Alto-falante:** o seu fone, o mesmo que o `loopback_device` (ou a saída padrão).

Atalhos globais:
- `Ctrl+Alt+M`: liga/desliga a tradução da sua fala.
- `Ctrl+Alt+L`: liga/desliga a legenda.
- `Ctrl+Alt+C` ou o botão 🧹: limpa a tela e o contexto da conversa (quando algo foi capturado errado).

A janela é arrastável e redimensionável pelo canto.

## Dicas

- **Use fone de ouvido.**
- **Feche outros áudios** (YouTube, música): o loopback captura tudo o que sai na saída escolhida.
- **Avise a outra pessoa** que você está usando tradução automática; ela vai notar o atraso.
- **Ajustes** ficam no `config.toml`: voz, sensibilidade do VAD, silêncio que fecha a frase, modelo de tradução.
- **Log:** fica em `call-tradutor.log`, com tempo de cada etapa (`stt`, `tr`, `out`).

## Sugestão de resposta

Quando a outra pessoa termina de falar (`turn_gap_s` de silêncio), aparece um bloco **💡 Sugestão** com o que você pode responder. Ele usa a conversa até ali e o seu `perfil.md` (experiência, stack, casos para contar). O modelo só usa fatos do perfil e, se não souber, sugere uma resposta honesta.

- `perfil.md` fica fora do git (tem dados pessoais). Edite à vontade: quanto mais casos concretos, melhores as sugestões.
- `Ctrl+Alt+R` ou o botão 💡 liga e desliga.
- `[coach] lang = "en"` faz a sugestão sair em inglês, para ler em voz alta.
