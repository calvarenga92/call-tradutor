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


VIRTUAL = ("cable", "vb-audio", "voicemeeter")


def is_virtual(name: str) -> bool:
    """Cabo virtual: é por onde a voz traduzida sai, nunca o que o programa deve escutar."""
    return any(v in name.lower() for v in VIRTUAL)


def _default_speaker():
    speaker = sc.default_speaker()
    if is_virtual(speaker.name):
        # acontece ao instalar o VB-Cable: o Windows passa a saída padrão para o cabo,
        # e o programa escutaria a própria voz em inglês
        raise RuntimeError(f"A saída padrão do Windows é '{speaker.name}'. Em Som > Reprodução, "
                           "defina o seu fone como padrão (ou preencha loopback_device no config.toml).")
    return speaker


def get_loopback(name: str = ""):
    """Microfone 'loopback' que grava o que sai no dispositivo de saída."""
    speaker = _match(sc.all_speakers(), name) if name else _default_speaker()
    if speaker is None:
        raise RuntimeError(f"Saída de áudio não encontrada: {name!r}")
    return sc.get_microphone(id=str(speaker.name), include_loopback=True), speaker.name


def get_mic(name: str = ""):
    mic = _match(sc.all_microphones(), name) if name else sc.default_microphone()
    if mic is None:
        raise RuntimeError(f"Microfone não encontrado: {name!r}")
    if is_virtual(mic.name):
        raise RuntimeError(f"O microfone escolhido é '{mic.name}' (o cabo virtual). Em Som > Gravação, "
                           "defina o seu microfone como padrão (ou preencha mic_device no config.toml).")
    return mic, mic.name


def get_output(name: str = ""):
    speaker = _match(sc.all_speakers(), name) if name else _default_speaker()
    if speaker is None:
        raise RuntimeError(f"Saída de áudio não encontrada: {name!r}")
    return speaker


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


class Player:
    """Toca áudio num dispositivo numa thread própria (fila), sem bloquear o pipeline.

    `playing` fica setado enquanto toca (+ `tail_s`), para quem precisa ignorar esse áudio.
    """

    def __init__(self, speaker, samplerate: int, tail_s: float = 0.4):
        self.speaker = speaker
        self.samplerate = samplerate
        self.tail_s = tail_s
        self._queue: list[np.ndarray] = []
        self._cv = threading.Condition()
        self.playing = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def play(self, samples: np.ndarray) -> None:
        with self._cv:
            self._queue.append(samples.astype(np.float32))
            self.playing.set()
            self._cv.notify()

    def _run(self) -> None:
        init_thread()
        # em todos os canais: mono (channels=1) sai só no lado esquerdo do fone
        n = self.speaker.channels if isinstance(self.speaker.channels, int) else len(self.speaker.channels)
        n = max(n, 1)
        with self.speaker.player(samplerate=self.samplerate, channels=n) as player:
            while True:
                with self._cv:
                    while not self._queue:
                        self._cv.wait()
                    samples = self._queue.pop(0)
                self.playing.set()
                # o "rabo" de silêncio cobre o atraso entre tocar e o loopback captar
                mono = np.concatenate([samples, np.zeros(int(self.samplerate * self.tail_s),
                                                         dtype=np.float32)])
                player.play(np.repeat(mono[:, None], n, axis=1))
                with self._cv:
                    if not self._queue:
                        self.playing.clear()
