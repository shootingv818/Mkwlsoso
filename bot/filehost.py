"""Built-in direct-download file host.

Turn an uploaded file into a plain HTTP link that downloads on a tap. Sending a
LINK (not a file) sidesteps Eitaa's document filter entirely: the recipient taps
the link, the apk downloads, they install it. For non-technical recipients this
is the simplest path there is.

Design / safety
---------------
* Stdlib only (ThreadingHTTPServer) - nothing to pip install.
* Serves ONLY files under DATA_DIR/filehost, and only through an unguessable
  per-file token path (/<token>/<name>), so the link cannot be found by
  scanning and nothing else on the server is exposed.
* No directory listing. Path traversal is rejected.
* Sends `Content-Disposition: attachment` + the right apk MIME, so a tap
  downloads instead of opening a page.
* Supports HTTP Range (206) so a big apk resumes on a flaky connection -
  important on mobile networks.

Config (env):
    MKWL_FILEHOST_PORT   port to listen on (default 8080; 80 is most compatible)
    MKWL_FILEHOST_HOST   public IP or domain to put in links, or a full base URL
                         like https://files.example.com (default: auto-detect
                         the server's public IP)
"""
from __future__ import annotations

import http.server
import os
import secrets
import shutil
import socket
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from config import config

_PORT = int((os.environ.get("MKWL_FILEHOST_PORT", "8080") or "8080"))
_HOST_OVERRIDE = (os.environ.get("MKWL_FILEHOST_HOST", "") or "").strip()

_server = None
_lock = threading.Lock()
_public_ip_cache: str | None = None

_MIME = {
    ".apk": "application/vnd.android.package-archive",
    ".apks": "application/octet-stream",
    ".xapk": "application/octet-stream",
    ".zip": "application/zip",
    ".pdf": "application/pdf",
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".png": "image/png",
}


def _root() -> Path:
    d = config.DATA_DIR / "filehost"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _guess_mime(name: str) -> str:
    return _MIME.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "mkwl-filehost"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep the bot log clean
        return

    def _resolve(self):
        """Map the request path to a real file under the host root, or None."""
        p = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        parts = [seg for seg in p.split("/") if seg not in ("", ".", "..")]
        if len(parts) < 2:
            return None
        token, name = parts[0], parts[-1]
        if not token.isalnum() or len(token) < 8:
            return None
        root = _root().resolve()
        fp = (root / token / name).resolve()
        if root not in fp.parents or not fp.is_file():
            return None
        return fp

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path == "/healthz":
            self._send_body(b"ok", "text/plain")
            return
        fp = self._resolve()
        if fp is None:
            self.send_error(404, "Not found")
            return
        self._serve(fp, head=False)

    def do_HEAD(self):
        fp = self._resolve()
        if fp is None:
            self.send_error(404)
            return
        self._serve(fp, head=True)

    def _send_body(self, body: bytes, mime: str):
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve(self, fp: Path, head: bool):
        size = fp.stat().st_size
        name = fp.name
        start, end, partial = 0, size - 1, False
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                s, _, e = rng[6:].partition("-")
                if s.strip():
                    start = int(s)
                if e.strip():
                    end = int(e)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                partial = True
            except Exception:  # noqa: BLE001
                start, end, partial = 0, size - 1, False
        length = end - start + 1
        _stem, _ext = os.path.splitext(name)
        _astem = _stem.encode("ascii", "ignore").decode().strip().replace('"', "")
        ascii_name = (_astem or "app") + _ext
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", _guess_mime(name))
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition",
                         "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                         % (ascii_name, urllib.parse.quote(name)))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return
        with open(fp, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(262144, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)


def start(port: int | None = None) -> dict:
    """Start the host once (idempotent). Never raises; returns a status dict."""
    global _server
    with _lock:
        if _server is not None:
            return {"ok": True, "already": True,
                    "port": _server.server_address[1]}
        p = int(port or _PORT)
        try:
            srv = http.server.ThreadingHTTPServer(("0.0.0.0", p), _Handler)
        except Exception as exc:  # noqa: BLE001 - port busy / not permitted
            return {"ok": False, "code": f"{type(exc).__name__}: {exc}", "port": p}
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        _server = srv
        return {"ok": True, "port": p}


def running_port() -> int:
    return _server.server_address[1] if _server else _PORT


def _detect_ip() -> str:
    if _HOST_OVERRIDE:
        return _HOST_OVERRIDE
    global _public_ip_cache
    if _public_ip_cache:
        return _public_ip_cache
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip",
                "https://icanhazip.com"):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                ip = r.read().decode().strip()
                if ip and len(ip) <= 64:
                    _public_ip_cache = ip
                    return ip
        except Exception:  # noqa: BLE001
            continue
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


def public_base() -> str:
    host = _detect_ip()
    if host.startswith("http://") or host.startswith("https://"):
        return host.rstrip("/")
    port = running_port()
    return f"http://{host}" if port == 80 else f"http://{host}:{port}"


def link_for(token: str, name: str) -> str:
    return f"{public_base()}/{token}/{urllib.parse.quote(name)}"


def host_file(src_path: str | None = None, data: bytes | None = None,
              filename: str = "file") -> dict:
    """Place a file under a fresh token dir and return its direct link.

    Pass either src_path (a file on disk) or data (bytes). Never raises."""
    try:
        root = _root()
        token = secrets.token_hex(8)
        d = root / token
        d.mkdir(parents=True, exist_ok=True)
        safe = os.path.basename(filename) or "file"
        dest = d / safe
        if data is not None:
            dest.write_bytes(data)
        elif src_path:
            shutil.copyfile(src_path, dest)
        else:
            return {"ok": False, "code": "no source"}
        return {"ok": True, "token": token, "name": safe,
                "size": dest.stat().st_size, "url": link_for(token, safe)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": f"{type(exc).__name__}: {exc}"}


def list_hosted() -> list:
    """Every file currently hosted, newest first. For a panel listing."""
    out = []
    try:
        root = _root()
        for d in root.iterdir():
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if f.is_file():
                    out.append({"token": d.name, "name": f.name,
                                "size": f.stat().st_size, "mtime": f.stat().st_mtime,
                                "url": link_for(d.name, f.name)})
    except Exception:  # noqa: BLE001
        pass
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def remove(token: str) -> bool:
    try:
        d = (_root() / token).resolve()
        if _root().resolve() in d.parents and d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False
