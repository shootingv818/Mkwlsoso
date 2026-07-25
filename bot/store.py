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
        # accounts ticked for a multi-account (simultaneous) send.
        "selected_accounts": [],
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
        """A COPY of the settings, with `engine` already resolved.

        Jobs receive this dict and read `settings["engine"]` from it, so the
        effective engine has to be baked in here -- otherwise a stale stored
        "direct" would still route jobs to the browser-free engine even though
        the panel is bridge-only. The stored value itself is left untouched so
        MKWL_ENABLE_DIRECT=1 restores the previous choice.
        """
        out = dict(self._data["settings"])
        out["engine"] = self.engine
        return out

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
        """The engine every job uses.

        The panel is bridge-only, so this reports "bridge" no matter what is
        stored -- unless MKWL_ENABLE_DIRECT=1 brings the switch back. The stored
        value is left untouched so enabling the flag restores the old choice.
        """
        if not config.ENABLE_DIRECT:
            return "bridge"
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
        """The account's own phone (digits) if known, else the account name.

        Accounts are now created with the phone digits AS their profile name,
        so the fallback is already the phone for anything added from the panel.
        """
        return str(self._data.get("accounts_meta", {}).get(name, {}).get("phone") or name)

    def remove_account(self, name: str) -> None:
        """Forget everything the panel remembers about an account.

        Called when the owner deletes an account; the browser profile itself is
        removed by the caller (it is a directory, not panel state).
        """
        self._data.setdefault("accounts_meta", {}).pop(name, None)
        sel = [a for a in self._data.get("selected_accounts", []) if a != name]
        self._data["selected_accounts"] = sel
        if self._data.get("active_account") == name:
            self._data["active_account"] = None
        self.save()

    # ---- multi-account selection (simultaneous send) ----
    @property
    def selected_accounts(self) -> list[str]:
        sel = self._data.get("selected_accounts")
        return list(sel) if isinstance(sel, list) else []

    def is_selected(self, name: str) -> bool:
        return name in self.selected_accounts

    def toggle_selected(self, name: str) -> bool:
        """Tick/untick an account. Returns the new state (True = selected)."""
        sel = self.selected_accounts
        if name in sel:
            sel.remove(name)
            state = False
        else:
            sel.append(name)
            state = True
        self._data["selected_accounts"] = sel
        self.save()
        return state

    def set_selected(self, names: list[str]) -> None:
        self._data["selected_accounts"] = list(dict.fromkeys(names))
        self.save()

    def clear_selected(self) -> None:
        self._data["selected_accounts"] = []
        self.save()

    def prune_selected(self, existing: list[str]) -> list[str]:
        """Drop ticks for accounts that no longer exist, then return the rest."""
        keep = [a for a in self.selected_accounts if a in existing]
        if keep != self.selected_accounts:
            self._data["selected_accounts"] = keep
            self.save()
        return keep

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
