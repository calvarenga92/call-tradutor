"""Abaixa o som dos outros apps (o Meet, por exemplo) enquanto a dublagem em português toca.

É o "ducking" de rádio: a voz original cai para `level` (ex.: 40%) do volume que estava e volta
quando a dublagem termina. Mexe só no volume por app do mixer do Windows, no aparelho onde a
dublagem toca; o volume geral do Windows não muda. Ao fechar o app, tudo volta ao normal.
"""
import logging
import os
import sys
import threading
import time

sys.coinit_flags = 0  # COINIT_MULTITHREADED, igual às threads de áudio
from comtypes import CLSCTX_ALL  # noqa: E402
from pycaw.pycaw import (AudioUtilities, IAudioSessionControl2, IAudioSessionManager2,  # noqa: E402
                         ISimpleAudioVolume)

log = logging.getLogger("ducking")

FADE_S = 0.15   # transição curta, para não dar "tranco" no som
POLL_S = 0.02


def _session_manager(device_name: str):
    for d in AudioUtilities.GetAllDevices():
        if (d.FriendlyName and device_name.lower() in d.FriendlyName.lower()
                and str(getattr(d, "state", "")).endswith("Active")):
            dev = AudioUtilities.GetDeviceEnumerator().GetDevice(d.id)
            return dev.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None) \
                .QueryInterface(IAudioSessionManager2)
    return None


class Ducker:
    def __init__(self, device_name: str, playing: threading.Event, level: float = 0.4):
        self.device_name = device_name
        self.playing = playing
        self.level = max(0.0, min(level, 1.0))
        self.stop = threading.Event()
        self._ducked: dict[int, tuple[object, float]] = {}  # pid → (volume, valor original)
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="ducking", daemon=True)
        self._thread.start()

    # --- volume ---------------------------------------------------------------------------
    def _others(self):
        """Volumes por app no aparelho da dublagem, menos o do próprio app."""
        mgr = _session_manager(self.device_name)
        if mgr is None:
            return []
        out = []
        sessions = mgr.GetSessionEnumerator()
        for i in range(sessions.GetCount()):
            ctl = sessions.GetSession(i).QueryInterface(IAudioSessionControl2)
            pid = ctl.GetProcessId()
            if pid in (0, os.getpid()):
                continue  # 0 = sons do sistema
            out.append((pid, ctl.QueryInterface(ISimpleAudioVolume)))
        return out

    def _fade(self, targets: list[tuple[object, float, float]]) -> None:
        steps = max(int(FADE_S / POLL_S), 1)
        for k in range(1, steps + 1):
            for vol, start, end in targets:
                try:
                    vol.SetMasterVolume(start + (end - start) * k / steps, None)
                except Exception:  # noqa: BLE001 (app fechou no meio)
                    pass
            time.sleep(POLL_S)

    def set_level(self, level: float) -> None:
        """Muda o nível na hora (controle do ⚙). Vale a partir da próxima dublagem."""
        self.level = max(0.0, min(level, 1.0))

    def duck(self) -> None:
        with self._lock:
            targets = []
            for pid, vol in self._others():
                if pid in self._ducked:
                    continue
                orig = vol.GetMasterVolume()
                self._ducked[pid] = (vol, orig)
                targets.append((vol, orig, orig * self.level))
            self._fade(targets)

    def restore(self) -> None:
        with self._lock:
            targets = []
            for vol, orig in self._ducked.values():
                try:
                    targets.append((vol, vol.GetMasterVolume(), orig))
                except Exception:  # noqa: BLE001
                    pass
            self._ducked.clear()
            self._fade(targets)

    # --- ciclo ----------------------------------------------------------------------------
    def _run(self) -> None:
        from .audio import init_thread

        init_thread()
        ducked = False
        while not self.stop.is_set():
            try:
                if self.playing.is_set() and not ducked:
                    self.duck()
                    ducked = True
                elif not self.playing.is_set() and ducked:
                    self.restore()
                    ducked = False
            except Exception:  # noqa: BLE001
                log.exception("ducking")
            time.sleep(POLL_S)
        self.restore()

    def clear(self) -> None:  # botão Limpar: nada a fazer
        pass

    def close(self) -> None:
        """Ao fechar o app: a própria thread devolve o volume original (os objetos COM são dela)."""
        self.stop.set()
        self._thread.join(timeout=1.5)
