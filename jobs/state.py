"""Restart-safe job state.

A campaign is persisted as a single JSON file under artifacts/jobs/<job_id>.json.
After every recipient we rewrite this file, so if the process is killed or the
server reboots, `campaign --resume <job_id>` continues exactly where it left off
without re-sending to anyone already marked done.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from config import config


# Per-recipient status values.
PENDING = "pending"
SENT = "sent"
FAILED = "failed"
SKIPPED = "skipped"

# Job status values.
RUNNING = "running"
PAUSED = "paused"
STOPPED = "stopped"
DONE = "done"


@dataclass
class Recipient:
    name: str
    status: str = PENDING
    detail: str = ""
    attempts: int = 0
    updated: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Recipient":
        return cls(
            name=d["name"],
            status=d.get("status", PENDING),
            detail=d.get("detail", ""),
            attempts=int(d.get("attempts", 0)),
            updated=float(d.get("updated", 0.0)),
        )


@dataclass
class JobState:
    job_id: str
    account: str
    text: str
    status: str = RUNNING
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    recipients: list[Recipient] = field(default_factory=list)

    # ---- persistence ----
    @property
    def path(self) -> Path:
        return config.JOBS_DIR / f"{self.job_id}.json"

    @property
    def stop_flag(self) -> Path:
        return config.JOBS_DIR / f"{self.job_id}.stop"

    def save(self) -> None:
        config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
        self.updated = time.time()
        data = {
            "job_id": self.job_id,
            "account": self.account,
            "text": self.text,
            "status": self.status,
            "created": self.created,
            "updated": self.updated,
            "recipients": [r.to_dict() for r in self.recipients],
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @classmethod
    def load(cls, job_id: str) -> "JobState":
        path = config.JOBS_DIR / f"{job_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        state = cls(
            job_id=data["job_id"],
            account=data["account"],
            text=data["text"],
            status=data.get("status", RUNNING),
            created=float(data.get("created", time.time())),
            updated=float(data.get("updated", time.time())),
            recipients=[Recipient.from_dict(r) for r in data.get("recipients", [])],
        )
        return state

    # ---- helpers ----
    @classmethod
    def create(cls, account: str, text: str, names: list[str]) -> "JobState":
        # Dedup while preserving order.
        seen: set[str] = set()
        unique: list[str] = []
        for n in names:
            n = n.strip()
            if not n or n in seen:
                continue
            seen.add(n)
            unique.append(n)
        job_id = time.strftime("%Y%m%d-%H%M%S") + f"_{account}"
        return cls(
            job_id=job_id,
            account=account,
            text=text,
            recipients=[Recipient(name=n) for n in unique],
        )

    def counts(self) -> dict[str, int]:
        c = {PENDING: 0, SENT: 0, FAILED: 0, SKIPPED: 0}
        for r in self.recipients:
            c[r.status] = c.get(r.status, 0) + 1
        c["total"] = len(self.recipients)
        return c

    def next_pending(self):
        for r in self.recipients:
            if r.status == PENDING:
                yield r
