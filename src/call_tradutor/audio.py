"""Dispositivos de áudio: loopback (o que você ouve), microfone e o cabo virtual.

Usa `soundcard`, que fala WASAPI (Windows), CoreAudio (Mac) e PulseAudio (Linux).
No Mac não existe loopback nativo: use o BlackHole como saída e aponte loopback_device para ele.
"""
import sys
import threading

import numpy as np
import soundcard as sc

SAMPLE_RATE = 16000
BLOCK = 512  # 32 ms a 16 kHz — o tamanho que o Silero VAD espera


def init_thread() -> None:
    """No Windows, cada thread que mexe com áudio precisa do COM inicializado."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED


def _match(devices, name: str):
    name = name.lower()
    for d in devices:
        if name in d.name.lower():
            return d
    return None


def get_loopback(name: str = ""):
    """Microfone 'loopback' que grava o que sai no dispositivo de saída."""
    speaker = _match(sc.all_speakers(), name) if name else sc.default_speaker()
    if speaker is None:
        raise RuntimeError(f"Saída de áudio não encontrada: {name!r}")
    return sc.get_microphone(id=str(speaker.name), include_loopback=True), speaker.name


def get_mic(name: str = ""):
    mic = _match(sc.all_microphones(), name) if name else sc.default_microphone()
    if mic is None:
        raise RuntimeError(f"Microfone não encontrado: {name!r}")
    return mic, mic.name


def get_cable(name: str):
    return _match(sc.all_speakers(), name) if name else None


def blocks(device, stop: threading.Event):
    """Gera blocos mono float32 de BLOCK amostras a 16 kHz até `stop` ser setado."""
    init_thread()
    with device.recorder(samplerate=SAMPLE_RATE, channels=1, blocksize=BLOCK) as rec:
        buf = np.zeros(0, dtype=np.float32)
        while not stop.is_set():
            data = rec.record(numframes=BLOCK)
            if data.ndim > 1:
                data = data.mean(axis=1)
            buf = np.concatenate([buf, data.astype(np.float32)])
            while len(buf) >= BLOCK:
                yield buf[:BLOCK]
                buf = buf[BLOCK:]


class CablePlayer:
    """Toca áudio no cabo virtual numa thread própria (fila), sem bloquear o pipeline."""

    def __init__(self, speaker, samplerate: int):
        self.speaker = speaker
        self.samplerate = samplerate
        self._queue: list[np.ndarray] = []
        self._cv = threading.Condition()
        self.playing = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def play(self, samples: np.ndarray) -> None:
        with self._cv:
            self._queue.append(samples.astype(np.float32))
            self._cv.notify()

    def _run(self) -> None:
        init_thread()
        with self.speaker.player(samplerate=self.samplerate, channels=1) as player:
            while True:
                with self._cv:
                    while not self._queue:
                        self._cv.wait()
                    samples = self._queue.pop(0)
                self.playing.set()
                player.play(samples)
                if not self._queue:
                    self.playing.clear()
