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


def boot(cfg: dict, bridge: Bridge, only_captions: bool, pipelines: dict,
         dub_on: threading.Event) -> None:
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
        # aquece a GPU nos dois modos (com e sem idioma), senão a 1ª fala trava alguns segundos
        import numpy as np
        noise = (np.random.randn(16000 * 3) * 0.01).astype(np.float32)
        for lang in (None, "en", "pt"):
            stt.transcribe(noise, lang)

        t = cfg["translate"]
        bridge.status.emit(f"carregando {t['model']} no Ollama…", False)
        listen_tr = Translator(t["ollama_url"], t["model"], "auto", "pt", t["context_turns"])
        try:
            listen_tr.warmup()
            listen_tr.translate("Hello, can you hear me?", remember=False)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Ollama não respondeu em {t['ollama_url']} — ele está aberto? ({e})")

        # sugestão de resposta: recebe os mesmos eventos que a tela
        c = cfg.get("coach", {})
        if c.get("enabled"):
            from .coach import Coach

            coach = Coach(t["ollama_url"], t["model"], ROOT / c.get("profile", "perfil.md"),
                          bridge.event.emit, turn_gap_s=c.get("turn_gap_s", 1.2),
                          name=c.get("name", "Me"))
            pipelines["coach"] = coach

            def on_event(p: str, k: str, txt: str) -> None:
                bridge.event.emit(p, k, txt)
                coach.feed(p, k, txt)

        seg_args = dict(threshold=v["threshold"], silence_ms=v["silence_ms"],
                        min_speech_ms=v["min_speech_ms"], soft_max_s=v["soft_max_s"],
                        max_utterance_s=v["max_utterance_s"])
        partial = ui.get("partial_every_s", 0.6) if ui["partials"] else None
        d = cfg["dub"]
        cable = None if only_captions else audio.get_cable(a["cable_device"])

        tts = None
        if d["enabled"] or cable is not None:
            from .tts import TTS

            bridge.status.emit("carregando voz (Kokoro)…", False)
            tts = TTS(**cfg["tts"])
            tts.speak("Ready.")  # aquece

        # --- ouvir: o que toca no fone (qualquer idioma) → legenda PT (+ dublagem PT no fone) -------
        loop_dev, loop_name = audio.get_loopback(a["loopback_device"])
        dub_player = None
        if tts is not None and d["enabled"]:
            dub_player = audio.Player(audio.get_output(d["device"] or a["loopback_device"]),
                                      tts.samplerate)

        def dub(text: str) -> None:
            if dub_player and dub_on.is_set():
                for c in tts.speak_chunks(text, voice=d["voice"], lang="pt-br", speed=d["speed"]):
                    dub_player.play(c)

        pipelines[LISTEN] = Pipeline(
            LISTEN, loop_dev, None, stt, listen_tr, Segmenter(**seg_args, partial_every_s=partial),
            on_event, output=dub, mute_when=dub_player.playing if dub_player else None,
            translate_partials=bool(partial))
        pipelines[LISTEN].start()
        status = f"ouvindo: {loop_name}" + ("  ·  dublagem PT" if dub_player else "")

        # --- falar: seu microfone (português) → texto EN na tela (+ voz EN no VB-Cable) -----
        if not only_captions:
            mic_dev, mic_name = audio.get_mic(a["mic_device"])
            cable_player = audio.Player(cable, tts.samplerate, tail_s=0) if cable else None

            def to_cable(text: str) -> None:
                if cable_player:
                    for c in tts.speak_chunks(text):
                        cable_player.play(c)

            speak_tr = Translator(t["ollama_url"], t["model"], "pt", "en", t["context_turns"])
            pipelines[SPEAK] = Pipeline(
                SPEAK, mic_dev, "pt", stt, speak_tr, Segmenter(**seg_args, partial_every_s=None),
                on_event, output=to_cable)
            pipelines[SPEAK].start()
            status += f"  ·  mic: {mic_name}" + (" → VB-Cable" if cable else " (sem VB-Cable: só texto)")

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
        hk["toggle_dub"]: lambda: bridge.toggle.emit("dub"),
        hk["toggle_coach"]: lambda: bridge.toggle.emit("coach"),
        hk.get("clear", "<ctrl>+<alt>+c"): lambda: bridge.toggle.emit("clear"),
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
    dub_on = threading.Event()
    if cfg["dub"]["enabled"] and cfg["dub"].get("start_on", False):
        dub_on.set()

    def on_toggle(which: str) -> None:
        if which == "mic":
            p = pipelines.get(SPEAK)
            if p:
                win.set_toggle("mic", p.toggle())
        elif which == "clear":
            for key, obj in pipelines.items():
                obj.clear()
            win.clear()
        elif which == "coach":
            coach = pipelines.get("coach")
            if coach:
                if coach.enabled.is_set():
                    coach.enabled.clear()
                else:
                    coach.enabled.set()
                win.set_toggle("coach", coach.enabled.is_set())
        elif which == "dub":
            if dub_on.is_set():
                dub_on.clear()
            else:
                dub_on.set()
            win.set_toggle("dub", dub_on.is_set())
        else:
            win.set_toggle("captions", not win.captions_on)

    bridge.toggle.connect(on_toggle)
    if args.so_legenda:
        win.btn_mic.hide()
    if not cfg.get("coach", {}).get("enabled"):
        win.btn_coach.hide()
        win.reply.hide()
    if cfg["dub"]["enabled"]:
        win.set_toggle("dub", dub_on.is_set())
    else:
        win.btn_dub.hide()
    win.show()

    threading.Thread(target=boot, args=(cfg, bridge, args.so_legenda, pipelines, dub_on),
                     daemon=True).start()
    start_hotkeys(cfg, bridge)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
