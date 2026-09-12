#!/usr/bin/env python3
"""Native Secret Agent desktop app with fully local model inference.

The app downloads an on-device llama.cpp runtime and GGUF model once during
setup. After that, the model runs entirely on localhost. Optional organization
connectors may use the network, but prompts are never sent to a cloud model.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import webview

APP_NAME = "Secret Agent"
MODEL_FILENAME = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
MODEL_URL = (
    "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/"
    "qwen2.5-1.5b-instruct-q4_k_m.gguf?download=true"
)
MODEL_DISPLAY_NAME = "Qwen2.5-1.5B-Instruct Q4_K_M"
LLAMA_RELEASE_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"

_llama_process: subprocess.Popen[Any] | None = None
_web_server: Any = None


def app_data_dir() -> Path:
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


def configure_runtime() -> Path:
    data = app_data_dir()
    os.environ["SECRET_AGENT_DATA_DIR"] = str(data)
    os.environ["COMMONTASKS_DB"] = str(data / "secret_agent.db")
    os.environ["COMMONTASKS_HOST"] = "127.0.0.1"
    os.environ["SECRET_AGENT_MODEL_NAME"] = MODEL_DISPLAY_NAME
    return data


def model_path() -> Path:
    path = app_data_dir() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path / MODEL_FILENAME


def runtime_dir() -> Path:
    path = app_data_dir() / "runtime" / "llama.cpp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def server_binary() -> Path:
    name = "llama-server.exe" if os.name == "nt" else "llama-server"
    return runtime_dir() / name


def local_ai_installed() -> bool:
    return model_path().exists() and server_binary().exists()


def _download(url: str, destination: Path, progress=None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "SecretAgent/0.2"})
    with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = response.read(8 * 1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    partial.replace(destination)


def _runtime_asset() -> tuple[str, str]:
    request = urllib.request.Request(LLAMA_RELEASE_API, headers={"User-Agent": "SecretAgent/0.2"})
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)

    machine = platform.machine().lower()
    assets = release.get("assets") or []
    if os.name == "nt":
        suffix = "-bin-win-cpu-arm64.zip" if "arm" in machine else "-bin-win-cpu-x64.zip"
    elif sys.platform == "darwin":
        suffix = "-bin-macos-arm64.tar.gz" if machine in {"arm64", "aarch64"} else "-bin-macos-x64.tar.gz"
    else:
        suffix = "-bin-ubuntu-arm64.tar.gz" if "arm" in machine else "-bin-ubuntu-x64.tar.gz"

    for asset in assets:
        name = str(asset.get("name") or "")
        if name.endswith(suffix):
            return name, str(asset["browser_download_url"])
    raise RuntimeError(f"Could not find a llama.cpp runtime asset ending in {suffix}")


def install_llama_runtime(progress=None) -> None:
    if server_binary().exists():
        return
    name, url = _runtime_asset()
    archive = app_data_dir() / "downloads" / name
    _download(url, archive, progress)

    extract_root = app_data_dir() / "downloads" / "llama-extracted"
    shutil.rmtree(extract_root, ignore_errors=True)
    extract_root.mkdir(parents=True, exist_ok=True)
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extract_root)
    else:
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(extract_root)

    binary_name = "llama-server.exe" if os.name == "nt" else "llama-server"
    matches = list(extract_root.rglob(binary_name))
    if not matches:
        raise RuntimeError("llama.cpp archive did not contain llama-server")

    source_dir = matches[0].parent
    target = runtime_dir()
    for child in source_dir.iterdir():
        if child.is_file():
            shutil.copy2(child, target / child.name)
    if os.name != "nt":
        server_binary().chmod(server_binary().stat().st_mode | 0o111)


def install_model(progress=None) -> None:
    path = model_path()
    if path.exists() and path.stat().st_size > 500_000_000:
        return
    _download(MODEL_URL, path, progress)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_url(url: str, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if 200 <= response.status < 500:
                    return
        except Exception as exc:
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError(f"Local model server did not become ready: {last_error}")


def start_llama_server() -> tuple[subprocess.Popen[Any], int]:
    global _llama_process
    if not local_ai_installed():
        raise RuntimeError("Local AI runtime is not installed")

    port = _free_port()
    threads = max(2, min(8, (os.cpu_count() or 4) - 1))
    cmd = [
        str(server_binary()),
        "--model", str(model_path()),
        "--host", "127.0.0.1",
        "--port", str(port),
        "--ctx-size", "8192",
        "--threads", str(threads),
    ]
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(runtime_dir()),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    _llama_process = subprocess.Popen(cmd, **kwargs)
    _wait_for_url(f"http://127.0.0.1:{port}/health")
    os.environ["SECRET_AGENT_LLM_URL"] = f"http://127.0.0.1:{port}/v1/chat/completions"
    return _llama_process, port


def start_web_server() -> tuple[Any, str]:
    global _web_server
    from http.server import ThreadingHTTPServer
    import webapp

    webapp.seed_database(50_000)
    _web_server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    port = int(_web_server.server_address[1])
    threading.Thread(target=_web_server.serve_forever, daemon=True).start()
    return _web_server, f"http://127.0.0.1:{port}/"


def start_backend() -> str:
    start_llama_server()
    _, app_url = start_web_server()
    return app_url


SETUP_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Secret Agent Setup</title>
<style>
:root{font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color-scheme:light dark}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f7f7f5;color:#20201e}
.card{width:min(620px,calc(100% - 40px));background:#fff;border:1px solid #e4e4df;border-radius:22px;padding:34px;box-shadow:0 24px 70px #1112}
.mark{width:48px;height:48px;border-radius:14px;background:#20201e;color:#fff;display:grid;place-items:center;font-weight:800;margin-bottom:22px}
h1{font-size:34px;letter-spacing:-.04em;margin:0}p{color:#666660;line-height:1.55}.privacy{background:#f7f7f5;border-radius:14px;padding:15px;margin:22px 0;font-size:13px;line-height:1.5}
button{width:100%;height:50px;border:0;border-radius:13px;background:#20201e;color:#fff;font-size:15px;font-weight:750;cursor:pointer}button:disabled{opacity:.55}
.progress{height:8px;background:#ecece7;border-radius:999px;overflow:hidden;margin:16px 0 8px}.bar{height:100%;width:0;background:#20201e;transition:width .2s}.status{font-size:13px;color:#666660;min-height:20px}.small{font-size:12px;color:#909088;margin-top:12px}
@media(prefers-color-scheme:dark){body{background:#191917;color:#f4f4f0}.card{background:#21211f;border-color:#373733}.mark,button{background:#f2f2ed;color:#191917}.privacy{background:#30302c}p,.status,.small{color:#b7b7af}.progress{background:#373733}.bar{background:#f2f2ed}}
</style></head>
<body><div class="card"><div class="mark">S</div><h1>Install Secret Agent's local AI</h1>
<p>Secret Agent runs its language model on this computer. No prompt or model inference is sent to OpenRouter, OpenAI, Anthropic, or another cloud model.</p>
<div class="privacy"><strong>One-time setup</strong><br>Secret Agent will download llama.cpp plus an Apache-2.0 Qwen2.5 1.5B model. After installation, the model works offline. Optional connectors such as Granola can still use the internet when you choose to connect them.</div>
<button id="install">Install local AI</button><div class="progress"><div class="bar" id="bar"></div></div><div class="status" id="status">About 1.1 GB of model weights will be stored on this computer.</div><div class="small">You can delete the model later from Secret Agent's application-data folder.</div></div>
<script>
const btn=document.getElementById('install'), bar=document.getElementById('bar'), status=document.getElementById('status');
let timer=null;
async function poll(){
  try{const s=await pywebview.api.get_setup_status();status.textContent=s.message||'';bar.style.width=(s.progress||0)+'%';if(s.error){btn.disabled=false;btn.textContent='Retry';clearInterval(timer);}if(s.done){bar.style.width='100%';clearInterval(timer);}}catch(e){}
}
btn.onclick=async()=>{btn.disabled=true;btn.textContent='Installing…';await pywebview.api.install_local_ai();timer=setInterval(poll,500);poll();};
</script></body></html>
"""


class SetupApi:
    def __init__(self) -> None:
        self.window: Any = None
        self.status = {"progress": 0, "message": "Ready to install.", "done": False, "error": None}
        self._running = False

    def get_setup_status(self) -> dict[str, Any]:
        return dict(self.status)

    def install_local_ai(self) -> dict[str, Any]:
        if self._running:
            return {"ok": True}
        self._running = True
        threading.Thread(target=self._install, daemon=True).start()
        return {"ok": True}

    def _set(self, progress: int, message: str) -> None:
        self.status.update(progress=max(0, min(100, progress)), message=message)

    def _install(self) -> None:
        try:
            self._set(2, "Downloading local model runtime…")

            def runtime_progress(done: int, total: int) -> None:
                frac = (done / total) if total else 0
                self._set(2 + int(frac * 10), "Downloading llama.cpp runtime…")

            install_llama_runtime(runtime_progress)
            self._set(14, "Downloading Qwen2.5 1.5B model…")

            def model_progress(done: int, total: int) -> None:
                frac = (done / total) if total else 0
                mb = done / (1024 * 1024)
                if total:
                    total_mb = total / (1024 * 1024)
                    msg = f"Downloading local model… {mb:.0f} / {total_mb:.0f} MB"
                else:
                    msg = f"Downloading local model… {mb:.0f} MB"
                self._set(14 + int(frac * 76), msg)

            install_model(model_progress)
            self._set(92, "Starting the on-device model…")
            app_url = start_backend()
            self._set(100, "Secret Agent is ready.")
            self.status["done"] = True
            if self.window is not None:
                time.sleep(0.4)
                self.window.load_url(app_url)
        except Exception as exc:
            self.status["error"] = str(exc)
            self._set(self.status.get("progress", 0), f"Setup failed: {exc}")
            self._running = False


def main() -> int:
    configure_runtime()
    setup_api = SetupApi()
    try:
        if local_ai_installed():
            app_url = start_backend()
            webview.create_window(APP_NAME, app_url, width=1240, height=820, min_size=(900, 620))
        else:
            window = webview.create_window(
                APP_NAME,
                html=SETUP_HTML,
                js_api=setup_api,
                width=720,
                height=680,
                min_size=(560, 560),
            )
            setup_api.window = window
        webview.start()
        return 0
    finally:
        if _web_server is not None:
            try:
                _web_server.shutdown()
                _web_server.server_close()
            except Exception:
                pass
        if _llama_process is not None:
            try:
                _llama_process.terminate()
                _llama_process.wait(timeout=3)
            except Exception:
                try:
                    _llama_process.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
