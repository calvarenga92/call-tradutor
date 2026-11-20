"""Ponto de entrada: `python -m call_tradutor [--so-legenda]`."""
import argparse
import logging
import os
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
        # captura "tudo menos o app": a dublagem não volta na captura, então não precisa pausar
        # enquanto ela toca (e nada do que a outra pessoa fala se perde)
        echo_free = False
        if a.get("listen_capture", "processo") == "processo" and sys.platform == "win32":
            from . import process_loopback
            echo_free = process_loopback.available()
            if not echo_free:
                log.warning("captura por processo indisponível; usando o loopback do dispositivo")
        if echo_free:
            loop_dev = process_loopback.ProcessLoopback()
            loop_name = loop_dev.name
        else:
            loop_dev, loop_name = audio.get_loopback(a["loopback_device"])
        dub_player = None
        if tts is not None and d["enabled"]:
            dub_player = audio.Player(audio.get_output(d["device"] or a["loopback_device"]),
                                      tts.samplerate)

        if dub_player and d.get("duck", 0.4) < 1 and sys.platform == "win32":
            # abaixa o Meet enquanto a dublagem toca, para as duas vozes não se embolarem
            try:
                from .ducking import Ducker

                pipelines["ducking"] = Ducker(d["device"] or a["loopback_device"] or "",
                                              dub_player.playing, d.get("duck", 0.4))
            except Exception:  # noqa: BLE001
                log.exception("ducking indisponível")

        dub_voice = None
        if dub_player:
            from .tts import SpeechQueue

            dub_voice = SpeechQueue(tts, dub_player, voice=d["voice"], lang="pt-br", speed=d["speed"])

        def dub(text: str) -> None:
            if dub_voice and dub_on.is_set():
                dub_voice.say(text)

        pipelines[LISTEN] = Pipeline(
            LISTEN, loop_dev, cfg["stt"].get("listen_language") or None, stt, listen_tr,
            Segmenter(**seg_args, partial_every_s=partial), on_event, output=dub,
            mute_when=dub_player.playing if dub_player and not echo_free else None,
            translate_partials=bool(partial))
        pipelines[LISTEN].start()
        status = f"ouvindo: {loop_name}" + ("  ·  dublagem PT" if dub_player else "")

        # --- falar: seu microfone (português) → texto EN na tela (+ voz EN no VB-Cable) -----
        if not only_captions:
            mic_dev, mic_name = audio.get_mic(a["mic_device"])
            cable_player = audio.Player(cable, tts.samplerate, tail_s=0) if cable else None

            cable_voice = None
            if cable_player:
                from .tts import SpeechQueue

                cable_voice = SpeechQueue(tts, cable_player)

            def to_cable(text: str) -> None:
                if cable_voice:
                    cable_voice.say(text)

            speak_tr = Translator(t["ollama_url"], t["model"], "pt", "en", t["context_turns"])
            # sua fala espera a frase terminar: a voz em inglês sai inteira, não em pedaços
            speak_seg = {**seg_args, **cfg.get("vad_falar", {})}
            pipelines[SPEAK] = Pipeline(
                SPEAK, mic_dev, "pt", stt, speak_tr, Segmenter(**speak_seg, partial_every_s=None),
                on_event, output=to_cable)
            pipelines[SPEAK].start()
            status += f"  ·  mic: {mic_name}" + (" → VB-Cable" if cable else " (sem VB-Cable: só texto)")

        log.info("pronto | %s", status)
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
        hk.get("quit", "<ctrl>+<alt>+q"): lambda: bridge.toggle.emit("quit"),
    }).start()


def already_running() -> bool:
    """Uma instância só: duas cópias escutam e falam em dobro (e a janela de uma esconde a outra)."""
    if sys.platform != "win32":
        return False
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    global _MUTEX  # mantém o mutex vivo enquanto o app roda; o Windows solta ao fechar
    _MUTEX = kernel32.CreateMutexW(None, False, "Local\\call-tradutor")
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def main() -> None:
    if already_running():
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, "O Call Tradutor já está aberto.", "Call Tradutor", 0x40)
        return
    parser = argparse.ArgumentParser(description="Tradução de call em tempo real, local.")
    parser.add_argument("--so-legenda", action="store_true", help="só a legenda (EN→PT)")
    parser.add_argument("--config", help="caminho do config.toml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(ROOT / "call-tradutor.log", encoding="utf-8")])
    cfg = load_config(args.config and __import__("pathlib").Path(args.config))

    from . import translate
    t = cfg["translate"]
    translate.KEEP_ALIVE = t.get("keep_alive", "5m")

    app = QApplication(sys.argv)
    app.setApplicationName("Call Tradutor")
    bridge = Bridge()
    win = Overlay(bridge, cfg["ui"]["font_size"], cfg["ui"]["opacity"], cfg["ui"]["history"])
    pipelines: dict = {}
    dub_on = threading.Event()
    if cfg["dub"]["enabled"] and cfg["dub"].get("start_on", False):
        dub_on.set()

    def on_toggle(which: str) -> None:
        if which == "quit":
            win.close()  # salva a posição e encerra (ver shutdown)
        elif which == "mic":
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
        elif which == "captions":
            win.set_toggle("captions", not win.captions_on)

    def shutdown() -> None:
        """Fechar = encerrar tudo: para a captura, solta o modelo do Ollama e sai do processo."""
        log.info("encerrando")
        for obj in pipelines.values():
            stop = getattr(obj, "stop", None)
            if isinstance(stop, threading.Event):
                stop.set()
        ducker = pipelines.get("ducking")
        if ducker:
            ducker.close()  # devolve o volume do Meet antes de sair
        # o modelo sai da memória na hora (não espera o keep_alive)
        translate.unload(t["ollama_url"], t["model"])

    app.aboutToQuit.connect(shutdown)

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
    code = app.exec()
    logging.shutdown()
    # threads de áudio, atalhos globais e o carregamento podem estar presos em chamadas nativas;
    # sair "na marra" garante que não sobra processo escondido segurando microfone e GPU
    os._exit(code)


if __name__ == "__main__":
    main()
