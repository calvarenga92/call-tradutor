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
    toggle = Signal(str)             # "mic" | "captions"


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
        self.history: deque[tuple[str, str]] = deque(maxlen=history)
        self.current = {"partial": "", "src": "", "dst": ""}
        self.me = {"src": "", "dst": ""}
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
        btn_close = QPushButton("✕")
        self.btn_mic.clicked.connect(lambda: bridge.toggle.emit("mic"))
        self.btn_cap.clicked.connect(lambda: bridge.toggle.emit("captions"))
        btn_close.clicked.connect(self.close)

        top = QHBoxLayout()
        top.addWidget(self.status, 1)
        top.addWidget(self.btn_mic)
        top.addWidget(self.btn_cap)
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
        btn = self.btn_mic if which == "mic" else self.btn_cap
        label = "🎤 Fala" if which == "mic" else "💬 Legenda"
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
            c = self.current
            if kind == "partial_src":
                c["partial"] = text
            elif kind == "src":
                c.update(src=text, partial="", dst="")
            elif kind == "partial_dst":
                c["dst"] = text
            elif kind == "dst":
                self.history.append((c["src"], text))
                c.update(src="", partial="", dst="")
        else:
            if kind == "src":
                self.me.update(src=text, dst="…")
            elif kind in ("partial_dst", "dst"):
                self.me["dst"] = text
        self.render()

    # --- desenho -------------------------------------------------------------------------
    def _entry(self, src: str, dst: str) -> str:
        fs, small = self.font_size, max(int(self.font_size * 0.6), 11)
        return (f"<div style='margin-bottom:6px'>"
                f"<span style='color:#fff; font-size:{fs}px'>{html.escape(dst)}</span><br>"
                f"<span style='color:#8a8a8a; font-size:{small}px'>{html.escape(src)}</span>"
                f"</div>")

    def render(self) -> None:
        if not self.captions_on:
            self.captions.setText("<span style='color:#666'>legenda desligada</span>")
        else:
            parts = [self._entry(s, d) for s, d in self.history]
            c = self.current
            if c["src"]:
                parts.append(self._entry(c["src"], c["dst"] or "…"))
            elif c["partial"]:
                small = max(int(self.font_size * 0.6), 11)
                parts.append(f"<i style='color:#9a9a9a; font-size:{small}px'>"
                             f"{html.escape(c['partial'])}</i>")
            self.captions.setText("".join(parts) or
                                  "<span style='color:#666'>aguardando fala em inglês…</span>")
        if self.me["src"]:
            self.mine.setText(f"<span style='color:#7fd18b; font-size:13px'>Você: "
                              f"{html.escape(self.me['src'])} → <b>{html.escape(self.me['dst'])}</b>"
                              f"</span>")

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
