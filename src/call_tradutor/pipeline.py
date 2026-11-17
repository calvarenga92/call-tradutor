"""Uma direção de tradução: captura → frases → transcrição → tradução → saída."""
import logging
import queue
import threading
import time
from typing import Callable

import numpy as np

from .audio import blocks
from .segmenter import Segmenter
from .stt import STT
from .translate import Translator

log = logging.getLogger("pipeline")

# on_event(pipeline, tipo, texto). Tipos: partial_src, src, partial_dst, dst, error
EventFn = Callable[[str, str, str], None]


class Pipeline:
    def __init__(self, name: str, device, src: str | None, stt: STT, translator: Translator,
                 segmenter: Segmenter, on_event: EventFn,
                 output: Callable[[str], None] | None = None,
                 mute_when: threading.Event | None = None):
        self.name = name
        self.device = device
        self.src = src
        self.stt = stt
        self.translator = translator
        self.segmenter = segmenter
        self.on_event = on_event
        self.output = output
        self.mute_when = mute_when  # ex.: dublagem tocando no fone → não captar o próprio áudio
        self.enabled = threading.Event()
        self.enabled.set()
        self.stop = threading.Event()
        self.finals: queue.Queue[np.ndarray] = queue.Queue()
        self._partial: np.ndarray | None = None
        self._partial_lock = threading.Lock()

    def start(self) -> None:
        threading.Thread(target=self._capture, name=f"{self.name}-cap", daemon=True).start()
        threading.Thread(target=self._work, name=f"{self.name}-work", daemon=True).start()

    def toggle(self) -> bool:
        if self.enabled.is_set():
            self.enabled.clear()
        else:
            self.enabled.set()
        return self.enabled.is_set()

    # --- captura -------------------------------------------------------------------------
    def _capture(self) -> None:
        try:
            was_enabled = True
            for block in blocks(self.device, self.stop):
                if not self.enabled.is_set() or (self.mute_when and self.mute_when.is_set()):
                    if was_enabled:
                        self.segmenter.reset()
                        was_enabled = False
                    continue
                was_enabled = True
                for kind, audio in self.segmenter.feed(block):
                    if kind == "final":
                        with self._partial_lock:
                            self._partial = None
                        self.finals.put(audio)
                    else:
                        with self._partial_lock:
                            self._partial = audio
        except Exception as e:  # noqa: BLE001
            log.exception("captura %s", self.name)
            self.on_event(self.name, "error", f"Erro na captura de áudio: {e}")

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
        with self._partial_lock:
            audio, self._partial = self._partial, None
        if audio is None:
            return
        res = self.stt.transcribe(audio, self.src, blocking=False)
        if res and res[0] and self.finals.empty():
            self.on_event(self.name, "partial_src", res[0])

    def _do_final(self, audio: np.ndarray) -> None:
        t0 = time.perf_counter()
        text, lang = self.stt.transcribe(audio, self.src)
        t1 = time.perf_counter()
        if not text:
            self.on_event(self.name, "partial_src", "")
            return
        self.on_event(self.name, "src", text)
        if lang == self.translator.dst:
            # já está no idioma de destino (ex.: a pessoa falou português): só mostra
            self.on_event(self.name, "dst", text)
            log.info("%s [%s] sem tradução | %s", self.name, lang, text)
            return
        translated = self.translator.translate(
            text, on_partial=lambda t: self.on_event(self.name, "partial_dst", t)
        )
        t2 = time.perf_counter()
        self.on_event(self.name, "dst", translated)
        if self.output and translated:
            self.output(translated)
        t3 = time.perf_counter()
        log.info("%s [%s] audio=%.1fs stt=%dms tr=%dms out=%dms | %s → %s", self.name, lang,
                 len(audio) / 16000, (t1 - t0) * 1000, (t2 - t1) * 1000, (t3 - t2) * 1000,
                 text, translated)
