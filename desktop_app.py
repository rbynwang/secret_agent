#!/usr/bin/env python3
"""Native Secret Agent desktop app with fully local model inference.

The packaged desktop build contains the llama.cpp runtime. First launch only
needs to download the GGUF model weights. After that, model inference runs
entirely on localhost. Optional organization connectors may use the network,
but prompts are never sent to a cloud model.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import webview

APP_NAME = "Secret Agent"
MODEL_FILENAME = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
MODEL_URL = (
    "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/"
    "qwen2.5-1.5b-instruct-q4_k_m.gguf?download=true"
)
MODEL_SHA256 = "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e"
MODEL_DISPLAY_NAME = "Qwen2.5-1.5B-Instruct Q4_K_M"

# Pinned fallback used only when running from source without the packaged runtime.
# Production .exe/.app builds embed llama.cpp and never need this download.
LLAMA_FALLBACK_VERSION = "b10516"
LLAMA_FALLBACK_WINDOWS_X64 = (
    "https://github.com/ggml-org/llama.cpp/releases/download/b10516/"
    "llama-b10516-bin-win-cpu-x64.zip"
)
LLAMA_FALLBACK_MACOS_ARM64 = (
    "https://github.com/ggml-org/llama.cpp/releases/download/b10516/"
    "llama-b10516-bin-macos-arm64.tar.gz"
)
LLAMA_FALLBACK_MACOS_X64 = (
    "https://github.com/ggml-org/llama.cpp/releases/download/b10516/"
    "llama-b10516-bin-macos-x64.tar.gz"
)

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


def _resource_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parent


def _bundled_runtime_dir() -> Path | None:
    candidates = [
        _resource_root() / "bundled_llama",
        Path(__file__).resolve().parent / "build_resources" / "llama",
    ]
    binary_name = "llama-server.exe" if os.name == "nt" else "llama-server"
    for candidate in candidates:
        if (candidate / binary_name).exists():
            return candidate
        matches = list(candidate.rglob(binary_name)) if candidate.exists() else []
        if matches:
            return matches[0].parent
    return None


def _copy_runtime_tree(source: Path) -> None:
    target = runtime_dir()
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        dest = target / item.name
        if item.is_dir():
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)
    if os.name != "nt" and server_binary().exists():
        server_binary().chmod(server_binary().stat().st_mode | 0o111)


def _download_resumable(url: str, destination: Path, progress=None) -> None:
    """Download with an atomic .part file and resume support when the server allows it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    existing = partial.stat().st_size if partial.exists() else 0

    headers = {
        "User-Agent": "SecretAgent/0.3",
        "Accept-Encoding": "identity",
    }
    if existing:
        headers["Range"] = f"bytes={existing}-"

    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=120)
    except urllib.error.HTTPError as exc:
        # A stale partial can produce 416. Delete it and retry cleanly once.
        if exc.code == 416 and partial.exists():
            partial.unlink(missing_ok=True)
            return _download_resumable(url, destination, progress)
        raise

    with response:
        status = getattr(response, "status", 200)
        append = bool(existing and status == 206)
        if existing and not append:
            existing = 0
        mode = "ab" if append else "wb"

        content_range = response.headers.get("Content-Range") or ""
        content_length = int(response.headers.get("Content-Length") or 0)
        total = 0
        if "/" in content_range:
            try:
                total = int(content_range.rsplit("/", 1)[1])
            except ValueError:
                total = 0
        if not total and content_length:
            total = existing + content_length

        done = existing
        with partial.open(mode) as out:
            while True:
                chunk = response.read(4 * 1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)

    partial.replace(destination)


def _fallback_install_runtime(progress=None) -> None:
    """Development fallback. Packaged releases should never need this path."""
    import platform
    import tarfile
    import zipfile

    machine = platform.machine().lower()
    downloads = app_data_dir() / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)

    if os.name == "nt":
        if "arm" in machine:
            raise RuntimeError(
                "This development build does not contain the Windows ARM64 runtime. "
                "Use the packaged Secret Agent build for your architecture."
            )
        url = LLAMA_FALLBACK_WINDOWS_X64
        archive = downloads / f"llama-{LLAMA_FALLBACK_VERSION}-windows-x64.zip"
    elif sys.platform == "darwin":
        url = LLAMA_FALLBACK_MACOS_ARM64 if machine in {"arm64", "aarch64"} else LLAMA_FALLBACK_MACOS_X64
        archive = downloads / f"llama-{LLAMA_FALLBACK_VERSION}-macos.tar.gz"
    else:
        raise RuntimeError("No bundled llama.cpp runtime was found for this platform.")

    _download_resumable(url, archive, progress)
    extract_root = downloads / "llama-extracted"
    shutil.rmtree(extract_root, ignore_errors=True)
    extract_root.mkdir(parents=True, exist_ok=True)

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extract_root)
    else:
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(extract_root)

    binary_name = "llama-server.exe" if os.name == "nt" else "llama-server"
    matches = list(extract_root.rglob(binary_name))
    if not matches:
        raise RuntimeError("Downloaded llama.cpp archive did not contain llama-server.")
    _copy_runtime_tree(matches[0].parent)


def install_llama_runtime(progress=None) -> None:
    """Install llama.cpp from the copy embedded inside the desktop package."""
    bundled = _bundled_runtime_dir()
    if bundled is not None:
        _copy_runtime_tree(bundled)
    elif not server_binary().exists():
        _fallback_install_runtime(progress)

    if not server_binary().exists():
        raise RuntimeError("Secret Agent's bundled local-model runtime is missing.")


def _sha256(path: Path, progress=None) -> str:
    total = path.stat().st_size
    done = 0
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(8 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    return digest.hexdigest()


def model_installed() -> bool:
    path = model_path()
    return path.exists() and path.stat().st_size > 900_000_000


def local_ai_installed() -> bool:
    return model_installed() and server_binary().exists()


def install_model(progress=None, verify_progress=None) -> None:
    path = model_path()
    if not model_installed():
        _download_resumable(MODEL_URL, path, progress)

    actual = _sha256(path, verify_progress)
    if actual.lower() != MODEL_SHA256.lower():
        path.unlink(missing_ok=True)
        raise RuntimeError(
            "The model download was corrupted. Secret Agent removed it; press Retry to download it again."
        )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_url(url: str, timeout: float = 120.0) -> None:
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
        raise RuntimeError("Local AI is not fully installed.")

    port = _free_port()
    threads = max(2, min(8, (os.cpu_count() or 4) - 1))
    cmd = [
        str(server_binary()),
        "--model", str(model_path()),
        "--host", "127.0.0.1",
        "--port", str(port),
        "--ctx-size", "8192",
        "--threads", str(threads),
        "--no-webui",
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
.progress{height:8px;background:#ecece7;border-radius:999px;overflow:hidden;margin:16px 0 8px}.bar{height:100%;width:0;background:#20201e;transition:width .2s}.status{font-size:13px;color:#666660;min-height:20px}.error{display:none;margin-top:12px;padding:11px 12px;border-radius:10px;background:#fff1f0;color:#b42318;font-size:12px;line-height:1.45;white-space:pre-wrap}.small{font-size:12px;color:#909088;margin-top:12px}
@media(prefers-color-scheme:dark){body{background:#191917;color:#f4f4f0}.card{background:#21211f;border-color:#373733}.mark,button{background:#f2f2ed;color:#191917}.privacy{background:#30302c}p,.status,.small{color:#b7b7af}.progress{background:#373733}.bar{background:#f2f2ed}.error{background:#401c1c;color:#ffb4ab}}
</style></head>
<body><div class="card"><div class="mark">S</div><h1>Set up Secret Agent</h1>
<p>The language model runs on this computer. Secret Agent does not send prompts to OpenRouter, OpenAI, Anthropic, or another cloud model.</p>
<div class="privacy"><strong>One-time model download</strong><br>The local llama.cpp runtime is already included in this app. Setup only downloads the Apache-2.0 Qwen2.5 1.5B model. Once that finishes, normal Secret Agent inference works offline. Optional connectors such as Granola can use the internet separately.</div>
<button id="install">Download local model</button><div class="progress"><div class="bar" id="bar"></div></div><div class="status" id="status">About 1.12 GB will be stored on this computer.</div><div class="error" id="error"></div><div class="small">If the download is interrupted, Retry resumes the partial model instead of starting over.</div></div>
<script>
const btn=document.getElementById('install'), bar=document.getElementById('bar'), status=document.getElementById('status'), error=document.getElementById('error');
let timer=null;
async function poll(){
  try{
    const s=await pywebview.api.get_setup_status();
    status.textContent=s.message||'';
    bar.style.width=(s.progress||0)+'%';
    if(s.error){
      error.style.display='block';
      error.textContent=s.error;
      btn.disabled=false;
      btn.textContent='Retry';
      clearInterval(timer);
      timer=null;
    }else{error.style.display='none';error.textContent='';}
    if(s.done){bar.style.width='100%';btn.textContent='Ready';btn.disabled=true;clearInterval(timer);timer=null;}
  }catch(e){
    error.style.display='block';error.textContent='Could not read setup status: '+(e.message||String(e));
  }
}
btn.onclick=async()=>{
  btn.disabled=true;btn.textContent='Installing…';error.style.display='none';error.textContent='';
  const result=await pywebview.api.install_local_ai();
  if(!result || !result.ok){btn.disabled=false;btn.textContent='Retry';error.style.display='block';error.textContent=(result&&result.error)||'Setup could not start.';return;}
  if(timer)clearInterval(timer);timer=setInterval(poll,400);poll();
};
</script></body></html>
"""


class SetupApi:
    def __init__(self) -> None:
        self.window: Any = None
        self.status = {
            "progress": 0,
            "message": "Ready to install.",
            "done": False,
            "error": None,
        }
        self._running = False
        self._lock = threading.Lock()

    def get_setup_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.status)

    def install_local_ai(self) -> dict[str, Any]:
        with self._lock:
            if self._running:
                return {"ok": True}
            self._running = True
            self.status.update(
                progress=1,
                message="Preparing local runtime…",
                done=False,
                error=None,
            )
        threading.Thread(target=self._install, daemon=True).start()
        return {"ok": True}

    def _set(self, progress: int, message: str, error: str | None = None) -> None:
        with self._lock:
            self.status.update(
                progress=max(0, min(100, int(progress))),
                message=message,
                error=error,
            )

    def _install(self) -> None:
        try:
            self._set(2, "Preparing the bundled local runtime…")
            install_llama_runtime()
            self._set(5, "Downloading Qwen2.5 1.5B model…")

            def model_progress(done: int, total: int) -> None:
                mb = done / (1024 * 1024)
                if total:
                    total_mb = total / (1024 * 1024)
                    pct = 5 + int((done / total) * 82)
                    msg = f"Downloading local model… {mb:.0f} / {total_mb:.0f} MB"
                else:
                    pct = 5
                    msg = f"Downloading local model… {mb:.0f} MB"
                self._set(pct, msg)

            def verify_progress(done: int, total: int) -> None:
                pct = 88 + int((done / total) * 6) if total else 90
                self._set(pct, "Verifying model download…")

            install_model(model_progress, verify_progress)
            self._set(95, "Starting the on-device model…")
            app_url = start_backend()
            with self._lock:
                self.status.update(
                    progress=100,
                    message="Secret Agent is ready.",
                    done=True,
                    error=None,
                )
                self._running = False
            if self.window is not None:
                time.sleep(0.4)
                self.window.load_url(app_url)
        except urllib.error.HTTPError as exc:
            message = f"Download failed with HTTP {exc.code}. Check your internet connection and press Retry."
            self._set(self.get_setup_status().get("progress", 0), message, message)
            with self._lock:
                self._running = False
        except Exception as exc:
            detail = f"Setup failed: {type(exc).__name__}: {exc}"
            self._set(self.get_setup_status().get("progress", 0), "Setup failed. Press Retry after checking the error below.", detail)
            with self._lock:
                self._running = False


def main() -> int:
    configure_runtime()
    setup_api = SetupApi()
    try:
        # Repair/copy the bundled runtime before deciding whether setup is needed.
        # This also fixes installs created by older builds that failed while
        # downloading llama.cpp from GitHub at first launch.
        try:
            install_llama_runtime()
        except Exception:
            # Keep startup usable: the setup screen will display the actual error.
            pass

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
