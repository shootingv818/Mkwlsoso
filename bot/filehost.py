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

import hashlib
import http.server
import json
import os
import secrets
import shutil
import socket
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from config import config

_PORT = int((os.environ.get("MKWL_FILEHOST_PORT", "8080") or "8080"))
_HOST_OVERRIDE = (os.environ.get("MKWL_FILEHOST_HOST", "") or "").strip()

_server = None
_lock = threading.Lock()
_stats_lock = threading.Lock()
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
        # record the hit AFTER the body has been (attempted to be) sent, so the
        # 'bytes' field reflects what the client actually got
        try:
            _record_hit(fp, self.headers, self.client_address,
                        bytes_sent=getattr(self, "_sent", 0),
                        total_size=fp.stat().st_size)
        except Exception:  # noqa: BLE001 - stats must never break a download
            pass

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
        sent = 0
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
                sent += len(chunk)
        self._sent = sent


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
                if f.is_file() and f.name != "_hits.jsonl":
                    hits = _read_hits(d)
                    out.append({"token": d.name, "name": f.name,
                                "size": f.stat().st_size, "mtime": f.stat().st_mtime,
                                "url": link_for(d.name, f.name),
                                "hits": len(hits),
                                "unique": len({h.get("ipid") for h in hits
                                               if h.get("ipid")}),
                                "complete": sum(1 for h in hits if h.get("full"))})
    except Exception:  # noqa: BLE001
        pass
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def _ua_short(ua: str) -> str:
    """A short, human label for a User-Agent so stats are readable at a glance."""
    u = (ua or "").lower()
    if not u:
        return "?"
    if "android" in u:
        if "chrome" in u or "crmo" in u:
            return "Android Chrome"
        if "firefox" in u:
            return "Android Firefox"
        return "Android"
    if "iphone" in u or "ipad" in u or "ios" in u:
        return "iOS Safari" if "safari" in u else "iOS"
    if "windows" in u:
        return "Windows " + ("Chrome" if "chrome" in u else "Firefox" if "firefox" in u else "browser")
    if "mac os" in u or "macintosh" in u:
        return "macOS " + ("Safari" if "safari" in u and "chrome" not in u else "Chrome")
    if "linux" in u:
        return "Linux " + ("Chrome" if "chrome" in u else "Firefox" if "firefox" in u else "browser")
    if "curl" in u:
        return "curl"
    if "wget" in u:
        return "wget"
    if "bot" in u or "spider" in u or "crawler" in u:
        return "bot"
    return ua[:40]


def _hits_path(token_dir: Path) -> Path:
    return token_dir / "_hits.jsonl"


def _record_hit(fp: Path, headers, client_addr, bytes_sent: int, total_size: int) -> None:
    """Append a hit line next to the file it was served from. JSONL so appends
    are atomic enough across threads and the file never gets corrupted."""
    token_dir = fp.parent
    try:
        ip = client_addr[0] if client_addr else ""
    except Exception:  # noqa: BLE001
        ip = ""
    # one-way identifier so repeat downloaders are counted without storing IPs
    ipid = hashlib.sha1((ip or "").encode("utf-8")).hexdigest()[:12]
    ua = headers.get("User-Agent", "") or ""
    ref = headers.get("Referer", "") or ""
    rng = headers.get("Range", "") or ""
    # "complete" if the client got (near-)all the bytes in a NON-ranged GET; a
    # Range request is a resume piece and is counted as a hit, not a completion
    full = (not rng) and total_size > 0 and bytes_sent >= int(total_size * 0.95)
    row = {"t": int(time.time()), "ipid": ipid, "ua": _ua_short(ua),
           "bytes": int(bytes_sent), "total": int(total_size),
           "ranged": bool(rng), "full": bool(full),
           "ref": ref[:80] if ref else ""}
    try:
        with _stats_lock:
            with open(_hits_path(token_dir), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _read_hits(token_dir: Path) -> list:
    try:
        with open(_hits_path(token_dir), "r", encoding="utf-8") as f:
            out = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
            return out
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001
        return []


def stats_for(token: str) -> dict:
    """Click / download stats for one hosted file. Never raises."""
    try:
        d = (_root() / token).resolve()
        if _root().resolve() not in d.parents or not d.is_dir():
            return {"ok": False, "code": "unknown token"}
        hits = _read_hits(d)
        uniq_ips = {h.get("ipid") for h in hits if h.get("ipid")}
        completions = sum(1 for h in hits if h.get("full"))
        ua_counts: dict = {}
        for h in hits:
            u = h.get("ua") or "?"
            ua_counts[u] = ua_counts.get(u, 0) + 1
        last = max((h.get("t") or 0) for h in hits) if hits else 0
        first = min((h.get("t") or 0) for h in hits) if hits else 0
        name = ""
        for f in d.iterdir():
            if f.is_file() and f.name != "_hits.jsonl":
                name = f.name
                break
        return {"ok": True, "token": token, "name": name,
                "hits": len(hits), "unique_visitors": len(uniq_ips),
                "complete_downloads": completions,
                "by_platform": sorted(ua_counts.items(), key=lambda x: -x[1]),
                "first_hit": first, "last_hit": last,
                "recent": hits[-10:][::-1]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": f"{type(exc).__name__}: {exc}"}


def remove(token: str) -> bool:
    try:
        d = (_root() / token).resolve()
        if _root().resolve() in d.parents and d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False
