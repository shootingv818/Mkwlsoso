"""Central configuration for the Eitaa web capture tool.

All values come from environment variables (optionally loaded from a local
.env file). Nothing here contains secrets; the .env file itself is gitignored.
"""

from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader so we don't add a dependency.

    Only parses simple KEY=VALUE lines. Existing environment variables win.
    """
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


class Config:
    EITAA_WEB_URL: str = os.environ.get("EITAA_WEB_URL", "https://web.eitaa.com")
    PROFILES_DIR: Path = Path(os.environ.get("PROFILES_DIR", "./profiles"))
    ARTIFACTS_DIR: Path = Path(os.environ.get("ARTIFACTS_DIR", "./artifacts"))
    HEADED: bool = _get_bool("HEADED", True)
    BASELINE_SECONDS: int = _get_int("BASELINE_SECONDS", 12)
    ACTION_TRAIL_SECONDS: int = _get_int("ACTION_TRAIL_SECONDS", 8)
    RAW_ENCRYPTION_KEY: str = os.environ.get("RAW_ENCRYPTION_KEY", "")

    # Broadcaster (tabchi) rate limiting
    SEND_MIN_DELAY: int = _get_int("SEND_MIN_DELAY", 8)
    SEND_MAX_DELAY: int = _get_int("SEND_MAX_DELAY", 18)
    SEND_BATCH_SIZE: int = _get_int("SEND_BATCH_SIZE", 20)
    SEND_BATCH_COOLDOWN: int = _get_int("SEND_BATCH_COOLDOWN", 90)
    MAX_CONSECUTIVE_FAILURES: int = _get_int("MAX_CONSECUTIVE_FAILURES", 5)

    JOBS_DIR: Path = Path(os.environ.get("ARTIFACTS_DIR", "./artifacts")) / "jobs"

    # ---- Telegram control bot ----
    # Telethon needs API_ID/API_HASH (from my.telegram.org) plus a BOT_TOKEN
    # (from @BotFather). OWNER_ID is the only Telegram user allowed to use the
    # panel. REPORT_TO is where log cards are posted (defaults to the owner).
    API_ID: int = _get_int("API_ID", 0)
    API_HASH: str = os.environ.get("API_HASH", "")
    BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
    OWNER_ID: int = _get_int("OWNER_ID", 0)
    REPORT_TO: int = _get_int("REPORT_TO", 0)  # 0 -> fall back to OWNER_ID
    BOT_VERSION: str = os.environ.get("BOT_VERSION", "1.0")
    # Optional: the noVNC URL shown in the "Add Account" login hint so the
    # owner knows where to complete the phone+code login (e.g.
    # http://<ip>:6080/vnc.html). Left blank -> a generic hint is shown.
    NOVNC_URL: str = os.environ.get("NOVNC_URL", "")

    # Runtime-tunable defaults (the Settings panel overrides these live and
    # persists them under DATA_DIR/settings.json).
    TEXT_SEND_DELAY: float = float(os.environ.get("TEXT_SEND_DELAY", "3"))
    CONTACT_CREATE_DELAY: float = float(os.environ.get("CONTACT_CREATE_DELAY", "0.2"))
    SEND_LOG_EVERY: int = _get_int("SEND_LOG_EVERY", 50)
    # Which engine drives actions: "bridge" (browser/tweb) or "direct"
    # (browser-free MTProto in direct/). Overridable live from the Settings panel.
    ENGINE: str = os.environ.get("ENGINE", "bridge")
    # Host used for the Settings "server ping" probe.
    PING_HOST: str = os.environ.get("PING_HOST", "majid.eitaa.com")

    DATA_DIR: Path = Path(os.environ.get("DATA_DIR", "./data"))

    @classmethod
    def report_to(cls) -> int:
        return cls.REPORT_TO or cls.OWNER_ID

    @classmethod
    def profile_dir(cls, account: str) -> Path:
        return cls.PROFILES_DIR / account

    @classmethod
    def ensure_dirs(cls) -> None:
        cls.PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        cls.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        cls.JOBS_DIR.mkdir(parents=True, exist_ok=True)
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)


config = Config()
