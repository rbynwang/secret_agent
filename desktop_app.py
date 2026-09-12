#!/usr/bin/env python3
"""Native desktop shell for Secret Agent.

Packages the existing Secret Agent web UI + Python backend into a local desktop
application. The HTTP server only binds to localhost; organizational memory is
stored in the user's application-data directory.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

import keyring
import webview

APP_NAME = "Secret Agent"
KEYRING_SERVICE = "Secret Agent"
KEYRING_ACCOUNT = "openrouter_api_key"


def app_data_dir() -> Path:
    """Return an OS-appropriate writable application-data directory."""
    home = Path.home()
    if sys.platform == "darwin":
        root = home / "Library" / "Application Support"
    elif os.name == "nt":
        root = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))

    path = root / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def fallback_key_path() -> Path:
    return app_data_dir() / "credentials.json"


def load_api_key() -> str:
    """Load an OpenRouter key from env, OS keychain, or a restricted fallback file."""
    env_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if env_key:
        return env_key

    try:
        stored = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        if stored:
            return stored.strip()
    except Exception:
        pass

    path = fallback_key_path()
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return str(payload.get("openrouter_api_key", "")).strip()
        except Exception:
            return ""
    return ""


def save_api_key(api_key: str) -> None:
    """Persist the key in the OS keychain when possible."""
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, api_key)
        return
    except Exception:
        pass

    path = fallback_key_path()
    path.write_text(json.dumps({"openrouter_api_key": api_key}), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def configure_runtime() -> Path:
    """Point the existing backend at desktop-safe local storage."""
    data_dir = app_data_dir()
    os.environ.setdefault("COMMONTASKS_DB", str(data_dir / "secret_agent.db"))
    os.environ.setdefault("COMMONTASKS_HOST", "127.0.0.1")
    return data_dir


SETUP_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Secret Agent Setup</title>
<style>
:root{color-scheme:light dark;font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f7f7f5;color:#20201e}
.card{width:min(560px,calc(100% - 40px));background:white;border:1px solid #e4e4df;border-radius:20px;padding:32px;box-shadow:0 20px 60px #1111}
.mark{width:46px;height:46px;border-radius:13px;background:#20201e;color:white;display:grid;place-items:center;font-weight:800;margin-bottom:22px}
h1{margin:0;font-size:32px;letter-spacing:-.03em}
p{color:#666660;line-height:1.55}
label{display:block;margin:24px 0 8px;font-size:13px;font-weight:700}
input{width:100%;height:48px;border:1px solid #d2d2cb;border-radius:12px;padding:0 14px;font-size:15px;background:white;color:#20201e;outline:none}
input:focus{border-color:#777}
button{width:100%;height:48px;margin-top:14px;border:0;border-radius:12px;background:#20201e;color:white;font-weight:700;font-size:15px;cursor:pointer}
button:disabled{opacity:.55;cursor:default}
.hint{font-size:12px;color:#909088;margin-top:12px}
.error{min-height:20px;color:#b42318;font-size:13px;margin-top:10px}
@media(prefers-color-scheme:dark){
  body{background:#191917;color:#f4f4f0}
  .card{background:#21211f;border-color:#373733}
  .mark{background:#f2f2ed;color:#191917}
  p,.hint{color:#b7b7af}
  input{background:#30302c;color:#f4f4f0;border-color:#4a4a44}
  button{background:#f2f2ed;color:#191917}
}
</style>
</head>
<body>
<div class="card">
  <div class="mark">S</div>
  <h1>Set up Secret Agent</h1>
  <p>Secret Agent runs the organization's procedural memory locally on this computer. Add an OpenRouter API key for model inference.</p>
  <label for="key">OpenRouter API key</label>
  <input id="key" type="password" autocomplete="off" placeholder="sk-or-v1-…" autofocus>
  <button id="save">Continue</button>
  <div class="hint">The key is stored in your operating system credential store when available. Secret Agent's local server binds only to 127.0.0.1.</div>
  <div class="error" id="error"></div>
</div>
<script>
const key=document.getElementById('key');
const save=document.getElementById('save');
const err=document.getElementById('error');
async function submit(){
  err.textContent='';
  const value=key.value.trim();
  if(!value){err.textContent='Enter an API key to continue.';return;}
  save.disabled=true;
  save.textContent='Saving…';
  try{
    const result=await pywebview.api.save_key(value);
    if(!result || !result.ok){throw new Error((result&&result.error)||'Could not save key');}
  }catch(e){
    err.textContent=e.message||String(e);
    save.disabled=false;
    save.textContent='Continue';
  }
}
save.addEventListener('click',submit);
key.addEventListener('keydown',e=>{if(e.key==='Enter')submit()});
</script>
</body>
</html>
"""


class SetupApi:
    def __init__(self, app_url: str) -> None:
        self.app_url = app_url
        self.window: Any = None

    def save_key(self, api_key: str) -> dict[str, Any]:
        api_key = str(api_key or "").strip()
        if not api_key:
            return {"ok": False, "error": "API key is required."}

        try:
            save_api_key(api_key)
            os.environ["OPENROUTER_API_KEY"] = api_key

            # liquid_agent captures the environment value at import time, so keep
            # its module-level cache in sync for this already-running process.
            import liquid_agent
            liquid_agent.MODEL_API_KEY = api_key

            if self.window is not None:
                threading.Thread(
                    target=lambda: self.window.load_url(self.app_url),
                    daemon=True,
                ).start()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def start_local_server() -> tuple[Any, str]:
    """Start the existing Secret Agent server on an ephemeral localhost port."""
    from http.server import ThreadingHTTPServer
    import webapp

    webapp.seed_database(50_000)
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    port = int(server.server_address[1])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}/"


def main() -> int:
    configure_runtime()

    api_key = load_api_key()
    if api_key:
        os.environ["OPENROUTER_API_KEY"] = api_key

    server, app_url = start_local_server()
    setup_api = SetupApi(app_url)

    try:
        if api_key and os.environ.get("SECRET_AGENT_RESET_KEY") != "1":
            webview.create_window(
                APP_NAME,
                app_url,
                width=1240,
                height=820,
                min_size=(900, 620),
            )
        else:
            window = webview.create_window(
                APP_NAME,
                html=SETUP_HTML,
                js_api=setup_api,
                width=720,
                height=650,
                min_size=(560, 520),
            )
            setup_api.window = window

        webview.start()
        return 0
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
