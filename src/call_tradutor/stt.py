"""Transcrição com faster-whisper. Um único modelo na GPU, compartilhado pelas duas direções."""
import threading

import numpy as np
from faster_whisper import WhisperModel

# Frases que o Whisper costuma "inventar" em ruído/silêncio.
HALLUCINATIONS = {
    "thank you.", "thanks for watching!", "thank you for watching.", "thanks for watching.",
    "you", "bye.", "obrigado.", "obrigada.", "legendas pela comunidade amara.org",
    "e aí", "tchau.", "...",
}


class STT:
    def __init__(self, model: str, device: str, compute_type: str, hotwords: str = "",
                 listen_language: str = ""):  # listen_language é lido pelo main
        self.model = WhisperModel(model, device=device, compute_type=compute_type)
        self.hotwords = hotwords or None
        self.lock = threading.Lock()

    def transcribe(self, audio: np.ndarray, lang: str | None, blocking: bool = True,
                   prompt: str | None = None) -> tuple[str, str] | None:
        """Retorna (texto, idioma). `lang=None` detecta o idioma.

        Retorna None se `blocking=False` e a GPU estiver ocupada.
        """
        if not self.lock.acquire(blocking=blocking):
            return None
        try:
            segments, info = self.model.transcribe(
                audio,
                language=lang,
                beam_size=1,
                vad_filter=False,
                condition_on_previous_text=False,
                without_timestamps=True,
                # o fim do trecho anterior dá contexto: frase cortada no meio continua coerente
                initial_prompt=prompt or None,
                hotwords=self.hotwords,
            )
            parts = [
                s.text.strip()
                for s in segments
                if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)
            ]
        finally:
            self.lock.release()
        text = " ".join(p for p in parts if p).strip()
        if text.lower() in HALLUCINATIONS or not any(ch.isalnum() for ch in text):
            text = ""
        return text, info.language
