"""Troca o microfone padrão do Windows conforme o 🎤 Fala.

Fala ligada  → CABLE Output vira o microfone padrão: todo app que usa o "Padrão" (WhatsApp,
               Discord, Meet no Chrome…) recebe a sua voz traduzida, sem configurar nada.
Fala desligada ou app fechado → volta o seu microfone (o mic_device do config.toml, ex. Fifine).

O Windows não tem API pública para isso; usamos a IPolicyConfig, a mesma interface interna que
ferramentas como NirCmd e AudioDeviceCmdlets usam (Windows 7 em diante). O tradutor em si grava
direto do seu microfone, então ele não é afetado pela troca.
"""
import logging
import sys
import threading

sys.coinit_flags = 0  # COINIT_MULTITHREADED, igual às threads de áudio
import comtypes  # noqa: E402
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown  # noqa: E402
from ctypes import c_int, c_void_p  # noqa: E402
from ctypes.wintypes import LPCWSTR  # noqa: E402

from pycaw.pycaw import AudioUtilities  # noqa: E402

log = logging.getLogger("default_mic")

E_CAPTURE = 1
ROLES = (0, 1, 2)  # eConsole, eMultimedia, eCommunications — apps usam papéis diferentes


def _unused(name: str, n: int):
    return COMMETHOD([], HRESULT, name, *[(["in"], c_void_p, f"a{i}") for i in range(n)])


class IPolicyConfig(IUnknown):
    _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
    _methods_ = [
        _unused("GetMixFormat", 2), _unused("GetDeviceFormat", 3), _unused("ResetDeviceFormat", 1),
        _unused("SetDeviceFormat", 3), _unused("GetProcessingPeriod", 4),
        _unused("SetProcessingPeriod", 2), _unused("GetShareMode", 2), _unused("SetShareMode", 2),
        _unused("GetPropertyValue", 3), _unused("SetPropertyValue", 3),
        COMMETHOD([], HRESULT, "SetDefaultEndpoint", (["in"], LPCWSTR, "id"), (["in"], c_int, "role")),
        _unused("SetEndpointVisibility", 2),
    ]


CLSID_POLICY_CONFIG = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")


def _capture_devices() -> dict[str, str]:
    """Microfones ativos: nome → id."""
    out = {}
    for d in AudioUtilities.GetAllDevices():
        if d.id and d.id.startswith("{0.0.1.") and str(getattr(d, "state", "")).endswith("Active"):
            out[d.FriendlyName or ""] = d.id
    return out


def _find(devices: dict[str, str], part: str) -> str | None:
    part = part.lower()
    return next((i for n, i in devices.items() if part in n.lower()), None)


def _set_default(device_id: str) -> None:
    policy = comtypes.CoCreateInstance(CLSID_POLICY_CONFIG, IPolicyConfig, comtypes.CLSCTX_ALL)
    for role in ROLES:
        policy.SetDefaultEndpoint(device_id, role)


class DefaultMic:
    def __init__(self, own_mic: str, cable: str = "CABLE Output"):
        self.own_mic = own_mic or ""
        self.cable = cable
        self._lock = threading.Lock()
        self._wanted = False  # último estado pedido: cliques rápidos não chegam fora de ordem

    def _route(self) -> None:
        from .audio import init_thread

        init_thread()
        with self._lock:
            to_cable = self._wanted
            try:
                devices = _capture_devices()
                target = _find(devices, self.cable) if to_cable else _find(devices, self.own_mic)
                if target is None:
                    log.warning("microfone não encontrado para %s", "o cabo" if to_cable else self.own_mic)
                    return
                _set_default(target)
                log.info("microfone padrão → %s", "CABLE Output" if to_cable else self.own_mic)
            except Exception:  # noqa: BLE001
                log.exception("troca do microfone padrão")

    def route(self, to_cable: bool, wait: bool = False) -> None:
        """Numa thread própria (COM em modo multithread); `wait=True` ao fechar o app."""
        self._wanted = to_cable
        t = threading.Thread(target=self._route, name="mic-padrao", daemon=True)
        t.start()
        if wait:
            t.join(timeout=3)
