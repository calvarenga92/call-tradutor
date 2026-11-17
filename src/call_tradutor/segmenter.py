"""Corta o áudio contínuo em trechos curtos usando o Silero VAD (o mesmo do faster-whisper).

Trechos curtos = tradução rápida. Regras para fechar um trecho:
- silêncio de `silence_ms` (fim de frase normal);
- depois de `soft_max_s`, qualquer queda na fala (respiração, fim de palavra);
- em `max_utterance_s`, corta no ponto de fala mais fraca do último 1,5 s e
  continua o resto no trecho seguinte (para quem fala sem parar).
"""
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


def _blocks(ms: float) -> int:
    return max(int(ms // BLOCK_MS), 1)


class Segmenter:
    """Recebe blocos de 32 ms e devolve eventos ("partial" | "final", áudio)."""

    def __init__(self, threshold=0.5, silence_ms=400, min_speech_ms=250, soft_max_s=3.0,
                 max_utterance_s=5.0, partial_every_s: float | None = 0.8, preroll_ms=300):
        self.vad = StreamingVAD()
        self.on = threshold
        self.off = max(threshold - 0.15, 0.05)
        self.silence_blocks = _blocks(silence_ms)
        self.min_speech_blocks = _blocks(min_speech_ms)
        self.soft_blocks = _blocks(soft_max_s * 1000)
        self.max_blocks = _blocks(max_utterance_s * 1000)
        self.cut_window = _blocks(1500)
        self.partial_blocks = _blocks(partial_every_s * 1000) if partial_every_s else 0
        self.preroll_blocks = _blocks(preroll_ms)
        self._reset_state()

    def _reset_state(self) -> None:
        self.speaking = False
        self.buf: list[np.ndarray] = []
        self.probs: list[float] = []
        self.preroll: list[np.ndarray] = []
        self.silent = 0
        self.voiced = 0
        self.since_partial = 0

    def reset(self) -> None:
        self.vad.reset()
        self._reset_state()

    def feed(self, block: np.ndarray) -> list[tuple[str, np.ndarray]]:
        p = self.vad.prob(block)
        events: list[tuple[str, np.ndarray]] = []

        if not self.speaking:
            self.preroll.append(block)
            self.preroll = self.preroll[-self.preroll_blocks:]
            if p >= self.on:
                self.speaking = True
                self.buf = list(self.preroll)
                self.probs = [0.0] * (len(self.buf) - 1) + [p]
                self.voiced = 1
                self.silent = 0
                self.since_partial = 0
            return events

        self.buf.append(block)
        self.probs.append(p)
        self.since_partial += 1
        if p >= self.off:
            self.voiced += 1
            self.silent = 0
        else:
            self.silent += 1

        n = len(self.buf)
        if self.silent >= self.silence_blocks or (n >= self.soft_blocks and p < self.on):
            if self.voiced >= self.min_speech_blocks:
                keep = n - max(self.silent - 4, 0)  # tira o silêncio do fim, deixa ~130 ms
                events.append(("final", np.concatenate(self.buf[:keep])))
            self._reset_state()
        elif n >= self.max_blocks:
            # fala contínua: corta no ponto mais "fraco" do fim e segue com o resto
            window = self.probs[-self.cut_window:]
            cut = n - len(window) + int(np.argmin(window)) + 1
            events.append(("final", np.concatenate(self.buf[:cut])))
            self.buf, self.probs = self.buf[cut:], self.probs[cut:]
            self.voiced = len(self.buf)
            self.since_partial = 0
        elif self.partial_blocks and self.since_partial >= self.partial_blocks:
            self.since_partial = 0
            events.append(("partial", np.concatenate(self.buf)))
        return events
