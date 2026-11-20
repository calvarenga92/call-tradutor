"""Janela de legenda: sem borda, sempre no topo, arrastável, altura fixa com rolagem.

Aparece na barra de tarefas (dá para minimizar e restaurar por lá) e o ✕ encerra o app inteiro.
A posição e a largura ficam salvas entre uma abertura e outra.
"""
import html
import time

from PySide6.QtCore import QObject, QPoint, QSettings, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QSizeGrip, QVBoxLayout, QWidget)

LISTEN, SPEAK = "ouvir", "falar"


class Bridge(QObject):
    """Ponte thread-safe: as threads de áudio emitem, a UI recebe na thread do Qt."""
    event = Signal(str, str, str)    # pipeline, tipo, texto
    status = Signal(str, bool)       # mensagem, é erro?
    toggle = Signal(str)             # "mic" | "captions" | "dub" | "coach" | "clear" | "quit"


BTN_STYLE = """
QPushButton { color: #ddd; background: rgba(255,255,255,0.08); border: none;
              border-radius: 11px; padding: 4px 11px; font-size: 12px; }
QPushButton:hover { background: rgba(255,255,255,0.18); }
QPushButton[toggle="true"] { color: #d7f5dc; background: rgba(80, 200, 120, 0.28); }
QPushButton[toggle="true"]:hover { background: rgba(80, 200, 120, 0.40); }
QPushButton[toggle="true"][off="true"] { color: #8a8a8a; background: rgba(255,255,255,0.06); }
QPushButton[win="true"] { background: transparent; color: #aaa; font-size: 14px; padding: 2px 8px; }
QPushButton[win="true"]:hover { background: rgba(255,255,255,0.15); color: #fff; }
QPushButton#close:hover { background: rgba(230, 70, 70, 0.85); color: #fff; }
QToolTip { color: #eee; background: #222; border: 1px solid #444; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: rgba(255,255,255,0.25); border-radius: 3px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
"""

HEIGHT = 400          # altura fixa da janela
PARAGRAPH_GAP_S = 2.5  # pausa da outra pessoa que abre um parágrafo novo


STATE_COLORS = {"loading": "#e6b84a", "ready": "#4fc87a", "error": "#f06464"}


def app_icon() -> QIcon:
    """Ícone simples desenhado na hora (balão verde com "EN"), para a barra de tarefas."""
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#2f9e5b"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4, 6, 56, 42, 12, 12)
    p.drawPolygon([QPoint(16, 44), QPoint(30, 44), QPoint(14, 60)])
    p.setPen(QColor("white"))
    f = p.font()
    f.setBold(True)
    f.setPixelSize(22)
    p.setFont(f)
    p.drawText(4, 6, 56, 42, Qt.AlignCenter, "EN")
    p.end()
    return QIcon(pix)


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

        self.bridge = bridge
        self.setWindowTitle("Call Tradutor")
        self.setWindowIcon(app_icon())
        # sem Qt.Tool: com ele a janela some da barra de tarefas e o ✕ não encerra o app
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(BTN_STYLE)
        self.resize(820, HEIGHT)
        self.setFixedHeight(HEIGHT)
        self._restore_geometry()

        # cabeçalho: bolinha de estado + nome | chaves | minimizar e fechar
        self.dot = QLabel("●")
        title = QLabel("Call Tradutor")
        title.setStyleSheet("color:#eee; font-size:13px; font-weight:600;")
        self.btn_mic = self._toggle_btn(
            "🎤 Fala", "Traduz a sua fala para inglês e manda a voz para a call (Ctrl+Alt+M)", "mic")
        self.btn_cap = self._toggle_btn(
            "💬 Legenda", "Mostra em português o que a outra pessoa fala (Ctrl+Alt+L)", "captions")
        self.btn_dub = self._toggle_btn("🔊 Dublagem", "Voz em português no seu fone (Ctrl+Alt+D)", "dub")
        self.btn_coach = self._toggle_btn("💡 Resposta", "Sugere o que responder (Ctrl+Alt+R)", "coach")
        btn_clear = QPushButton("🧹")
        btn_clear.setToolTip("Limpar: apaga a tela e o contexto da conversa (Ctrl+Alt+C)")
        btn_clear.clicked.connect(lambda: bridge.toggle.emit("clear"))
        btn_min = QPushButton("—")
        btn_min.setProperty("win", True)
        btn_min.setToolTip("Minimizar (volta pela barra de tarefas)")
        btn_min.clicked.connect(self.showMinimized)
        btn_close = QPushButton("✕")
        btn_close.setObjectName("close")
        btn_close.setProperty("win", True)
        btn_close.setToolTip("Encerrar o Call Tradutor (Ctrl+Alt+Q)")
        btn_close.clicked.connect(self.close)

        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(self.dot)
        top.addWidget(title)
        top.addStretch(1)
        for b in (self.btn_mic, self.btn_cap, self.btn_dub, self.btn_coach, btn_clear):
            top.addWidget(b)
        top.addSpacing(8)
        top.addWidget(btn_min)
        top.addWidget(btn_close)

        # linha de estado: o que está carregando, os aparelhos em uso ou o erro
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.set_status("carregando…")

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
        layout.addWidget(self.status)
        layout.addWidget(self.captions_area, 1)
        layout.addWidget(self.reply_area)
        layout.addLayout(bottom)

        bridge.event.connect(self.on_event)
        bridge.status.connect(self.set_status)
        self.render()

    # --- estado --------------------------------------------------------------------------
    def _toggle_btn(self, label: str, tip: str, which: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setProperty("toggle", True)
        btn.setToolTip(tip)
        btn.clicked.connect(lambda: self.bridge.toggle.emit(which))
        return btn

    def set_status(self, msg: str, error: bool = False) -> None:
        loading = not error and msg.endswith("…")
        state = "error" if error else "loading" if loading else "ready"
        self.dot.setStyleSheet(f"color:{STATE_COLORS[state]}; font-size:14px;")
        self.dot.setToolTip({"error": "erro", "loading": "carregando", "ready": "pronto"}[state])
        self.status.setText(msg)
        self.status.setStyleSheet(f"color:{'#f77' if error else '#8c8c8c'}; font-size:11px;")

    def set_toggle(self, which: str, on: bool) -> None:
        btn = {"mic": self.btn_mic, "captions": self.btn_cap, "dub": self.btn_dub,
               "coach": self.btn_coach}[which]
        btn.setToolTip(btn.toolTip().split(" — ")[0] + (" — ligado" if on else " — desligado"))
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
                en, _, pt = text.partition("")
                self.reply.setText(
                    f"<span style='color:#ffd25a; font-size:{fs - 3}px'>💡 Diga:</span> "
                    f"<span style='color:#fff3d0; font-size:{fs}px'><b>{html.escape(en)}</b></span>"
                    + (f"<br><span style='color:#bfb08a; font-size:{fs - 3}px'>{html.escape(pt)}</span>"
                       if pt else ""))
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

    # --- janela --------------------------------------------------------------------------
    def _restore_geometry(self) -> None:
        st = QSettings("call-tradutor", "janela")
        pos, width = st.value("pos"), st.value("width")
        if width:
            self.resize(int(width), HEIGHT)
        # só volta para a posição salva se ela ainda cabe numa tela (monitor desligado, etc.)
        if pos is not None and any(s.availableGeometry().contains(pos)
                                   for s in QGuiApplication.screens()):
            self.move(pos)

    def closeEvent(self, e):
        st = QSettings("call-tradutor", "janela")
        st.setValue("pos", self.pos())
        st.setValue("width", self.width())
        e.accept()
        QApplication.quit()  # fechar a janela = encerrar o app inteiro

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
