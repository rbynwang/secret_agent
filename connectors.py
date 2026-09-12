#!/usr/bin/env python3
"""Online data connectors for Secret Agent.

The language model never runs here. Connectors may use the network to retrieve
organization context, then return plain text to the local inference layer.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

GRANOLA_MCP_URL = "https://mcp.granola.ai/mcp"


def data_dir() -> Path:
    root = Path(os.environ.get("SECRET_AGENT_DATA_DIR") or (Path.home() / ".secret-agent"))
    root.mkdir(parents=True, exist_ok=True)
    return root


class FileTokenStorage:
    """Persistent MCP OAuth tokens + dynamic-client registration metadata."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                return {}
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                return {}

    def _write(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        value = self._read().get("tokens")
        return OAuthToken.model_validate(value) if value else None

    async def set_tokens(self, tokens) -> None:
        payload = self._read()
        payload["tokens"] = tokens.model_dump(mode="json")
        self._write(payload)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        value = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def set_client_info(self, client_info) -> None:
        payload = self._read()
        payload["client_info"] = client_info.model_dump(mode="json")
        self._write(payload)


class LoopbackOAuthReceiver:
    """Receive the browser OAuth redirect on an ephemeral localhost port."""

    def __init__(self) -> None:
        self.callback_url: str | None = None
        self.event = threading.Event()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                receiver.callback_url = f"http://127.0.0.1:{self.server.server_address[1]}{self.path}"
                body = (
                    "<html><body style='font-family:system-ui;padding:40px'>"
                    "<h2>Granola connected to Secret Agent</h2>"
                    "<p>You can close this tab and return to Secret Agent.</p>"
                    "</body></html>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                receiver.event.set()

            def log_message(self, fmt: str, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.redirect_uri = f"http://127.0.0.1:{self.port}/oauth/callback"
        self.thread: threading.Thread | None = None

    async def open_browser(self, authorization_url: str) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
        webbrowser.open(authorization_url)

    async def wait_for_callback(self):
        from mcp.client.auth import AuthorizationCodeResult

        ok = await asyncio.to_thread(self.event.wait, 300)
        self.server.shutdown()
        self.server.server_close()
        if not ok or not self.callback_url:
            raise RuntimeError("Granola sign-in timed out. Try Connect again.")

        params = parse_qs(urlparse(self.callback_url).query)
        if "code" not in params:
            error = params.get("error_description", params.get("error", ["OAuth authorization failed"]))[0]
            raise RuntimeError(error)
        return AuthorizationCodeResult(
            code=params["code"][0],
            state=params.get("state", [None])[0],
            iss=params.get("iss", [None])[0],
        )


def _granola_storage() -> FileTokenStorage:
    return FileTokenStorage(data_dir() / "connectors" / "granola-oauth.json")


def _granola_auth(receiver: LoopbackOAuthReceiver):
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata
    from pydantic import AnyUrl

    return OAuthClientProvider(
        server_url=GRANOLA_MCP_URL,
        client_metadata=OAuthClientMetadata(
            client_name="Secret Agent",
            redirect_uris=[AnyUrl(receiver.redirect_uri)],
        ),
        storage=_granola_storage(),
        redirect_handler=receiver.open_browser,
        callback_handler=receiver.wait_for_callback,
    )


async def _with_granola(action):
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    receiver = LoopbackOAuthReceiver()
    auth = _granola_auth(receiver)
    async with httpx2.AsyncClient(auth=auth, follow_redirects=True) as http_client:
        transport = streamable_http_client(GRANOLA_MCP_URL, http_client=http_client)
        async with Client(transport) as client:
            return await action(client)


def granola_status() -> dict[str, Any]:
    path = data_dir() / "connectors" / "granola-oauth.json"
    connected = False
    account = None
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            connected = bool(payload.get("tokens"))
            account = payload.get("account")
        except Exception:
            pass
    return {
        "id": "granola",
        "name": "Granola",
        "connected": connected,
        "account": account,
        "network_required": True,
        "description": "Meeting notes and transcripts via Granola's official MCP connector.",
    }


def connect_granola() -> dict[str, Any]:
    async def action(client):
        tools = await client.list_tools()
        names = [tool.name for tool in getattr(tools, "tools", tools)]
        account = None
        if "get_account_info" in names:
            try:
                result = await client.call_tool("get_account_info", {})
                account = _result_text(result)[:1000]
            except Exception:
                account = None
        path = data_dir() / "connectors" / "granola-oauth.json"
        if path.exists() and account:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["account"] = account
                path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            except Exception:
                pass
        return {"ok": True, "tools": names, "account": account}

    try:
        return asyncio.run(_with_granola(action))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def disconnect_granola() -> dict[str, Any]:
    path = data_dir() / "connectors" / "granola-oauth.json"
    try:
        path.unlink(missing_ok=True)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _result_text(result: Any) -> str:
    blocks = getattr(result, "content", None) or []
    parts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    if parts:
        return "\n".join(parts)
    if hasattr(result, "model_dump_json"):
        return result.model_dump_json(indent=2)
    return str(result)


def query_granola(query: str) -> dict[str, Any]:
    if not granola_status()["connected"]:
        return {"ok": False, "error": "Granola is not connected."}

    async def action(client):
        result = await client.call_tool("query_granola_meetings", {"query": query})
        return {"ok": True, "text": _result_text(result)}

    try:
        return asyncio.run(_with_granola(action))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def connector_statuses() -> dict[str, Any]:
    return {"connectors": [granola_status()]}


def should_query_granola(prompt: str) -> bool:
    if not granola_status()["connected"]:
        return False
    p = prompt.lower()
    hints = (
        "granola", "meeting", "call", "transcript", "notes", "discussed",
        "decided", "decision", "action item", "follow up", "follow-up",
        "who said", "conversation with", "standup", "stand-up",
    )
    return any(hint in p for hint in hints)


def connector_context(prompt: str) -> list[dict[str, str]]:
    contexts: list[dict[str, str]] = []
    if should_query_granola(prompt):
        result = query_granola(prompt)
        if result.get("ok") and result.get("text"):
            contexts.append({"source": "Granola", "content": str(result["text"])})
    return contexts
