import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_config(path: Path | None = None) -> dict:
    path = path or ROOT / "config.toml"
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    # caminhos relativos ficam relativos à raiz do projeto
    for key in ("model_path", "voices_path"):
        p = Path(cfg["tts"][key])
        if not p.is_absolute():
            cfg["tts"][key] = str(ROOT / p)
    return cfg
