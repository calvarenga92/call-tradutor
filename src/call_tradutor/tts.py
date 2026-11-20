"""Voz em inglês com Kokoro (ONNX). Usa a GPU se o onnxruntime-gpu estiver instalado."""
import logging
import re
import threading
from typing import Iterator

import numpy as np
import onnxruntime as ort
from kokoro_onnx import Kokoro

log = logging.getLogger("tts")


def split_text(text: str) -> list[str]:
    """Quebra em fim de frase; frases longas quebram também em vírgula após ~60 caracteres."""
    parts, cur = [], ""
    for piece in re.split(r"(?<=[.!?;,:])\s+", text.strip()):
        cur = f"{cur} {piece}".strip()
        if re.search(r"[.!?]$", cur) or len(cur) >= 60:
            parts.append(cur)
            cur = ""
    if cur:
        parts.append(cur)
    return parts


class TTS:
    def __init__(self, model_path: str, voices_path: str, voice: str, speed: float = 1.0):
        providers = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider")
                     if p in ort.get_available_providers()]
        opts = ort.SessionOptions()
        opts.log_severity_level = 3  # só erros
        session = ort.InferenceSession(model_path, sess_options=opts, providers=providers)
        log.info("kokoro providers: %s", session.get_providers())
        self.kokoro = Kokoro.from_session(session, voices_path)
        self.voice = voice
        self.speed = speed
        self.samplerate = 24000
        self._lock = threading.Lock()  # o phonemizer (espeak) não é thread-safe

    def speak(self, text: str, voice: str | None = None, lang: str = "en-us",
              speed: float | None = None) -> np.ndarray:
        from .voices import resolve

        with self._lock:
            samples, sr = self.kokoro.create(text, voice=resolve(self.voice if voice is None else voice),
                                             speed=speed or self.speed, lang=lang)
        self.samplerate = sr
        return samples

    def speak_chunks(self, text: str, **kw) -> Iterator[np.ndarray]:
        """Gera o áudio frase a frase, para começar a tocar antes de terminar tudo."""
        for part in split_text(text):
            yield self.speak(part, **kw)


class SpeechQueue:
    """Fila de fala numa thread própria: quem traduz só entrega o texto e segue para o próximo
    trecho, enquanto a voz deste é gerada e tocada (antes a transcrição do trecho seguinte
    esperava a voz inteira ficar pronta)."""

    def __init__(self, tts: "TTS", player, **speak_kw):
        import queue

        self.tts = tts
        self.player = player
        self.kw = speak_kw
        self.gain = 1.0  # volume (o da dublagem é ajustável no ⚙)
        self.epoch = 0   # muda no flush: a frase que estava sendo gerada não toca
        self.q: "queue.Queue[str]" = queue.Queue()
        threading.Thread(target=self._run, name="fala", daemon=True).start()

    def say(self, text: str) -> None:
        self.q.put((text, None))

    def flush(self) -> None:
        """Descarta o que falta falar e corta o que está tocando."""
        import queue

        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.epoch += 1
        self.player.flush()

    def say_with(self, text: str, **kw) -> None:
        """Uma frase com outros parâmetros (ex.: amostra de voz), sem mudar a fila."""
        self.q.put((text, kw))

    def _run(self) -> None:
        while True:
            text, extra = self.q.get()
            ep = self.epoch
            try:
                for chunk in self.tts.speak_chunks(text, **{**self.kw, **(extra or {})}):
                    if ep != self.epoch:
                        break
                    self.player.play(np.clip(chunk * self.gain, -1, 1) if self.gain != 1 else chunk)
            except Exception:  # noqa: BLE001
                log.exception("fala")
