"""Cria uma voz do Kokoro parecida com a sua, misturando as vozes em inglês que já existem.

1. Mede o "timbre" da sua gravação (vozes/minha-voz.wav) com um modelo de reconhecimento de
   locutor (WeSpeaker ResNet34, ONNX, via sherpa-onnx).
2. Gera a mesma fala com cada voz do Kokoro e mede o quanto cada uma lembra a sua.
3. Procura a mistura (média ponderada dos vetores de estilo) que mais se aproxima.
4. Salva vozes/<nome>.npy — aparece com ★ no ⚙ do app — e uma amostra .wav para ouvir.

Não deixa o app mais lento: a voz nova é do mesmo tipo das outras.

    .venv\\Scripts\\python scripts\\criar_voz.py [--nome minha-voz] [--rodadas 120]
"""
import argparse
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from call_tradutor.gpu import setup_cuda_dlls  # noqa: E402

setup_cuda_dlls()

import numpy as np  # noqa: E402
import sherpa_onnx  # noqa: E402

from call_tradutor.config import load_config  # noqa: E402
from call_tradutor.tts import TTS  # noqa: E402

SPEAKER_MODEL = ROOT / "models" / "wespeaker_en_voxceleb_resnet34_LM.onnx"
SENTENCES = [
    "Hi, my name is Claudio, and I'm a software developer from Brazil.",
    "I really enjoy building products that people actually use.",
    "Thank you for your time. I'm happy to answer any questions about my experience.",
]


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768


def write_wav(path: Path, a: np.ndarray, sr: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(a, -1, 1) * 32767).astype(np.int16).tobytes())


def speech_only(a: np.ndarray, sr: int = 16000) -> np.ndarray:
    """Tira os silêncios (por energia em janelas de 30 ms) para o timbre não ser diluído."""
    win = int(sr * 0.03)
    frames = a[: len(a) // win * win].reshape(-1, win)
    energy = np.sqrt((frames ** 2).mean(axis=1))
    keep = energy > max(energy.max() * 0.06, 0.004)
    return frames[keep].reshape(-1)


def to16k(a: np.ndarray, sr: int) -> np.ndarray:
    n = int(len(a) * 16000 / sr)
    return np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)


class Embedder:
    def __init__(self):
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(SPEAKER_MODEL), num_threads=4)
        self.ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)

    def __call__(self, a16k: np.ndarray) -> np.ndarray:
        s = self.ex.create_stream()
        s.accept_waveform(sample_rate=16000, waveform=a16k)
        s.input_finished()
        e = np.array(self.ex.compute(s), dtype=np.float32)
        return e / np.linalg.norm(e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nome", default="minha-voz")
    ap.add_argument("--rodadas", type=int, default=120, help="misturas testadas na busca")
    ap.add_argument("--gravacao", default=str(ROOT / "vozes" / "minha-voz.wav"))
    args = ap.parse_args()

    rec = speech_only(read_wav(Path(args.gravacao)))
    print(f"sua gravação: {len(rec) / 16000:.1f} s de fala")
    if len(rec) < 16000 * 15:
        print("✗ Pouca fala na gravação (precisa de pelo menos ~15 s). Grave de novo.")
        return 1

    emb = Embedder()
    half = len(rec) // 2
    me = emb(rec)
    ceiling = float(emb(rec[:half]) @ emb(rec[half:]))
    print(f"referência (metade 1 × metade 2 da sua própria voz): {ceiling:.3f}")

    cfg = load_config()
    tts = TTS(**cfg["tts"])
    bank = np.load(cfg["tts"]["voices_path"])
    names = [n for n in bank.files if n[:3] in ("am_", "bm_", "af_", "bf_")]

    def score(style: np.ndarray) -> float:
        a = np.concatenate([tts.speak(s, voice=style) for s in SENTENCES])
        return float(me @ emb(speech_only(to16k(a, tts.samplerate))))

    t0 = time.time()
    single = sorted(((score(bank[n]), n) for n in names), reverse=True)
    print("\nvozes prontas mais parecidas:")
    for s, n in single[:8]:
        print(f"  {s:.3f}  {n}")

    # busca: misturas das 6 mais parecidas (pesos aleatórios + refinamento em volta da melhor)
    top = [n for _, n in single[:6]]
    styles = np.stack([bank[n] for n in top])
    rng = np.random.default_rng(7)
    best_w = np.eye(len(top))[0]
    best = single[0][0]
    for i in range(args.rodadas):
        if i < args.rodadas // 2:
            w = rng.dirichlet(np.ones(len(top)) * 0.7)
        else:  # perto da melhor até agora
            w = np.clip(best_w + rng.normal(0, 0.08, len(top)), 0, None)
            w = w / w.sum() if w.sum() else best_w
        s = score(np.tensordot(w, styles, axes=1).astype(np.float32))
        if s > best:
            best, best_w = s, w
            print(f"  rodada {i + 1:3d}: {s:.3f}  " +
                  ", ".join(f"{n} {x:.0%}" for n, x in zip(top, w) if x >= 0.05))

    voice = np.tensordot(best_w, styles, axes=1).astype(np.float32)
    out = ROOT / "vozes" / f"{args.nome}.npy"
    np.save(out, voice)
    sample = np.concatenate([tts.speak(s, voice=voice) for s in SENTENCES])
    write_wav(out.with_suffix(".amostra.wav"), sample, tts.samplerate)

    current = next(s for s, n in single if n == cfg["tts"]["voice"])
    print(f"\n✓ voz salva: {out.name}  ({time.time() - t0:.0f} s)")
    print(f"  parecença com você: {best:.3f}  (a atual, {cfg['tts']['voice']}: {current:.3f};"
          f" teto, você com você mesmo: {ceiling:.3f})")
    print("  mistura: " + ", ".join(f"{n} {x:.0%}" for n, x in zip(top, best_w) if x >= 0.02))
    print(f"  amostra para ouvir: {out.with_suffix('.amostra.wav').name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
