"""One interface, three engines: bridge, direct, and hybrid.

The send loop used to hard-code the browser bridge and carry a separate,
duplicated loop for the browser-free engine. That is why the direct engine drifted
out of the panel: every fix had to be made twice, so it was made once.

Here every engine exposes the SAME four calls, so the send loop is written once:

    await t.prepare_file(path, caption)   -> {ok, code}          (once per run)
    await t.send_text(peer_id, text)      -> {ok, limit, code, method}
    await t.send_file(peer_id, caption)   -> {ok, limit, code, method}
    t.label                               -> what to show in cards/logs

The reply shape is the bridge's existing one, so nothing downstream changes:
  ok=True                       delivered
  limit=True                    the server refused (PEER_FLOOD/FLOOD_WAIT...)
  code="..."                    why it failed

Engines
-------
bridge  Drives the real web app inside Chromium. Proven, but every send is a
        round trip through the page, and the browser costs 150-200s to start and
        ~500 MB of RAM on the target host.
direct  Plain HTTPS from Python, no browser at all. It needs two things the app
        knows: a session context (see bot/direct_ctx.py) and, per recipient, the
        peer's access_hash - which the API contacts list now provides.
hybrid  direct for the sending, bridge as the safety net: any recipient the
        direct engine cannot deliver to is retried through the page. This is the
        mode that gets the speed without giving up the proven path.
"""

from __future__ import annotations

import asyncio
from typing import Any


class Transport:
    """Base interface. `label` is what the cards and logs call this engine."""

    label = "bridge"
    #: True when this transport needs no browser page for its sends.
    browserless = False

    async def prepare_file(self, file_path: str, caption: str = "") -> dict:
        raise NotImplementedError

    async def send_text(self, peer_id: str, text: str) -> dict:
        raise NotImplementedError

    async def send_file(self, peer_id: str, caption: str = "") -> dict:
        raise NotImplementedError

    async def file_ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class BridgeTransport(Transport):
    """The proven path: Eitaa's own web app, driven in-page."""

    label = "bridge"

    def __init__(self, driver) -> None:
        self.driver = driver

    async def prepare_file(self, file_path: str, caption: str = "") -> dict:
        return await self.driver.bridge_file_init(file_path, caption)

    async def file_ready(self) -> bool:
        return await self.driver.bridge_file_ready()

    async def send_text(self, peer_id: str, text: str) -> dict:
        return await self.driver.bridge_send(peer_id, text)

    async def send_file(self, peer_id: str, caption: str = "") -> dict:
        return await self.driver.bridge_file_send(peer_id, caption)


class DirectTransport(Transport):
    """Browser-free HTTPS sends.

    `access_hashes` maps peer_id -> access_hash. The API contacts list provides
    it for every contact, which is what finally makes this engine usable for a
    campaign: before, a peer could only be addressed if it had been harvested
    into the separate peer store first.

    The direct client is synchronous, so every call runs in a worker thread; that
    also means several sends really can be in flight at once.
    """

    label = "direct"
    browserless = True

    def __init__(self, sender, access_hashes: dict[str, str] | None = None,
                 peers_module=None) -> None:
        self.sender = sender
        self.access_hashes = access_hashes or {}
        self._peers = peers_module
        self._file_ready = False

    # -- peer resolution --
    def _peer(self, peer_id: str) -> bytes | None:
        """The 20-byte peer for this recipient, from the contacts list or store."""
        if not peer_id:
            return None
        ah = self.access_hashes.get(str(peer_id))
        if ah:
            try:
                if self._peers is None:
                    from direct import peers as _peers
                    self._peers = _peers
                return self._peers.peer_bytes(int(peer_id), int(ah))
            except Exception:  # noqa: BLE001
                pass
        # Fall back to anything harvested earlier under an id alias.
        try:
            if self._peers is None:
                from direct import peers as _peers
                self._peers = _peers
            return self._peers.resolve(self.sender.account, f"id:{peer_id}")
        except Exception:  # noqa: BLE001
            return None

    async def prepare_file(self, file_path: str, caption: str = "") -> dict:
        res = await asyncio.to_thread(self.sender.upload_file, file_path, caption)
        self._file_ready = bool(res.get("ok"))
        return res

    async def file_ready(self) -> bool:
        return self._file_ready

    async def send_text(self, peer_id: str, text: str) -> dict:
        peer = self._peer(peer_id)
        if not peer:
            return {"ok": False, "code": "no access_hash for this peer"}
        return await asyncio.to_thread(self.sender.send_text, peer, text)

    async def send_file(self, peer_id: str, caption: str = "") -> dict:
        if not self._file_ready:
            return {"ok": False, "code": "file not initialized"}
        peer = self._peer(peer_id)
        if not peer:
            return {"ok": False, "code": "no access_hash for this peer"}
        return await asyncio.to_thread(self.sender.send_uploaded_file, peer, caption)

    async def close(self) -> None:
        try:
            await asyncio.to_thread(self.sender.close)
        except Exception:  # noqa: BLE001
            pass


class HybridTransport(Transport):
    """Send with the fast browser-free engine, fall back to the page per recipient.

    A server refusal (PEER_FLOOD) is NOT retried through the bridge: the server
    already gave its answer and the page would only repeat it more slowly. Only
    engine-side problems - no context, transport error, an unresolvable peer -
    fall through to the proven path.
    """

    label = "hybrid"

    def __init__(self, direct: DirectTransport, bridge: BridgeTransport) -> None:
        self.direct = direct
        self.bridge = bridge
        self.stats = {"direct": 0, "bridge": 0, "fell_back": 0}

    async def prepare_file(self, file_path: str, caption: str = "") -> dict:
        res = await self.direct.prepare_file(file_path, caption)
        if res.get("ok"):
            return res
        # The browser-free upload failed; the page has its own, proven upload.
        self.stats["fell_back"] += 1
        alt = await self.bridge.prepare_file(file_path, caption)
        if alt.get("ok"):
            alt = dict(alt)
            alt["code"] = f"direct upload failed ({res.get('code')}); used the bridge"
        return alt

    async def file_ready(self) -> bool:
        return await self.direct.file_ready() or await self.bridge.file_ready()

    @staticmethod
    def _worth_retrying(res: dict | None) -> bool:
        if res is None:
            return True
        if res.get("ok") or res.get("limit"):
            return False
        return True

    async def _both(self, which: str, peer_id: str, payload: str) -> dict:
        first = getattr(self.direct, which)
        second = getattr(self.bridge, which)
        try:
            res = await first(peer_id, payload)
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "code": f"{type(exc).__name__}: {exc}"}
        if res.get("ok"):
            self.stats["direct"] += 1
            return res
        if not self._worth_retrying(res):
            return res
        self.stats["fell_back"] += 1
        alt = await second(peer_id, payload)
        if alt.get("ok"):
            self.stats["bridge"] += 1
            alt = dict(alt)
            alt["method"] = f"bridge-after-direct({res.get('code')})"
        return alt

    async def send_text(self, peer_id: str, text: str) -> dict:
        return await self._both("send_text", peer_id, text)

    async def send_file(self, peer_id: str, caption: str = "") -> dict:
        return await self._both("send_file", peer_id, caption)

    async def close(self) -> None:
        await self.direct.close()


def access_hash_map(items: Any) -> dict[str, str]:
    """peer_id -> access_hash from the saved contacts records."""
    out: dict[str, str] = {}
    for c in items or []:
        pid, ah = c.get("peer_id"), c.get("access_hash")
        if pid and ah:
            out[str(pid)] = str(ah)
    return out
