"""Testa o caminho da voz sem precisar de call: Kokoro → CABLE Input → CABLE Output.

Gera uma frase em inglês, toca no cabo e grava o que sai do outro lado ao mesmo tempo.
Se o nível gravado for audível, a pessoa da call vai ouvir a voz.

    .venv\\Scripts\\python scripts\\testar_cabo.py            # só confere
    .venv\\Scripts\\python scripts\\testar_cabo.py --ouvir    # e toca a gravação no seu fone
"""
import argparse
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from call_tradutor.gpu import setup_cuda_dlls  # noqa: E402

setup_cuda_dlls()

import numpy as np  # noqa: E402
import soundcard as sc  # noqa: E402

from call_tradutor import audio  # noqa: E402
from call_tradutor.config import load_config  # noqa: E402
from call_tradutor.tts import TTS  # noqa: E402

FRASE = "Hi, this is a test of the call translator. Can you hear me clearly?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ouvir", action="store_true", help="toca a gravação no fone no final")
    args = ap.parse_args()

    cfg = load_config()
    cable_in = audio.get_cable(cfg["audio"]["cable_device"])
    if cable_in is None:
        print("✗ CABLE Input não encontrado. Instale o VB-Cable.")
        return 1
    cable_out = next((m for m in sc.all_microphones() if "CABLE Output" in m.name), None)
    if cable_out is None:
        print("✗ CABLE Output (o 'microfone' da call) não encontrado.")
        return 1
    print(f"saída  : {cable_in.name}\nentrada: {cable_out.name}")

    tts = TTS(**cfg["tts"])
    voz = tts.speak(FRASE)
    dur = len(voz) / tts.samplerate
    print(f"voz gerada: {dur:.1f}s ({cfg['tts']['voice']})")

    sr = 16000
    gravado: list[np.ndarray] = []
    pronto = threading.Event()

    def gravar() -> None:
        audio.init_thread()
        with cable_out.recorder(samplerate=sr, channels=1) as rec:
            pronto.set()
            gravado.append(rec.record(numframes=int(sr * (dur + 1.5))))

    t = threading.Thread(target=gravar)
    t.start()
    pronto.wait(5)
    with cable_in.player(samplerate=tts.samplerate, channels=1) as p:
        p.play(voz.astype(np.float32))
    t.join()

    rec = gravado[0].reshape(-1)
    pico = float(np.abs(rec).max())
    rms = float(np.sqrt(np.mean(rec ** 2)))
    print(f"gravado: pico={pico:.3f} rms={rms:.4f}")
    if pico < 0.01:
        print("✗ Nada chegou no CABLE Output. Confira o volume do 'CABLE Input' em Som > Reprodução.")
        return 1
    print("✓ A voz em inglês chega no CABLE Output: na call, escolha 'CABLE Output' como microfone.")

    if args.ouvir:
        fone = audio.get_output(cfg["audio"]["loopback_device"])
        print(f"tocando a gravação em: {fone.name}")
        fone.play(rec, samplerate=sr, channels=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
