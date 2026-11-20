"""Captura o som do PC inteiro, menos o do próprio app (Windows 10 2004+ / Windows 11).

Com o loopback comum, a dublagem em português que o app toca no fone volta na captura, e o app
precisava "tapar os ouvidos" enquanto ela tocava — o que a outra pessoa falava nesse meio-tempo
se perdia. Aqui o Windows entrega a mistura de todos os processos, exceto a árvore do nosso
(AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK com PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE).

A interface imita o `soundcard`: `device.recorder(samplerate, channels, blocksize)` devolve um
gravador com `.record(numframes)` em float32, para o resto do código não mudar.
"""
import ctypes
import os
import sys
import threading
import time
from ctypes import POINTER, byref, c_uint32, c_uint64, c_void_p, wintypes

# o comtypes inicializa o COM ao ser importado; as threads de áudio do app usam o modo multithread
sys.coinit_flags = 0  # COINIT_MULTITHREADED
import comtypes  # noqa: E402
import numpy as np  # noqa: E402
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown  # noqa: E402

# --- interfaces COM ----------------------------------------------------------------------


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["out"], POINTER(POINTER(ctypes.c_byte)), "data"),
                  (["out"], POINTER(c_uint32), "frames"),
                  (["out"], POINTER(wintypes.DWORD), "flags"),
                  (["out"], POINTER(c_uint64), "devpos"),
                  (["out"], POINTER(c_uint64), "qpcpos")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "frames")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "frames")),
    ]


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
                ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD)]


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Initialize",
                  (["in"], ctypes.c_int, "sharemode"), (["in"], wintypes.DWORD, "flags"),
                  (["in"], ctypes.c_longlong, "buffer"), (["in"], ctypes.c_longlong, "period"),
                  (["in"], POINTER(WAVEFORMATEX), "fmt"), (["in"], c_void_p, "session")),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint32), "n")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(ctypes.c_longlong), "l")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_uint32), "n")),
        COMMETHOD([], HRESULT, "IsFormatSupported", (["in"], ctypes.c_int, "m"),
                  (["in"], POINTER(WAVEFORMATEX), "f"), (["in"], c_void_p, "c")),
        COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(c_void_p), "f")),
        COMMETHOD([], HRESULT, "GetDevicePeriod", (["out"], POINTER(ctypes.c_longlong), "d"),
                  (["out"], POINTER(ctypes.c_longlong), "m")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], wintypes.HANDLE, "h")),
        COMMETHOD([], HRESULT, "GetService", (["in"], POINTER(GUID), "iid"),
                  (["out"], POINTER(c_void_p), "obj")),
    ]


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetActivateResult",
                  (["out"], POINTER(HRESULT), "hr"),
                  (["out"], POINTER(POINTER(IUnknown)), "obj")),
    ]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = [
        COMMETHOD([], HRESULT, "ActivateCompleted",
                  (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "op")),
    ]


class IAgileObject(IUnknown):
    """Marca o callback como chamável de qualquer thread (o Windows exige)."""
    _iid_ = GUID("{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}")
    _methods_ = []


class _Done(comtypes.COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self):
        super().__init__()
        self.event = threading.Event()

    def ActivateCompleted(self, op):  # noqa: N802 (nome da interface COM)
        self.event.set()
        return 0


# --- ativação ----------------------------------------------------------------------------


class _LoopbackParams(ctypes.Structure):
    _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", ctypes.c_int)]


class _ActivationParams(ctypes.Structure):
    _fields_ = [("ActivationType", ctypes.c_int), ("ProcessLoopbackParams", _LoopbackParams)]


class _Blob(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", c_void_p)]


class _PropVariant(ctypes.Structure):
    _fields_ = [("vt", wintypes.USHORT), ("r1", wintypes.WORD), ("r2", wintypes.WORD),
                ("r3", wintypes.WORD), ("blob", _Blob)]


ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
MODE_EXCLUDE_TREE = 1
VT_BLOB = 65
STREAMFLAGS_LOOPBACK = 0x00020000
STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
BUFFERFLAGS_SILENT = 0x2
RATE = 48000  # pedimos 48 kHz estéreo 16 bits e reduzimos para 16 kHz mono aqui


def _activate(exclude_pid: int) -> IAudioClient:
    params = _ActivationParams(ACTIVATION_TYPE_PROCESS_LOOPBACK,
                               _LoopbackParams(exclude_pid, MODE_EXCLUDE_TREE))
    pv = _PropVariant(vt=VT_BLOB)
    pv.blob = _Blob(ctypes.sizeof(params), ctypes.cast(byref(params), c_void_p))
    done = _Done()
    handler = done.QueryInterface(IActivateAudioInterfaceCompletionHandler)
    op = POINTER(IActivateAudioInterfaceAsyncOperation)()
    fn = ctypes.windll.mmdevapi.ActivateAudioInterfaceAsync
    fn.restype = HRESULT
    fn.argtypes = [wintypes.LPCWSTR, POINTER(GUID), POINTER(_PropVariant),
                   POINTER(IActivateAudioInterfaceCompletionHandler),
                   POINTER(POINTER(IActivateAudioInterfaceAsyncOperation))]
    fn("VAD\\Process_Loopback", byref(IAudioClient._iid_), byref(pv), handler, byref(op))
    if not done.event.wait(5):
        raise RuntimeError("o Windows não respondeu à captura por processo")
    hr, unk = op.GetActivateResult()
    if hr < 0:
        raise OSError(hr, "falha ao ativar a captura por processo")
    return unk.QueryInterface(IAudioClient)


class _Recorder:
    def __init__(self, exclude_pid: int, samplerate: int, blocksize: int):
        if samplerate != 16000:
            raise ValueError("process_loopback só entrega 16 kHz")
        self.client = _activate(exclude_pid)
        fmt = WAVEFORMATEX(1, 2, RATE, RATE * 4, 4, 16, 0)
        self.client.Initialize(0, STREAMFLAGS_LOOPBACK | STREAMFLAGS_AUTOCONVERTPCM
                               | STREAMFLAGS_SRC_DEFAULT_QUALITY, 2_000_000, 0, byref(fmt), None)
        ptr = self.client.GetService(byref(IAudioCaptureClient._iid_))
        self.capture = ctypes.cast(ptr, POINTER(IAudioCaptureClient))
        self.client.Start()
        self._pending = np.zeros(0, dtype=np.float32)  # 48 kHz mono ainda não reduzido
        self._out = np.zeros(0, dtype=np.float32)      # 16 kHz pronto para entregar

    def _pull(self) -> bool:
        got = False
        while True:
            n = self.capture.GetNextPacketSize()
            if not n:
                return got
            data, frames, flags, _, _ = self.capture.GetBuffer()
            if flags & BUFFERFLAGS_SILENT:
                chunk = np.zeros(frames, dtype=np.float32)
            else:
                raw = np.ctypeslib.as_array(ctypes.cast(data, POINTER(ctypes.c_int16)),
                                            shape=(frames * 2,))
                chunk = raw.reshape(-1, 2).astype(np.float32).mean(axis=1) / 32768.0
            self.capture.ReleaseBuffer(frames)
            self._pending = np.concatenate([self._pending, chunk])
            got = True
            # 48 kHz → 16 kHz: média de cada 3 amostras (filtro passa-baixa simples)
            k = len(self._pending) // 3 * 3
            if k:
                self._out = np.concatenate([self._out, self._pending[:k].reshape(-1, 3).mean(axis=1)])
                self._pending = self._pending[k:]

    def record(self, numframes: int) -> np.ndarray:
        while len(self._out) < numframes:
            if not self._pull():
                time.sleep(0.005)
        out, self._out = self._out[:numframes], self._out[numframes:]
        return out[:, None]

    def close(self) -> None:
        try:
            self.client.Stop()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class ProcessLoopback:
    """'Dispositivo' que grava todo o som do PC, menos o do processo `exclude_pid` (e filhos)."""

    def __init__(self, exclude_pid: int | None = None):
        self.exclude_pid = exclude_pid or os.getpid()
        self.name = "som do PC (menos o do app)"

    def recorder(self, samplerate: int = 16000, channels: int = 1, blocksize: int | None = None):
        return _Recorder(self.exclude_pid, samplerate, blocksize or 512)


def available() -> bool:
    """Testa se o Windows aceita a captura por processo (Windows 10 2004+)."""
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass
    try:
        _Recorder(os.getpid(), 16000, 512).close()
        return True
    except Exception:  # noqa: BLE001
        return False
