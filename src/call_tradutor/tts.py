"""Voz em inglês com Kokoro (ONNX). Usa a GPU se o onnxruntime-gpu estiver instalado."""
import logging
import re
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

    def speak(self, text: str) -> np.ndarray:
        samples, sr = self.kokoro.create(text, voice=self.voice, speed=self.speed, lang="en-us")
        self.samplerate = sr
        return samples

    def speak_chunks(self, text: str) -> Iterator[np.ndarray]:
        """Gera o áudio frase a frase, para começar a tocar antes de terminar tudo."""
        for part in split_text(text):
            yield self.speak(part)
