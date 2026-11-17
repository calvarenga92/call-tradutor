"""Ponto de entrada: `python -m call_tradutor [--so-legenda]`."""
import argparse
import logging
import sys
import threading

from .config import ROOT, load_config
from .gpu import setup_cuda_dlls

setup_cuda_dlls()

from PySide6.QtWidgets import QApplication  # noqa: E402

from . import audio  # noqa: E402
from .ui import LISTEN, SPEAK, Bridge, Overlay  # noqa: E402

log = logging.getLogger("main")


def boot(cfg: dict, bridge: Bridge, only_captions: bool, pipelines: dict) -> None:
    """Carrega modelos e sobe as pipelines fora da thread da UI."""
    try:
        from .pipeline import Pipeline
        from .segmenter import Segmenter
        from .stt import STT
        from .translate import Translator

        a, v, ui = cfg["audio"], cfg["vad"], cfg["ui"]
        on_event = bridge.event.emit

        bridge.status.emit("carregando Whisper na GPU…", False)
        stt = STT(**cfg["stt"])

        t = cfg["translate"]
        bridge.status.emit(f"carregando {t['model']} no Ollama…", False)
        listen_tr = Translator(t["ollama_url"], t["model"], "en", "pt", t["context_turns"])
        try:
            listen_tr.warmup()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Ollama não respondeu em {t['ollama_url']} — ele está aberto? ({e})")

        seg_args = dict(threshold=v["threshold"], silence_ms=v["silence_ms"],
                        min_speech_ms=v["min_speech_ms"], max_utterance_s=v["max_utterance_s"])

        loop_dev, loop_name = audio.get_loopback(a["loopback_device"])
        pipelines[LISTEN] = Pipeline(
            LISTEN, loop_dev, "en", stt, listen_tr,
            Segmenter(**seg_args, partial_every_s=1.0 if ui["partials"] else None), on_event)
        pipelines[LISTEN].start()
        status = f"ouvindo: {loop_name}"

        if not only_captions:
            cable = audio.get_cable(a["cable_device"])
            if cable is None:
                status += "  ·  sem VB-Cable: só legenda"
            else:
                from .tts import TTS

                bridge.status.emit("carregando voz (Kokoro)…", False)
                tts = TTS(**cfg["tts"])
                tts.speak("Ready.")  # aquece
                player = audio.CablePlayer(cable, tts.samplerate)
                mic_dev, mic_name = audio.get_mic(a["mic_device"])
                speak_tr = Translator(t["ollama_url"], t["model"], "pt", "en", t["context_turns"])
                pipelines[SPEAK] = Pipeline(
                    SPEAK, mic_dev, "pt", stt, speak_tr,
                    Segmenter(**seg_args, partial_every_s=None), on_event,
                    output=lambda text: [player.play(c) for c in tts.speak_chunks(text)])
                pipelines[SPEAK].start()
                status += f"  ·  mic: {mic_name} → {cable.name}"

        bridge.status.emit(status, False)
    except Exception as e:  # noqa: BLE001
        log.exception("boot")
        bridge.status.emit(str(e), True)


def start_hotkeys(cfg: dict, bridge: Bridge) -> None:
    try:
        from pynput import keyboard
    except ImportError:
        return
    hk = cfg["hotkeys"]
    keyboard.GlobalHotKeys({
        hk["toggle_mic"]: lambda: bridge.toggle.emit("mic"),
        hk["toggle_captions"]: lambda: bridge.toggle.emit("captions"),
    }).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="Tradução de call em tempo real, local.")
    parser.add_argument("--so-legenda", action="store_true", help="só a legenda (EN→PT)")
    parser.add_argument("--config", help="caminho do config.toml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(ROOT / "call-tradutor.log", encoding="utf-8")])
    cfg = load_config(args.config and __import__("pathlib").Path(args.config))

    app = QApplication(sys.argv)
    bridge = Bridge()
    win = Overlay(bridge, cfg["ui"]["font_size"], cfg["ui"]["opacity"], cfg["ui"]["history"])
    pipelines: dict = {}

    def on_toggle(which: str) -> None:
        if which == "mic":
            p = pipelines.get(SPEAK)
            if p:
                win.set_toggle("mic", p.toggle())
        else:
            win.set_toggle("captions", not win.captions_on)

    bridge.toggle.connect(on_toggle)
    if args.so_legenda:
        win.btn_mic.hide()
    win.show()

    threading.Thread(target=boot, args=(cfg, bridge, args.so_legenda, pipelines),
                     daemon=True).start()
    start_hotkeys(cfg, bridge)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
