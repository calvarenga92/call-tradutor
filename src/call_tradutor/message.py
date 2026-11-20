"""Modo mensagem: grava a mensagem inteira, traduz de uma vez e entrega um áudio pronto.

Para áudios de WhatsApp (e afins): em tempo real o app grava silêncio enquanto você fala, porque
o inglês só chega depois. Aqui você fala tudo, o app traduz a frase completa (mais natural, sem
picotar em trechos) e gera um .ogg (Opus, o formato dos áudios do WhatsApp) que já vai para a área
de transferência: no WhatsApp é só Ctrl+V e enviar.
"""
import logging
import shutil
import subprocess
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

from .audio import SAMPLE_RATE, init_thread
from .config import ROOT

log = logging.getLogger("mensagem")

OUT_DIR = ROOT / "mensagens"
PAUSE_S = 0.18  # respiro entre as frases da voz gerada


class MessageMode:
    """Estados enviados por `on_state(estado, dados)`: recording, working, done, error, idle."""

    def __init__(self, stt, translator, tts, get_mic: Callable[[], object],
                 get_voice: Callable[[], object], on_state: Callable[[str, str], None]):
        self.stt = stt
        self.translator = translator
        self.tts = tts
        self.get_mic = get_mic
        self.get_voice = get_voice
        self.on_state = on_state
        self.recording = False
        self.busy = False
        self._stop = threading.Event()
        self._chunks: list[np.ndarray] = []
        self._thread: threading.Thread | None = None

    # --- gravação ---------------------------------------------------------------------------
    def start(self) -> None:
        if self.recording or self.busy:
            return
        self.recording = True
        self._stop.clear()
        self._chunks = []
        self._thread = threading.Thread(target=self._record, name="mensagem", daemon=True)
        self._thread.start()
        self.on_state("recording", "")

    def _record(self) -> None:
        init_thread()
        try:
            with self.get_mic().recorder(samplerate=SAMPLE_RATE, channels=1) as rec:
                while not self._stop.is_set():
                    self._chunks.append(rec.record(numframes=SAMPLE_RATE // 10).reshape(-1))
        except Exception as e:  # noqa: BLE001
            log.exception("gravação da mensagem")
            self.on_state("error", f"Não consegui gravar: {e}")

    def stop(self) -> None:
        """Termina a gravação e processa numa thread (transcreve, traduz, gera o áudio)."""
        if not self.recording:
            return
        self.recording = False
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, np.float32)
        self.busy = True
        threading.Thread(target=self._process, args=(audio,), name="mensagem-proc", daemon=True).start()

    def cancel(self) -> None:
        self.recording = False
        self._stop.set()
        self.on_state("idle", "")

    # --- processamento ---------------------------------------------------------------------
    def _process(self, audio: np.ndarray) -> None:
        t0 = time.perf_counter()
        try:
            if len(audio) < SAMPLE_RATE * 0.5:
                self.on_state("error", "Mensagem curta demais.")
                return
            self.on_state("working", "transcrevendo…")
            pt = self._transcribe(audio)
            if not pt:
                self.on_state("error", "Não entendi nada na gravação.")
                return
            self.on_state("working", "traduzindo…")
            en = self.translator.translate(pt, remember=False)
            self.on_state("working", "gerando a voz…")
            voice = self.get_voice()
            parts = []
            for chunk in self.tts.speak_chunks(en, voice=voice):
                parts += [chunk, np.zeros(int(self.tts.samplerate * PAUSE_S), np.float32)]
            path = self._save(np.concatenate(parts), self.tts.samplerate)
            log.info("mensagem %.1fs de fala → %s em %.1fs | %s → %s", len(audio) / SAMPLE_RATE,
                     path.name, time.perf_counter() - t0, pt, en)
            self.on_state("done", f"{path}\x1f{en}\x1f{pt}")
        except Exception as e:  # noqa: BLE001
            log.exception("mensagem")
            self.on_state("error", f"Erro na mensagem: {e}")
        finally:
            self.busy = False

    def _transcribe(self, audio: np.ndarray) -> str:
        # mensagem inteira: com o filtro de voz do Whisper, os silêncios não viram texto inventado
        with self.stt.lock:
            segments, _ = self.stt.model.transcribe(
                audio, language="pt", beam_size=5, vad_filter=True,
                hotwords=self.stt.hotwords, condition_on_previous_text=True)
            return " ".join(s.text.strip() for s in segments).strip()

    @staticmethod
    def _save(samples: np.ndarray, sr: int) -> Path:
        OUT_DIR.mkdir(exist_ok=True)
        stem = OUT_DIR / f"mensagem-{datetime.now():%Y%m%d-%H%M%S}"
        wav = stem.with_suffix(".wav")
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return wav  # sem ffmpeg: vai .wav mesmo
        ogg = stem.with_suffix(".ogg")
        # Opus mono 48 kHz: o mesmo formato das mensagens de voz do WhatsApp
        r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(wav), "-ar", "48000",
                            "-ac", "1", "-c:a", "libopus", "-b:a", "32k", str(ogg)],
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0 or not ogg.exists():
            return wav
        wav.unlink(missing_ok=True)
        return ogg
