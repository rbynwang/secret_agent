#!/usr/bin/env python3
"""Secret Agent — browser UI.

Serves the chat UI and three answer modes over the company memory:
  agent          SLM (Liquid via OpenRouter) + the 12 skills   (needs OPENROUTER_API_KEY)
  deterministic  skills + template composition, no model, no network
  naive          keyword top-k only (condition B) — the live A/B comparison

With no API key set, 'agent' quietly falls back to 'deterministic' so the
public site never hard-fails.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict, deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from secretagent.agent import (MODEL, MODEL_API_KEY, run_agent, run_deterministic,
                               run_naive)
from secretagent.config import load_company
from secretagent.memory import Memory

HOST = os.environ.get("COMMONTASKS_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", os.environ.get("COMMONTASKS_PORT", "8000")))
ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
COMPANY = os.environ.get("SECRETAGENT_COMPANY", str(ROOT / "companies/clarion/config.json"))

MEMORY = Memory(load_company(COMPANY))

# crude per-IP throttle so a public deploy can't drain the free model quota
_hits: dict[str, deque] = defaultdict(deque)
RATE, WINDOW = 20, 60.0


def _throttled(ip: str) -> bool:
    q = _hits[ip]
    now = time.time()
    while q and now - q[0] > WINDOW:
        q.popleft()
    if len(q) >= RATE:
        return True
    q.append(now)
    return False


def build_prompt(message: str, history: list[dict[str, Any]]) -> str:
    lines = []
    for item in history[-10:]:
        content = str(item.get("content", "")).strip()
        if content:
            lines.append(f"{str(item.get('role', 'user')).upper()}: {content[:600]}")
    if not lines:
        return message
    return ("Recent conversation (for context; retrieve evidence fresh):\n"
            + "\n".join(lines) + f"\nUSER: {message}")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/api/health":
            self._json(200, {
                "ok": True, "company": MEMORY.cfg.name, "as_of": MEMORY.cfg.today,
                "claims": len(MEMORY.claims), "nodes": len(MEMORY.nodes),
                "model": MODEL if MODEL_API_KEY else None,
                "default_mode": "agent" if MODEL_API_KEY else "deterministic"})
            return
        if self.path == "/":
            self.path = "/landing.html"
        elif self.path in ("/app", "/app/"):
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        if self.path != "/api/chat":
            self._json(404, {"error": "not found"})
            return
        if _throttled(self.client_address[0]):
            self._json(429, {"error": "rate limit: please slow down"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 200_000:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode())
            message = str(payload.get("message", "")).strip()
            if not message:
                raise ValueError("message is required")
            history = payload.get("history") or []
            mode = str(payload.get("mode", "agent"))

            if mode == "naive":
                out = run_naive(MEMORY, message)
            elif mode == "agent" and MODEL_API_KEY:
                out = run_agent(MEMORY, build_prompt(message, history))
            else:
                mode = "deterministic"
                out = run_deterministic(MEMORY, message)
            out["mode"] = mode
            self._json(200, out)
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[web] {self.address_string()} - {fmt % args}")


def main() -> None:
    print(f"Secret Agent · {MEMORY.cfg.name} · {len(MEMORY.claims)} claims / "
          f"{len(MEMORY.nodes)} graph nodes · as-of {MEMORY.cfg.today}")
    print("Inference: " + (f"{MODEL} via OpenRouter" if MODEL_API_KEY
                           else "no key set — deterministic mode"))
    print(f"Open http://localhost:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
