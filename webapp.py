#!/usr/bin/env python3
"""Browser UI for the Secret Agent organizational-memory desktop app."""

from __future__ import annotations

import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from connectors import connector_statuses, connect_granola, disconnect_granola
from demo import list_tasks, seed_database
from local_agent import MODEL, run_agent

PRODUCT_NAME = "Secret Agent"
HOST = os.environ.get("COMMONTASKS_HOST") or os.environ.get("HOST") or "127.0.0.1"
PORT = int(os.environ.get("PORT") or os.environ.get("COMMONTASKS_PORT", "8000"))
ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"


def build_prompt(message: str, history: list[dict[str, Any]]) -> str:
    recent = history[-12:]
    lines: list[str] = []
    for item in recent:
        role = str(item.get("role", "user")).strip().lower()
        content = str(item.get("content", "")).strip()
        if content:
            lines.append(f"{role.upper()}: {content}")
    if not lines:
        return message
    return (
        "Continue this employee conversation. Case-specific facts must come from the conversation or retrieved organization context.\n\n"
        "Recent conversation:\n"
        + "\n".join(lines)
        + f"\nUSER: {message}"
    )


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 2_000_000:
            raise ValueError("invalid request size")
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        if self.path == "/api/health":
            self._json(
                200,
                {
                    "ok": True,
                    "product": PRODUCT_NAME,
                    "model": MODEL,
                    "inference": "Local device",
                    "network_for_model": False,
                    "tasks": len(list_tasks()["tasks"]),
                    **connector_statuses(),
                },
            )
            return
        if self.path == "/api/connectors":
            self._json(200, connector_statuses())
            return
        if self.path in {"/", "/index.html"}:
            return super().do_GET()
        super().do_GET()

    def do_POST(self) -> None:
        try:
            if self.path == "/api/chat":
                payload = self._read_json()
                message = str(payload.get("message", "")).strip()
                history = payload.get("history") or []
                if not message:
                    raise ValueError("message is required")
                if not isinstance(history, list):
                    raise ValueError("history must be a list")
                answer = run_agent(build_prompt(message, history), verbose=False)
                self._json(200, {"answer": answer, "model": MODEL})
                return

            if self.path == "/api/connect/granola":
                self._read_json()
                result = connect_granola()
                self._json(200 if result.get("ok") else 500, result)
                return

            if self.path == "/api/disconnect/granola":
                self._read_json()
                result = disconnect_granola()
                self._json(200 if result.get("ok") else 500, result)
                return

            self._json(404, {"error": "not found"})
        except Exception as exc:
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[web] {self.address_string()} - {fmt % args}")


def main() -> None:
    info = seed_database(50_000)
    print(
        f"{PRODUCT_NAME} ready: {info['corpus_rows']:,} procedural-memory records across "
        f"{info['tasks']} tasks in {info['database']}"
    )
    print(f"Local model: {MODEL}")
    print(f"Open http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
