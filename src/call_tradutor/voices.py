"""Catálogo de vozes para a interface: as do Kokoro + as personalizadas em `vozes/*.npy`.

Uma voz personalizada é um vetor de estilo do Kokoro (510 × 1 × 256, float32), como as que vêm
no voices-v1.0.bin — por exemplo, uma mistura criada para lembrar a voz do usuário.
"""
from pathlib import Path

import numpy as np

from .config import ROOT

CUSTOM_DIR = ROOT / "vozes"

# rótulos curtos e aproximados; quem decide é o ouvido
EN = {
    "am_michael": "Michael — americano, grave",
    "am_adam": "Adam — americano, jovem",
    "am_echo": "Echo — americano, calmo",
    "am_eric": "Eric — americano, firme",
    "am_fenrir": "Fenrir — americano, encorpado",
    "am_liam": "Liam — americano, leve",
    "am_onyx": "Onyx — americano, bem grave",
    "am_puck": "Puck — americano, animado",
    "bm_george": "George — britânico, maduro",
    "bm_lewis": "Lewis — britânico, grave",
    "bm_daniel": "Daniel — britânico, neutro",
    "bm_fable": "Fable — britânico, narrador",
    "af_heart": "Heart — americana",
    "af_bella": "Bella — americana",
    "bf_emma": "Emma — britânica",
}
PT = {
    "pm_alex": "Alex — masculina",
    "pm_santa": "Santa — masculina, grave",
    "pf_dora": "Dora — feminina",
}
SAMPLE = {"en": "Hi, this is how I will sound in the call.",
          "pt": "Oi, é assim que eu vou soar na dublagem."}


def custom() -> dict[str, str]:
    """Vozes personalizadas: id "custom:<nome>" → rótulo com estrela."""
    if not CUSTOM_DIR.exists():
        return {}
    return {f"custom:{p.stem}": f"★ {p.stem.replace('-', ' ').replace('_', ' ')} — personalizada"
            for p in sorted(CUSTOM_DIR.glob("*.npy"))}


def catalog(lang: str) -> dict[str, str]:
    """Vozes para a lista da interface. As personalizadas vêm primeiro (servem para os dois idiomas)."""
    return {**custom(), **(EN if lang == "en" else PT)}


def resolve(voice_id: str):
    """O que o Kokoro recebe: o nome da voz ou o vetor de uma voz personalizada."""
    if voice_id.startswith("custom:"):
        path = CUSTOM_DIR / f"{voice_id.split(':', 1)[1]}.npy"
        return np.load(path).astype(np.float32)
    return voice_id
