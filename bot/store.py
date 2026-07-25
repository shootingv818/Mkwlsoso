"""Persistent bot state: settings, content, and the active account.

Everything is a single JSON file under DATA_DIR/bot_state.json so the panel
keeps its configuration across restarts. No secrets are stored here.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from config import config

_LOCK = threading.Lock()


def _defaults() -> dict[str, Any]:
    return {
        "settings": {
            "text_send_delay": float(config.TEXT_SEND_DELAY),
            "contact_create_delay": float(config.CONTACT_CREATE_DELAY),
            "send_log_every": int(config.SEND_LOG_EVERY),
            # "bridge" (browser/tweb) or "direct" (browser-free MTProto).
            "engine": str(config.ENGINE),
        },
        # content to send: kind is "text" or "file".
        "content": {
            "kind": None,
            "text": "",
            "file_path": "",
            "file_name": "",
            "caption": "",
        },
        "active_account": None,
        # per-account metadata: name -> {"phone": str, "contacts": int, "pvs": int}
        "accounts_meta": {},
    }


class Store:
    def __init__(self) -> None:
        self.path: Path = config.DATA_DIR / "bot_state.json"
        self._data: dict[str, Any] = _defaults()
        self.load()

    # ---- persistence ----
    def load(self) -> None:
        with _LOCK:
            if self.path.is_file():
                try:
                    disk = json.loads(self.path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001 - corrupt file -> keep defaults
                    disk = {}
                merged = _defaults()
                for key, val in disk.items():
                    if isinstance(val, dict) and isinstance(merged.get(key), dict):
                        merged[key].update(val)
                    else:
                        merged[key] = val
                self._data = merged

    def save(self) -> None:
        with _LOCK:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)

    # ---- settings ----
    @property
    def settings(self) -> dict[str, Any]:
        return self._data["settings"]

    def set_setting(self, key: str, value: Any) -> None:
        self._data["settings"][key] = value
        self.save()

    @property
    def text_send_delay(self) -> float:
        return float(self._data["settings"].get("text_send_delay", config.TEXT_SEND_DELAY))

    @property
    def contact_create_delay(self) -> float:
        return float(self._data["settings"].get("contact_create_delay", config.CONTACT_CREATE_DELAY))

    @property
    def send_log_every(self) -> int:
        return int(self._data["settings"].get("send_log_every", config.SEND_LOG_EVERY))

    @property
    def engine(self) -> str:
        eng = str(self._data["settings"].get("engine", config.ENGINE))
        return eng if eng in ("bridge", "direct") else "bridge"

    def set_engine(self, engine: str) -> None:
        self._data["settings"]["engine"] = "direct" if engine == "direct" else "bridge"
        self.save()

    def toggle_engine(self) -> str:
        new = "direct" if self.engine == "bridge" else "bridge"
        self.set_engine(new)
        return new

    # ---- per-account metadata (phone / contacts / pvs) ----
    @property
    def accounts_meta(self) -> dict[str, Any]:
        return self._data.setdefault("accounts_meta", {})

    def account_meta(self, name: str) -> dict[str, Any]:
        return dict(self._data.setdefault("accounts_meta", {}).get(name, {}))

    def set_account_meta(self, name: str, **fields: Any) -> None:
        meta = self._data.setdefault("accounts_meta", {})
        cur = dict(meta.get(name, {}))
        for k, v in fields.items():
            if v is not None:
                cur[k] = v
        meta[name] = cur
        self.save()

    def account_phone(self, name: str) -> str:
        """The account's own phone (digits) if known, else the account name."""
        return str(self._data.get("accounts_meta", {}).get(name, {}).get("phone") or name)

    # ---- content ----
    @property
    def content(self) -> dict[str, Any]:
        return self._data["content"]

    def set_text_content(self, text: str) -> None:
        self._data["content"] = {
            "kind": "text", "text": text,
            "file_path": "", "file_name": "", "caption": "",
        }
        self.save()

    def set_file_content(self, file_path: str, file_name: str, caption: str = "") -> None:
        self._data["content"] = {
            "kind": "file", "text": "",
            "file_path": file_path, "file_name": file_name, "caption": caption,
        }
        self.save()

    def clear_content(self) -> None:
        self._data["content"] = _defaults()["content"]
        self.save()

    def content_summary(self) -> str:
        c = self._data["content"]
        if c.get("kind") == "text":
            t = (c.get("text") or "").replace("\n", " ")
            return f"Text ({len(c.get('text') or '')} chars): {t[:60]}"
        if c.get("kind") == "file":
            cap = c.get("caption") or ""
            extra = f" + caption ({len(cap)} chars)" if cap else ""
            return f"File: {c.get('file_name')}{extra}"
        return "not set"

    # ---- active account ----
    @property
    def active_account(self) -> str | None:
        return self._data.get("active_account")

    def set_active_account(self, account: str | None) -> None:
        self._data["active_account"] = account
        self.save()


store = Store()
