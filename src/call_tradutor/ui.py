"""Janela de legenda: sem borda, sempre no topo, arrastável e redimensionável."""
import html
from collections import deque

from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSizeGrip, QVBoxLayout,
                               QWidget)

LISTEN, SPEAK = "ouvir", "falar"


class Bridge(QObject):
    """Ponte thread-safe: as threads de áudio emitem, a UI recebe na thread do Qt."""
    event = Signal(str, str, str)    # pipeline, tipo, texto
    status = Signal(str, bool)       # mensagem, é erro?
    toggle = Signal(str)             # "mic" | "captions" | "dub"


BTN_STYLE = """
QPushButton { color: #ddd; background: rgba(255,255,255,0.08); border: none;
              border-radius: 6px; padding: 3px 9px; font-size: 12px; }
QPushButton:hover { background: rgba(255,255,255,0.18); }
QPushButton[off="true"] { color: #f88; }
"""


class Overlay(QWidget):
    def __init__(self, bridge: Bridge, font_size=22, opacity=0.88, history=3):
        super().__init__()
        self.font_size = font_size
        self.opacity = opacity
        self.lines: deque[str] = deque(maxlen=history * 4)
        self.max_chars = history * 110
        self.preview = ""
        self.me = ""
        self.captions_on = True
        self._drag: QPoint | None = None

        self.setWindowTitle("call-tradutor")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(BTN_STYLE)
        self.resize(760, 260)

        self.status = QLabel("carregando…")
        self.status.setStyleSheet("color:#aaa; font-size:12px;")
        self.btn_mic = QPushButton("🎤 Fala: ON")
        self.btn_cap = QPushButton("💬 Legenda: ON")
        self.btn_dub = QPushButton("🔊 Dublagem: ON")
        self.btn_dub.clicked.connect(lambda: bridge.toggle.emit("dub"))
        btn_close = QPushButton("✕")
        self.btn_mic.clicked.connect(lambda: bridge.toggle.emit("mic"))
        self.btn_cap.clicked.connect(lambda: bridge.toggle.emit("captions"))
        btn_close.clicked.connect(self.close)

        top = QHBoxLayout()
        top.addWidget(self.status, 1)
        top.addWidget(self.btn_mic)
        top.addWidget(self.btn_cap)
        top.addWidget(self.btn_dub)
        top.addWidget(btn_close)

        self.captions = QLabel()
        self.captions.setWordWrap(True)
        self.captions.setTextFormat(Qt.RichText)
        self.captions.setAlignment(Qt.AlignLeft | Qt.AlignBottom)
        self.mine = QLabel()
        self.mine.setWordWrap(True)
        self.mine.setTextFormat(Qt.RichText)

        bottom = QHBoxLayout()
        bottom.addWidget(self.mine, 1)
        bottom.addWidget(QSizeGrip(self), 0, Qt.AlignBottom | Qt.AlignRight)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 8, 8, 8)
        layout.addLayout(top)
        layout.addWidget(self.captions, 1)
        layout.addLayout(bottom)

        bridge.event.connect(self.on_event)
        bridge.status.connect(self.set_status)
        self.render()

    # --- estado --------------------------------------------------------------------------
    def set_status(self, msg: str, error: bool = False) -> None:
        self.status.setText(msg)
        self.status.setStyleSheet(f"color:{'#f77' if error else '#aaa'}; font-size:12px;")

    def set_toggle(self, which: str, on: bool) -> None:
        btn, label = {"mic": (self.btn_mic, "🎤 Fala"), "captions": (self.btn_cap, "💬 Legenda"),
                      "dub": (self.btn_dub, "🔊 Dublagem")}[which]
        btn.setText(f"{label}: {'ON' if on else 'OFF'}")
        btn.setProperty("off", not on)
        btn.style().unpolish(btn)
        btn.style().polish(btn)
        if which == "captions":
            self.captions_on = on
            self.render()

    def on_event(self, pipeline: str, kind: str, text: str) -> None:
        if kind == "error":
            self.set_status(f"{pipeline}: {text}", error=True)
            return
        if pipeline == LISTEN:
            # só português na tela: o texto original não aparece
            if kind == "partial_dst":
                self.preview = text
            elif kind == "dst":
                self.lines.append(text)
                self.preview = ""
        else:
            if kind in ("partial_dst", "dst"):
                self.me = text
        self.render()

    # --- desenho -------------------------------------------------------------------------
    def render(self) -> None:
        fs = self.font_size
        if not self.captions_on:
            self.captions.setText("<span style='color:#666'>legenda desligada</span>")
        elif not self.lines and not self.preview:
            self.captions.setText("<span style='color:#666'>aguardando fala…</span>")
        else:
            # legenda corrida: últimas falas confirmadas (branco) + prévia (cinza)
            done = " ".join(self.lines)
            if len(done) > self.max_chars:
                done = "…" + done[-self.max_chars:].split(" ", 1)[-1]
            self.captions.setText(
                f"<span style='color:#fff; font-size:{fs}px'>{html.escape(done)}</span> "
                f"<span style='color:#9a9a9a; font-size:{fs}px'>{html.escape(self.preview)}</span>")
        if self.me:
            self.mine.setText(f"<span style='color:#7fd18b; font-size:{max(int(fs * 0.7), 13)}px'>"
                              f"Você → {html.escape(self.me)}</span>")

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(15, 15, 20, int(255 * self.opacity)))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(self.rect(), 12, 12)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _):
        self._drag = None
