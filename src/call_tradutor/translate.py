"""Tradução via Ollama local, com as últimas falas como contexto de conversa."""
import json
from collections import deque
from typing import Callable

import httpx

LANG_NAMES = {"en": "English", "pt": "Brazilian Portuguese"}

SYSTEM = (
    "You are a live interpreter in a voice call. Translate each message from {src} to {dst}. "
    "Reply with the translation only: no quotes, no notes, no explanations. "
    "Keep the tone natural and spoken, as a person would say it. "
    "Translate idioms and expressions by their meaning, never word for word. "
    "Keep technical terms (framework names, 'front end', 'deploy', etc.) as professionals use them. "
    "If the message is a fragment, translate the fragment as is."
)


class Translator:
    def __init__(self, url: str, model: str, src: str, dst: str, context_turns: int = 3):
        self.url = url.rstrip("/") + "/api/chat"
        self.model = model
        self.system = SYSTEM.format(src=LANG_NAMES[src], dst=LANG_NAMES[dst])
        self.history: deque[tuple[str, str]] = deque(maxlen=context_turns)
        self.client = httpx.Client(timeout=httpx.Timeout(30.0, connect=3.0))

    def warmup(self) -> None:
        """Carrega o modelo na GPU antes da primeira fala."""
        self.client.post(
            self.url.replace("/api/chat", "/api/generate"),
            json={"model": self.model, "keep_alive": "60m"},
        )

    def translate(self, text: str, on_partial: Callable[[str], None] | None = None) -> str:
        messages = [{"role": "system", "content": self.system}]
        for src, dst in self.history:
            messages += [{"role": "user", "content": src}, {"role": "assistant", "content": dst}]
        messages.append({"role": "user", "content": text})

        out = ""
        with self.client.stream(
            "POST",
            self.url,
            json={
                "model": self.model,
                "messages": messages,
                "stream": True,
                "keep_alive": "60m",
                "options": {"temperature": 0.2},
            },
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                out += chunk.get("message", {}).get("content", "")
                if on_partial:
                    on_partial(out.strip())
                if chunk.get("done"):
                    break
        out = out.strip().strip('"').strip()
        self.history.append((text, out))
        return out
