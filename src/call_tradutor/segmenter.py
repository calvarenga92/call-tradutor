"""Corta o áudio contínuo em frases usando o Silero VAD (o mesmo que vem no faster-whisper)."""
import numpy as np
from faster_whisper.vad import get_vad_model

from .audio import BLOCK, SAMPLE_RATE

CONTEXT = 64
BLOCK_MS = BLOCK * 1000 // SAMPLE_RATE  # 32 ms


class StreamingVAD:
    """Silero em modo streaming: mantém o estado (h, c) e o contexto entre blocos."""

    def __init__(self):
        self.session = get_vad_model().session
        self.reset()

    def reset(self) -> None:
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.ctx = np.zeros(CONTEXT, dtype=np.float32)

    def prob(self, block: np.ndarray) -> float:
        x = np.concatenate([self.ctx, block]).astype(np.float32)[None, :]
        out, self.h, self.c = self.session.run(None, {"input": x, "h": self.h, "c": self.c})
        self.ctx = block[-CONTEXT:]
        return float(np.asarray(out).reshape(-1)[0])


class Segmenter:
    """Recebe blocos de 32 ms e devolve eventos ("partial" | "final", áudio)."""

    def __init__(self, threshold=0.5, silence_ms=600, min_speech_ms=250,
                 max_utterance_s=15, partial_every_s: float | None = 1.0, preroll_ms=300):
        self.vad = StreamingVAD()
        self.on = threshold
        self.off = max(threshold - 0.15, 0.05)
        self.silence_blocks = silence_ms // BLOCK_MS
        self.min_speech_blocks = min_speech_ms // BLOCK_MS
        self.max_blocks = int(max_utterance_s * 1000 // BLOCK_MS)
        self.long_blocks = 5000 // BLOCK_MS
        self.short_silence = min(250 // BLOCK_MS, self.silence_blocks)
        self.partial_blocks = int(partial_every_s * 1000 // BLOCK_MS) if partial_every_s else 0
        self.preroll_blocks = preroll_ms // BLOCK_MS
        self._reset_state()

    def _reset_state(self) -> None:
        self.speaking = False
        self.buf: list[np.ndarray] = []
        self.preroll: list[np.ndarray] = []
        self.silent = 0
        self.voiced = 0
        self.since_partial = 0

    def reset(self) -> None:
        self.vad.reset()
        self._reset_state()

    def feed(self, block: np.ndarray) -> list[tuple[str, np.ndarray]]:
        p = self.vad.prob(block)
        events = []

        if not self.speaking:
            self.preroll.append(block)
            self.preroll = self.preroll[-self.preroll_blocks:]
            if p >= self.on:
                self.speaking = True
                self.buf = list(self.preroll)
                self.voiced = 1
                self.silent = 0
                self.since_partial = 0
            return events

        self.buf.append(block)
        self.since_partial += 1
        if p >= self.off:
            self.voiced += 1
            self.silent = 0
        else:
            self.silent += 1

        # frase já longa (> 5 s): uma pausa curta basta para fechar, senão a legenda atrasa
        needed = self.silence_blocks if len(self.buf) < self.long_blocks else self.short_silence
        if self.silent >= needed or len(self.buf) >= self.max_blocks:
            if self.voiced >= self.min_speech_blocks:
                # corta o silêncio do fim, deixando ~150 ms
                keep = len(self.buf) - max(self.silent - 5, 0)
                events.append(("final", np.concatenate(self.buf[:keep])))
            self._reset_state()
        elif self.partial_blocks and self.since_partial >= self.partial_blocks:
            self.since_partial = 0
            events.append(("partial", np.concatenate(self.buf)))
        return events
