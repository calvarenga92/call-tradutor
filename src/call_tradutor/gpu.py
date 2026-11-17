"""Deixa as DLLs do CUDA (pacotes pip nvidia-*) visíveis para o ctranslate2 no Windows."""
import os
import sys
import sysconfig
from pathlib import Path


def setup_cuda_dlls() -> None:
    if sys.platform != "win32":
        return
    nvidia = Path(sysconfig.get_paths()["purelib"]) / "nvidia"
    if not nvidia.exists():
        return
    for bin_dir in nvidia.glob("*/bin"):
        os.add_dll_directory(str(bin_dir))
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]
