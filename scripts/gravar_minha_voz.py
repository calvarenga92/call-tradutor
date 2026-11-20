"""Grava ~45 s da sua voz no microfone do config.toml, para criar uma voz parecida com a sua.

Abre uma janela com o texto para ler e o botão Gravar. Salva em vozes/minha-voz.wav (fora do git).
Depois: .venv\\Scripts\\python scripts\\criar_voz.py
"""
import sys
import threading
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
from PySide6.QtCore import QTimer, Qt  # noqa: E402
from PySide6.QtWidgets import (QApplication, QLabel, QProgressBar, QPushButton,  # noqa: E402
                               QVBoxLayout, QWidget)

from call_tradutor import audio  # noqa: E402
from call_tradutor.config import load_config  # noqa: E402

SECONDS = 45
RATE = 16000
OUT = ROOT / "vozes" / "minha-voz.wav"

TEXT = """Hi, my name is Claudio, and I'm a software developer from Brazil.
I started my career as a web designer, then I moved to front-end development,
and today I work as a full-stack developer.

I really enjoy building products that people actually use.
Most of my work was in finance and retail, with React, TypeScript and Node.

In my free time, I like to study new technologies, play games,
and record videos for my channel.

Thank you for your time. I'm looking forward to talking with you,
and I'm happy to answer any questions about my experience.

(se acabar antes do tempo, leia de novo — pode ser no seu ritmo normal)"""


class Recorder(QWidget):
    def __init__(self, mic):
        super().__init__()
        self.mic = mic
        self.samples: list[np.ndarray] = []
        self.stop = threading.Event()
        self.setWindowTitle("Gravar minha voz")
        self.resize(640, 520)
        self.setStyleSheet("QWidget{background:#18181d;color:#eee;font-size:15px}"
                           "QPushButton{background:#2f9e5b;border:none;border-radius:8px;padding:10px;"
                           "font-size:16px;font-weight:600}QPushButton:disabled{background:#444}"
                           "QProgressBar{border:none;background:#2a2a30;border-radius:6px;height:14px}"
                           "QProgressBar::chunk{background:#2f9e5b;border-radius:6px}")
        info = QLabel(f"Microfone: {mic.name}\nLeia em voz alta, com a sua voz normal, "
                      f"perto do microfone. São {SECONDS} segundos.")
        info.setStyleSheet("color:#aaa;font-size:13px")
        text = QLabel(TEXT)
        text.setWordWrap(True)
        text.setStyleSheet("font-size:18px;line-height:150%;padding:8px 0")
        self.bar = QProgressBar()
        self.bar.setRange(0, SECONDS * 10)
        self.bar.setTextVisible(False)
        self.btn = QPushButton("● Gravar")
        self.btn.clicked.connect(self.start)
        self.status = QLabel("")
        self.status.setAlignment(Qt.AlignCenter)
        lay = QVBoxLayout(self)
        for w in (info, text, self.bar, self.btn, self.status):
            lay.addWidget(w)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)

    def start(self):
        self.btn.setEnabled(False)
        self.btn.setText("Gravando… leia o texto")
        self.elapsed = 0
        threading.Thread(target=self.record, daemon=True).start()
        self.timer.start(100)

    def record(self):
        audio.init_thread()
        with self.mic.recorder(samplerate=RATE, channels=1) as rec:
            while not self.stop.is_set():
                self.samples.append(rec.record(numframes=RATE // 10).reshape(-1))

    def tick(self):
        self.elapsed += 1
        self.bar.setValue(self.elapsed)
        if self.elapsed >= SECONDS * 10:
            self.timer.stop()
            self.stop.set()
            QTimer.singleShot(300, self.save)

    def save(self):
        a = np.concatenate(self.samples)
        OUT.parent.mkdir(exist_ok=True)
        with wave.open(str(OUT), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes((np.clip(a, -1, 1) * 32767).astype(np.int16).tobytes())
        peak = float(np.abs(a).max())
        self.btn.setText("✓ Gravado")
        self.status.setText(f"Salvo ({len(a) / RATE:.0f} s, volume {peak:.2f}). "
                            + ("Pode fechar esta janela." if peak > 0.05 else
                               "Ficou baixo demais — confira o microfone e grave de novo."))
        print(f"salvo: {OUT} | {len(a) / RATE:.1f}s | pico {peak:.3f}", flush=True)


def main():
    cfg = load_config()
    mic, _ = audio.get_mic(cfg["audio"]["mic_device"])
    app = QApplication(sys.argv)
    w = Recorder(mic)
    w.show()
    app.exec()


if __name__ == "__main__":
    main()
