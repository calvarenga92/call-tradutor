"""Uma direção de tradução: captura → frases → transcrição → tradução → saída."""
import logging
import queue
import re
import threading
import time
from typing import Callable

import numpy as np

from .audio import blocks
from .segmenter import Segmenter, StreamingVAD
from .stt import STT
from .translate import Translator

log = logging.getLogger("pipeline")

# interjeições de quem está pensando: não viram legenda, voz nem sugestão de resposta
_FILLER = re.compile(
    r"(?:\s*(?:h*m+h*|u+h*m*|e+r+m*|a+h+|o+h*|o+u+|e+h+|h+u+h+|m+h+m+|uh-huh|é+|ã+|ahn|hum+)"
    r"[\s,.!?…\-]*)+", re.IGNORECASE)


def is_filler(text: str) -> bool:
    return bool(_FILLER.fullmatch(text.strip()))

# on_event(pipeline, tipo, texto). Tipos: partial_src, src, partial_dst, dst, error
EventFn = Callable[[str, str, str], None]


class Pipeline:
    def __init__(self, name: str, device, src: str | None, stt: STT, translator: Translator,
                 segmenter: Segmenter, on_event: EventFn,
                 output: Callable[[str], None] | None = None,
                 mute_when: threading.Event | None = None, translate_partials: bool = False,
                 reopen: Callable[[], object] | None = None, track_voice: bool = False):
        self.name = name
        self.device = device
        self.src = src
        self.stt = stt
        self.translator = translator
        self.segmenter = segmenter
        self.on_event = on_event
        self.output = output
        self.mute_when = mute_when  # ex.: dublagem tocando no fone → não captar o próprio áudio
        self.translate_partials = translate_partials
        # aparelho caiu (Windows invalidou, USB reconectou…): reabre pelo nome e segue
        self.reopen = reopen
        # trava de eco: o "ouvir" fica surdo enquanto você fala (e logo depois)
        self.deaf_when: Callable[[], bool] | None = None
        self.voice_at = 0.0  # última vez que o seu microfone ouviu fala (track_voice)
        self._activity = StreamingVAD() if track_voice else None
        self.enabled = threading.Event()
        self.enabled.set()
        self.stop = threading.Event()
        self.finals: queue.Queue[np.ndarray] = queue.Queue()
        self._partial: np.ndarray | None = None
        self._partial_lock = threading.Lock()
        # idioma detectado fica "travado" por alguns trechos: detectar custa ~250 ms cada vez
        self._prev_text = ""     # fim do trecho anterior, contexto para o Whisper
        self._epoch = 0          # muda no "Limpar": resultados de antes são descartados
        self._reset_segmenter = False
        self._lang: str | None = None
        self._lang_left = 0

    LANG_REUSE = 6  # trechos com o idioma travado antes de detectar de novo
    LANG_MIN_AUDIO = 16000 * 1.5  # só trechos de 1,5 s ou mais decidem o idioma

    def _lang_hint(self) -> str | None:
        if self.src is not None:
            return self.src
        return self._lang if self._lang_left > 0 else None

    def _learn_lang(self, lang: str, detected: bool) -> None:
        if self.src is not None:
            return
        if detected:
            self._lang, self._lang_left = lang, self.LANG_REUSE
        else:
            self._lang_left -= 1

    def start(self) -> None:
        threading.Thread(target=self._capture, name=f"{self.name}-cap", daemon=True).start()
        threading.Thread(target=self._work, name=f"{self.name}-work", daemon=True).start()

    def clear(self) -> None:
        """Botão Limpar: descarta o que está na fila/em andamento e zera o contexto."""
        self._epoch += 1
        with self._partial_lock:
            self._partial = None
        while not self.finals.empty():
            try:
                self.finals.get_nowait()
            except queue.Empty:
                break
        self._prev_text = ""
        self.translator.history.clear()
        self._reset_segmenter = True  # a thread de captura reseta (o VAD é dela)

    def _emit(self, ep: int, kind: str, text: str) -> None:
        if ep == self._epoch:
            self.on_event(self.name, kind, text)

    def toggle(self) -> bool:
        if self.enabled.is_set():
            self.enabled.clear()
            # desligou: o que já estava gravado não é mais traduzido nem falado
            self._epoch += 1
            with self._partial_lock:
                self._partial = None
            while not self.finals.empty():
                try:
                    self.finals.get_nowait()
                except queue.Empty:
                    break
        else:
            self.enabled.set()
        return self.enabled.is_set()

    # --- captura -------------------------------------------------------------------------
    def _capture(self) -> None:
        """Captura com reconexão: se o aparelho cair, tenta de novo a cada 1 s até voltar."""
        self._failed = False
        while not self.stop.is_set():
            try:
                if self._failed and self.reopen:
                    self.device = self.reopen()
                self._capture_loop()
                return
            except Exception as e:  # noqa: BLE001
                if not self._failed:
                    log.exception("captura %s", self.name)
                    what = "Microfone" if self.name == "falar" else "Captura do som"
                    self.on_event(self.name, "error", f"{what} caiu ({e}) — reconectando…")
                self._failed = True
                self.segmenter.reset()
                self.stop.wait(1.0)

    def _capture_loop(self) -> None:
        was_enabled = True
        for block in blocks(self.device, self.stop):
            if self._failed:  # voltou depois de cair
                self._failed = False
                log.info("captura %s reconectada", self.name)
                self.on_event(self.name, "recovered", "")
            if self._activity is not None and self._activity.prob(block) > 0.5:
                self.voice_at = time.monotonic()
            deaf = self.deaf_when is not None and self.deaf_when()
            if not self.enabled.is_set() or deaf or (self.mute_when and self.mute_when.is_set()):
                if was_enabled:
                    self.segmenter.reset()
                    was_enabled = False
                continue
            was_enabled = True
            if self._reset_segmenter:
                self._reset_segmenter = False
                self.segmenter.reset()
                continue
            for kind, audio in self.segmenter.feed(block):
                if kind == "final":
                    with self._partial_lock:
                        self._partial = None
                    self.finals.put(audio)
                else:
                    with self._partial_lock:
                        self._partial = audio

    # --- processamento -------------------------------------------------------------------
    def _work(self) -> None:
        while not self.stop.is_set():
            try:
                audio = self.finals.get(timeout=0.05)
            except queue.Empty:
                self._do_partial()
                continue
            try:
                self._do_final(audio)
            except Exception as e:  # noqa: BLE001
                log.exception("processamento %s", self.name)
                self.on_event(self.name, "error", str(e))

    def _do_partial(self) -> None:
        ep = self._epoch
        with self._partial_lock:
            audio, self._partial = self._partial, None
        if audio is None or len(audio) < 16000:
            return  # menos de 1 s de áudio: o Whisper chuta e a prévia sai errada
        res = self.stt.transcribe(audio, self._lang_hint(), blocking=False,
                                  prompt=self._prev_text)
        if not res or len(res[0].split()) < 2 or is_filler(res[0]) or not self.finals.empty():
            return
        text, lang = res
        self._emit(ep, "partial_src", text)
        if self.translate_partials:
            # prévia já traduzida; se chegar um trecho final no meio, ele tem prioridade
            preview = text if lang == self.translator.dst else \
                self.translator.translate(text, remember=False)
            if preview and self.finals.empty():
                self._emit(ep, "partial_dst", preview)

    def _do_final(self, audio: np.ndarray) -> None:
        ep = self._epoch
        t0 = time.perf_counter()
        hint = self._lang_hint()
        text, lang = self.stt.transcribe(audio, hint, prompt=self._prev_text)
        t1 = time.perf_counter()
        if text and ep == self._epoch:
            # trecho curto ("e", "hm") detecta idioma errado: não deixa ele travar a conversa
            self._learn_lang(lang, detected=hint is None and len(audio) >= self.LANG_MIN_AUDIO)
            self._prev_text = text[-200:]
        if not text or is_filler(text):
            self._emit(ep, "partial_src", "")
            if text:
                log.info("%s ignorado (interjeição) | %s", self.name, text)
            return
        self._emit(ep, "src", text)
        if lang == self.translator.dst:
            # já está no idioma de destino (ex.: a pessoa falou português): só mostra
            self._emit(ep, "dst", text)
            log.info("%s [%s] sem tradução | %s", self.name, lang, text)
            return
        translated = self.translator.translate(
            text, on_partial=lambda t: self._emit(ep, "partial_dst", t)
        )
        t2 = time.perf_counter()
        self._emit(ep, "dst", translated)
        if self.output and translated and ep == self._epoch:
            self.output(translated)
        t3 = time.perf_counter()
        log.info("%s [%s] audio=%.1fs stt=%dms tr=%dms out=%dms | %s → %s", self.name, lang,
                 len(audio) / 16000, (t1 - t0) * 1000, (t2 - t1) * 1000, (t3 - t2) * 1000,
                 text, translated)
