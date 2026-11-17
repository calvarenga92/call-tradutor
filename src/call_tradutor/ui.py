"""Janela de legenda: sem borda, sempre no topo, arrastável, altura fixa com rolagem."""
import html
import time

from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QSizeGrip, QVBoxLayout, QWidget)

LISTEN, SPEAK = "ouvir", "falar"


class Bridge(QObject):
    """Ponte thread-safe: as threads de áudio emitem, a UI recebe na thread do Qt."""
    event = Signal(str, str, str)    # pipeline, tipo, texto
    status = Signal(str, bool)       # mensagem, é erro?
    toggle = Signal(str)             # "mic" | "captions" | "dub" | "coach" | "clear"


BTN_STYLE = """
QPushButton { color: #ddd; background: rgba(255,255,255,0.08); border: none;
              border-radius: 6px; padding: 3px 9px; font-size: 12px; }
QPushButton:hover { background: rgba(255,255,255,0.18); }
QPushButton[off="true"] { color: #f88; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: rgba(255,255,255,0.25); border-radius: 3px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
"""

HEIGHT = 400          # altura fixa da janela
PARAGRAPH_GAP_S = 2.5  # pausa da outra pessoa que abre um parágrafo novo


def _scroll(widget: QWidget, max_height: int | None = None) -> QScrollArea:
    area = QScrollArea()
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    if max_height:
        area.setMaximumHeight(max_height)
    return area


class Overlay(QWidget):
    def __init__(self, bridge: Bridge, font_size=22, opacity=0.88, history=3):
        super().__init__()
        self.font_size = font_size
        self.opacity = opacity
        # conversa inteira: [quem, texto] — "them" (outra pessoa, em PT) ou "me" (você, em EN)
        self.entries: list[list[str]] = []
        self._last_them = 0.0
        self.preview = ""
        self._stick = True  # rolagem acompanha o fim, a menos que você tenha subido
        self.captions_on = True
        self.coach_on = True
        self._drag: QPoint | None = None

        self.setWindowTitle("call-tradutor")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(BTN_STYLE)
        self.resize(820, HEIGHT)
        self.setFixedHeight(HEIGHT)

        self.status = QLabel("carregando…")
        self.status.setStyleSheet("color:#aaa; font-size:12px;")
        self.btn_mic = QPushButton("🎤 Fala: ON")
        self.btn_cap = QPushButton("💬 Legenda: ON")
        self.btn_dub = QPushButton("🔊 Dublagem: ON")
        self.btn_dub.clicked.connect(lambda: bridge.toggle.emit("dub"))
        self.btn_coach = QPushButton("💡 Resposta: ON")
        self.btn_coach.clicked.connect(lambda: bridge.toggle.emit("coach"))
        btn_clear = QPushButton("🧹 Limpar")
        btn_clear.setToolTip("Apaga a tela e o contexto da conversa (Ctrl+Alt+C)")
        btn_clear.clicked.connect(lambda: bridge.toggle.emit("clear"))
        btn_close = QPushButton("✕")
        self.btn_mic.clicked.connect(lambda: bridge.toggle.emit("mic"))
        self.btn_cap.clicked.connect(lambda: bridge.toggle.emit("captions"))
        btn_close.clicked.connect(self.close)

        top = QHBoxLayout()
        top.addWidget(self.status, 1)
        top.addWidget(self.btn_mic)
        top.addWidget(self.btn_cap)
        top.addWidget(self.btn_dub)
        top.addWidget(self.btn_coach)
        top.addWidget(btn_clear)
        top.addWidget(btn_close)

        self.captions = QLabel()
        self.captions.setWordWrap(True)
        self.captions.setTextFormat(Qt.RichText)
        self.captions.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.captions_area = _scroll(self.captions)
        bar = self.captions_area.verticalScrollBar()
        bar.rangeChanged.connect(lambda _min, mx: bar.setValue(mx) if self._stick else None)
        bar.valueChanged.connect(lambda v: setattr(self, "_stick", v >= bar.maximum() - 6))
        # bloco de sugestão de resposta
        self.reply = QLabel()
        self.reply.setWordWrap(True)
        self.reply.setTextFormat(Qt.RichText)
        self.reply.setStyleSheet("background: rgba(255, 210, 90, 0.10); border-radius: 8px;"
                                 "padding: 6px 8px;")
        self.reply.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.reply_area = _scroll(self.reply, max_height=130)
        self.reply_area.hide()

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(QSizeGrip(self), 0, Qt.AlignBottom | Qt.AlignRight)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 8, 8, 8)
        layout.addLayout(top)
        layout.addWidget(self.captions_area, 1)
        layout.addWidget(self.reply_area)
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
                      "dub": (self.btn_dub, "🔊 Dublagem"),
                      "coach": (self.btn_coach, "💡 Resposta")}[which]
        btn.setText(f"{label}: {'ON' if on else 'OFF'}")
        btn.setProperty("off", not on)
        btn.style().unpolish(btn)
        btn.style().polish(btn)
        if which == "coach":
            self.coach_on = on
            if not on:
                self.reply_area.hide()
        if which == "captions":
            self.captions_on = on
            self.render()

    def clear(self) -> None:
        self.entries.clear()
        self.preview = ""
        self._stick = True
        self.reply.setText("")
        self.reply_area.hide()
        self.render()

    def on_event(self, pipeline: str, kind: str, text: str) -> None:
        if kind == "error":
            self.set_status(f"{pipeline}: {text}", error=True)
            return
        if pipeline == "coach":
            if kind in ("reply_partial", "reply"):
                fs = max(int(self.font_size * 0.8), 14)
                self.reply.setText(f"<span style='color:#ffd25a; font-size:{fs - 3}px'>💡 Sugestão</span> "
                                   f"<span style='color:#fff3d0; font-size:{fs}px'>"
                                   f"{html.escape(text)}</span>")
                self.reply_area.setVisible(self.coach_on)
                # altura acompanha o texto (até 130 px; passou disso, rola)
                w = max(self.reply_area.viewport().width(), 200)
                self.reply_area.setFixedHeight(min(self.reply.heightForWidth(w) + 4, 130))
            return
        if pipeline == LISTEN:
            # só português na tela: o texto original não aparece
            if kind == "partial_dst":
                self.preview = text
            elif kind == "dst" and text:
                now = time.monotonic()
                last = self.entries[-1] if self.entries else None
                if last and last[0] == "them" and now - self._last_them < PARAGRAPH_GAP_S:
                    last[1] += " " + text  # continua o mesmo parágrafo
                else:
                    self.entries.append(["them", text])
                self._last_them = now
                self.preview = ""
        elif kind == "dst" and text:
            self.entries.append(["me", text])
        self.render()

    # --- desenho -------------------------------------------------------------------------
    def render(self) -> None:
        fs = self.font_size
        if not self.captions_on:
            self.captions.setText("<span style='color:#666'>legenda desligada</span>")
            return
        if not self.entries and not self.preview:
            self.captions.setText("<span style='color:#666'>aguardando fala…</span>")
            return
        small = max(int(fs * 0.7), 13)
        # prévia continua o último parágrafo, se a pessoa não fez pausa longa
        attach = bool(self.entries) and self.entries[-1][0] == "them" and             time.monotonic() - self._last_them < PARAGRAPH_GAP_S
        parts = []
        for i, (who, text) in enumerate(self.entries):
            if who == "them":
                tail = ""
                if i == len(self.entries) - 1 and self.preview and attach:
                    tail = f" <span style='color:#9a9a9a'>{html.escape(self.preview)}</span>"
                parts.append(f"<p style='margin:0 0 8px 0; color:#fff; font-size:{fs}px'>"
                             f"{html.escape(text)}{tail}</p>")
            else:
                parts.append(f"<p style='margin:0 0 8px 0; color:#7fd18b; font-size:{small}px'>"
                             f"Você → {html.escape(text)}</p>")
        if self.preview and not attach:
            parts.append(f"<p style='margin:0; color:#9a9a9a; font-size:{fs}px'>"
                         f"{html.escape(self.preview)}</p>")
        self.captions.setText("".join(parts))

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
