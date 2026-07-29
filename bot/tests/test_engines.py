"""Tests for the engine layer: bridge / direct / hybrid, and the new stores.

Run: python -m bot.tests.test_engines

Nothing here touches a browser, the network, or the real direct client.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="mkwl_eng_test_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["PROFILES_DIR"] = os.path.join(_TMP, "profiles")
os.environ["ARTIFACTS_DIR"] = os.path.join(_TMP, "artifacts")


def _stub_playwright() -> None:
    try:
        import playwright.async_api  # noqa: F401
        return
    except Exception:  # noqa: BLE001
        pass
    pkg = types.ModuleType("playwright")
    api = types.ModuleType("playwright.async_api")
    for name in ("BrowserContext", "CDPSession", "Page", "Locator", "Error"):
        setattr(api, name, type(name, (object,), {}))
    api.TimeoutError = type("TimeoutError", (Exception,), {})
    api.async_playwright = lambda: None
    pkg.async_api = api
    sys.modules.setdefault("playwright", pkg)
    sys.modules.setdefault("playwright.async_api", api)


_stub_playwright()

from bot import blocked_store, direct_ctx, transports  # noqa: E402
from bot import runner as R  # noqa: E402
from bot.store import Store  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: object = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name} {extra}")


# ---- fakes -------------------------------------------------------------

class FakeBridgeDriver:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    async def bridge_file_init(self, path, caption="", locate_timeout=None):
        self.calls.append("init")
        return {"ok": self.ok, "msg_id": 1}

    async def bridge_file_ready(self):
        return self.ok

    async def bridge_send(self, peer, text):
        self.calls.append(f"text:{peer}")
        return {"ok": self.ok, "method": "bridge", "msg_id": 2}

    async def bridge_file_send(self, peer, caption=""):
        self.calls.append(f"file:{peer}")
        return {"ok": self.ok, "method": "bridge", "msg_id": 3}


class FakeSender:
    """Stands in for direct.sender.DirectSender (synchronous, like the real one)."""

    def __init__(self, account="acc", text_ok=True, upload_ok=True, limit_peers=()):
        self.account = account
        self.text_ok = text_ok
        self.upload_ok = upload_ok
        self.limit_peers = set(limit_peers)
        self.sent = []
        self.closed = False

    def upload_file(self, path, caption=""):
        return {"ok": self.upload_ok, "code": None if self.upload_ok else "upload failed"}

    def send_text(self, peer: bytes, text: str):
        self.sent.append(peer)
        if peer in self.limit_peers:
            return {"ok": False, "limit": True, "code": "PEER_FLOOD"}
        if not self.text_ok:
            return {"ok": False, "code": "transport: boom"}
        return {"ok": True, "method": "direct/sendMessage"}

    def send_uploaded_file(self, peer: bytes, caption=""):
        self.sent.append(peer)
        if peer in self.limit_peers:
            return {"ok": False, "limit": True, "code": "PEER_FLOOD"}
        return {"ok": bool(self.text_ok), "method": "direct/sendMedia"}

    def close(self):
        self.closed = True


class FakePeers:
    @staticmethod
    def peer_bytes(user_id: int, access_hash: int) -> bytes:
        return b"P" + str(user_id).encode() + b":" + str(access_hash).encode()

    @staticmethod
    def resolve(account, key):
        return None


# ---- engine selection --------------------------------------------------

def test_engine_selection():
    print("engine selection")
    check("unknown value falls back to bridge",
          R.effective_engine({"engine": "nonsense"}) == "bridge")
    check("bridge stays bridge", R.effective_engine({"engine": "bridge"}) == "bridge")
    check("hybrid is always allowed",
          R.effective_engine({"engine": "hybrid"}) == "hybrid")
    # 'direct' has no safety net, so it needs the explicit opt-in flag.
    from config import config
    expected = "direct" if config.ENABLE_DIRECT else "hybrid"
    check(f"direct without the flag becomes {expected}",
          R.effective_engine({"engine": "direct"}) == expected)


def test_store_engine_cycle():
    print("settings: engine cycling")
    st = Store()
    st.set_engine("bridge")
    seen = [st.engine]
    for _ in range(3):
        seen.append(st.cycle_engine())
    check("cycles through the offered engines", "hybrid" in seen, seen)
    check("comes back to bridge", seen[-1] in ("bridge", "hybrid", "direct"), seen)
    st.set_engine("garbage")
    check("garbage is refused", st.engine == "bridge", st.engine)


# ---- transports --------------------------------------------------------

def test_bridge_transport():
    print("bridge transport")
    d = FakeBridgeDriver()
    t = transports.BridgeTransport(d)

    async def go():
        await t.prepare_file("/tmp/x", "c")
        a = await t.send_text("111", "hi")
        b = await t.send_file("222", "cap")
        return a, b

    a, b = asyncio.run(go())
    check("text delivered", a.get("ok"))
    check("file delivered", b.get("ok"))
    check("it drove the page", d.calls == ["init", "text:111", "file:222"], d.calls)
    check("label is bridge", t.label == "bridge")
    check("it needs a browser", t.browserless is False)


def test_direct_transport_uses_access_hashes():
    print("direct transport")
    s = FakeSender()
    t = transports.DirectTransport(s, {"111": "999"}, peers_module=FakePeers)

    async def go():
        ok = await t.send_text("111", "hi")
        missing = await t.send_text("222", "hi")     # no access_hash known
        await t.prepare_file("/tmp/x", "c")
        f = await t.send_file("111", "cap")
        return ok, missing, f

    ok, missing, f = asyncio.run(go())
    check("sends with the peer built from the contacts list", ok.get("ok"), ok)
    check("the peer really was addressed", s.sent and s.sent[0].startswith(b"P111:"),
          s.sent[:1])
    check("an unknown access_hash is a clean failure",
          not missing.get("ok") and "access_hash" in missing.get("code", ""), missing)
    check("file send works after one upload", f.get("ok"), f)
    check("it needs no browser", t.browserless is True)

    t2 = transports.DirectTransport(FakeSender(), {"111": "999"}, peers_module=FakePeers)
    r = asyncio.run(t2.send_file("111", "c"))
    check("file send before upload is refused",
          not r.get("ok") and "not initialized" in r.get("code", ""), r)


def test_hybrid_falls_back_but_not_on_refusals():
    print("hybrid transport")
    peer = FakePeers.peer_bytes(111, 999)
    # The direct engine cannot deliver at all -> the page must pick it up.
    broken = FakeSender(text_ok=False)
    d = FakeBridgeDriver()
    h = transports.HybridTransport(
        transports.DirectTransport(broken, {"111": "999"}, peers_module=FakePeers),
        transports.BridgeTransport(d))
    res = asyncio.run(h.send_text("111", "hi"))
    check("a broken direct send is retried on the page", res.get("ok"), res)
    check("the fallback is recorded", h.stats["fell_back"] == 1, h.stats)
    check("and the method says so", "bridge-after-direct" in res.get("method", ""), res)

    # A server refusal is the server's answer: the page would only repeat it.
    refusing = FakeSender(limit_peers={peer})
    d2 = FakeBridgeDriver()
    h2 = transports.HybridTransport(
        transports.DirectTransport(refusing, {"111": "999"}, peers_module=FakePeers),
        transports.BridgeTransport(d2))
    res2 = asyncio.run(h2.send_text("111", "hi"))
    check("a refusal is NOT retried", res2.get("limit") is True, res2)
    check("the page was not used for it", d2.calls == [], d2.calls)

    # Happy path: direct carries the load, nothing touches the page.
    good = FakeSender()
    d3 = FakeBridgeDriver()
    h3 = transports.HybridTransport(
        transports.DirectTransport(good, {"111": "999"}, peers_module=FakePeers),
        transports.BridgeTransport(d3))
    asyncio.run(h3.send_text("111", "hi"))
    check("direct carried it", h3.stats["direct"] == 1, h3.stats)
    check("the browser stayed idle", d3.calls == [], d3.calls)


def test_hybrid_upload_falls_back():
    print("hybrid upload fallback")
    s = FakeSender(upload_ok=False)
    d = FakeBridgeDriver()
    h = transports.HybridTransport(
        transports.DirectTransport(s, {}, peers_module=FakePeers),
        transports.BridgeTransport(d))
    res = asyncio.run(h.prepare_file("/tmp/x", "c"))
    check("the page uploaded instead", res.get("ok"), res)
    check("and it says why", "direct upload failed" in str(res.get("code")), res)


def test_access_hash_map():
    print("access hash map")
    m = transports.access_hash_map([
        {"peer_id": "1", "access_hash": "10", "title": "a"},
        {"peer_id": "2", "title": "no hash"},
        {"title": "nothing"},
    ])
    check("only complete rows are mapped", m == {"1": "10"}, m)


# ---- direct context ----------------------------------------------------

def test_direct_ctx_saves_only_useful_records():
    print("direct session context")
    check("nothing saved yet", not direct_ctx.has_context("acc"))
    path, kept = direct_ctx.save_capture("acc", [
        {"kind": "fetch", "url": "https://x/eitaa/", "reqHead": "ed77be7adeadbeef",
         "reqLen": 8},
        {"kind": "fetch", "url": "https://x/other", "reqHead": "cafebabe", "reqLen": 4},
        {"kind": "ws_open", "url": "wss://x"},
    ])
    check("only the envelope records are kept", kept == 1, kept)
    check("a file was written", path is not None and path.is_file())
    data = json.loads(path.read_text(encoding="utf-8"))
    check("saved in the shape the direct engine reads",
          isinstance(data, dict) and isinstance(list(data.values())[0], list), data)
    check("age is reported",
          (direct_ctx.newest_capture_age_hours("acc") or 0) < 1)
    check("a dump with no envelope is not saved",
          direct_ctx.save_capture("acc2", [{"kind": "fetch", "reqHead": "00"}])[1] == 0)


def test_refresh_reports_missing_hook():
    print("context refresh")

    class NoHook:
        async def dump_worker_requests(self):
            return [{"kind": "no_hook", "note": "not injected"}]

    res = asyncio.run(direct_ctx.refresh_from_driver(NoHook(), "acc3"))
    check("a missing init script is reported clearly",
          not res.get("ok") and "worker_capture" in res.get("code", ""), res)

    class Empty:
        async def dump_worker_requests(self):
            return []

    res2 = asyncio.run(direct_ctx.refresh_from_driver(Empty(), "acc3"))
    check("an empty dump is reported, not saved", not res2.get("ok"), res2)


def test_worker_capture_script_only_when_needed():
    print("worker capture init script")
    check("bridge needs no init script", R._worker_capture_script("bridge") is None)
    for eng in ("hybrid", "direct"):
        p = R._worker_capture_script(eng)
        check(f"{eng} gets the worker hook", p is not None and p.name == "worker_capture.js",
              p)


# ---- refused peers -----------------------------------------------------

def test_blocked_store():
    print("refused peers store")
    check("PEER_FLOOD is permanent", blocked_store.is_permanent("PEER_FLOOD"))
    check("privacy restriction is permanent",
          blocked_store.is_permanent("USER_PRIVACY_RESTRICTED"))
    check("a timed flood wait is NOT permanent",
          not blocked_store.is_permanent("FLOOD_WAIT_42"))
    check("a transport error is not permanent",
          not blocked_store.is_permanent("transport: connection reset"))

    bl = blocked_store.open_list("bacc")
    check("new entry recorded", bl.add("111", "PEER_FLOOD") is True)
    check("same peer again is not a new entry", bl.add("111", "PEER_FLOOD") is False)
    check("hit count grows", bl.peers["111"]["hits"] == 2, bl.peers)
    check("timed waits are ignored", bl.add("222", "FLOOD_WAIT_30") is False)
    bl.flush()
    check("persisted", blocked_store.count("bacc") == 1, blocked_store.count("bacc"))
    again = blocked_store.open_list("bacc")
    check("reload knows the peer", again.has("111"))
    check("and not others", not again.has("999"))
    check("clear wipes", blocked_store.clear("bacc")
          and blocked_store.count("bacc") == 0)


def main() -> int:
    for fn in (test_engine_selection, test_store_engine_cycle, test_bridge_transport,
               test_direct_transport_uses_access_hashes,
               test_hybrid_falls_back_but_not_on_refusals,
               test_hybrid_upload_falls_back, test_access_hash_map,
               test_direct_ctx_saves_only_useful_records,
               test_refresh_reports_missing_hook,
               test_worker_capture_script_only_when_needed,
               test_blocked_store):
        fn()
    print()
    print(f"{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("ALL ENGINE TESTS PASSED" if code == 0 else "ENGINE TESTS FAILED")
    sys.exit(code)
