"""Sugestão de resposta: quando a outra pessoa termina de falar, sugere o que você pode dizer,
usando o seu perfil profissional (perfil.md) e o histórico recente da conversa."""
import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

import httpx

from .ui import LISTEN, SPEAK

log = logging.getLogger("coach")

SYSTEM = """You help {name} during a live professional call (job interview, client or team meeting).
When the other person finishes speaking, write what {name} could say next, in the first person, \
in {lang}.

Rules:
- Use ONLY facts from the profile below. Never invent companies, numbers, dates or technologies. \
If the profile has no answer, give an honest, short answer and, if natural, connect it to related \
experience that IS in the profile.
- Sound like a person talking, not like a document: 2 to 4 short sentences, no lists, no markdown.
- Go straight to the point. Prefer one concrete example (situation, what he did, result).
- For small talk or logistics (greetings, scheduling, "can you hear me?"), answer in one short sentence.
- Reply with the suggested answer only.

PROFILE:
{profile}"""

LANGS = {"pt": "Brazilian Portuguese", "en": "English"}


class Coach:
    def __init__(self, url: str, model: str, profile_path: Path, on_event: Callable,
                 lang: str = "pt", turn_gap_s: float = 1.2, history: int = 10, name: str = "Claudio"):
        profile = profile_path.read_text(encoding="utf-8") if profile_path.exists() else ""
        self.system = SYSTEM.format(name=name, lang=LANGS.get(lang, lang), profile=profile)
        self.url = url.rstrip("/") + "/api/chat"
        self.model = model
        self.on_event = on_event
        self.turn_gap_s = turn_gap_s
        self.convo: deque[tuple[str, str]] = deque(maxlen=history)
        self.client = httpx.Client(timeout=httpx.Timeout(60.0, connect=3.0))
        self.enabled = threading.Event()
        self.enabled.set()
        self._lock = threading.Lock()
        self._last_activity = 0.0
        self._pending = False      # a outra pessoa falou algo novo desde a última sugestão
        self._generation = 0       # muda a cada atividade: cancela a sugestão em andamento
        threading.Thread(target=self._loop, name="coach", daemon=True).start()

    def clear(self) -> None:
        with self._lock:
            self.convo.clear()
            self._pending = False
            self._generation += 1  # cancela a sugestão em andamento

    # chamado pelas pipelines (mesmo formato do on_event da UI)
    def feed(self, pipeline: str, kind: str, text: str) -> None:
        with self._lock:
            if pipeline == LISTEN and kind in ("partial_src", "src") and text:
                self._last_activity = time.monotonic()
                self._generation += 1
                if kind == "src":
                    if self.convo and self.convo[-1][0] == "them":
                        self.convo[-1] = ("them", self.convo[-1][1] + " " + text)
                    else:
                        self.convo.append(("them", text))
                    self._pending = True
            elif pipeline == SPEAK and kind == "src" and text:
                if self.convo and self.convo[-1][0] == "me":
                    self.convo[-1] = ("me", self.convo[-1][1] + " " + text)
                else:
                    self.convo.append(("me", text))

    def _loop(self) -> None:
        while True:
            time.sleep(0.1)
            with self._lock:
                ready = (self._pending and self.enabled.is_set()
                         and time.monotonic() - self._last_activity >= self.turn_gap_s)
                if not ready:
                    continue
                self._pending = False
                gen = self._generation
                convo = list(self.convo)
            try:
                self._suggest(convo, gen)
            except Exception as e:  # noqa: BLE001
                log.exception("coach")
                self.on_event("coach", "error", f"sugestão: {e}")

    def _suggest(self, convo: list[tuple[str, str]], gen: int) -> None:
        transcript = "\n".join(f"{'Other person' if who == 'them' else 'Claudio'}: {t}"
                               for who, t in convo)
        messages = [
            {"role": "system", "content": self.system},
            {"role": "user", "content": f"Conversation so far:\n{transcript}\n\n"
                                        "What should Claudio say next?"},
        ]
        body = {"model": self.model, "messages": messages, "stream": True, "keep_alive": "60m",
                "options": {"temperature": 0.4, "num_predict": 160}}
        if self.model.startswith(("qwen3", "deepseek-r1")):
            body["think"] = False
        t0 = time.perf_counter()
        out = ""
        self.on_event("coach", "reply_partial", "…")
        with self.client.stream("POST", self.url, json=body) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if self._generation != gen:
                    # a pessoa voltou a falar: descarta, gera de novo quando ela parar
                    with self._lock:
                        self._pending = bool(self.convo)  # após "Limpar" a conversa está vazia
                    return
                if not line:
                    continue
                chunk = json.loads(line)
                out += chunk.get("message", {}).get("content", "")
                self.on_event("coach", "reply_partial", out.strip())
                if chunk.get("done"):
                    break
        self.on_event("coach", "reply", out.strip())
        log.info("coach %dms | %s", (time.perf_counter() - t0) * 1000, out.strip())
