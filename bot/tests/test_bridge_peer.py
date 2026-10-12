"""Tests for bridge peer resolution (the access_hash the page cannot look up).

Run: python -m bot.tests.test_bridge_peer

Offline: fake driver, no browser, no network.

Background. The bridge resolved every recipient through
`window.appPeersManager`. The current Eitaa build does not expose it, so a send
returned:

    {"ok": false, "method": "none", "code": "no bridge method available",
     "invoke_err": null}

- `method: none` and `invoke_err: null` mean nothing was even attempted: the
peer came back null, so the invokeApi path was skipped, and
`appMessagesManager` (also gone) could not run either. The server was never
contacted.

Nothing new was needed to fix it, because the project already had both halves:

  * `eitaa/contacts_list.js` reads every contact WITH its access_hash through
    `contacts.getContacts` - measured live, 124 of 124 contacts.
  * `direct/peers.py` already turns a user_id plus that hash into a peer: the
    20-byte inputPeerUser, ctor 0xdde8a54c + user_id:long + access_hash:long.
  * `bot/transports.py` already builds the peer_id -> access_hash map with
    `access_hash_map(contacts_store.contacts(account))`, and DirectTransport
    already takes it.

So BridgeTransport takes the same map, from the same call, and the page builds
the same inputPeerUser one layer up as a TL object. These tests hold that wiring
in place - including that the two layers agree on the field names, which is the
thing a refactor would silently break.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="mkwl_bridgepeer_test_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["PROFILES_DIR"] = os.path.join(_TMP, "profiles")
os.environ["ARTIFACTS_DIR"] = os.path.join(_TMP, "artifacts")

from bot import transports  # noqa: E402

FAILURES: list[str] = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)
    return cond


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeDriver:
    """Records exactly what the transport hands to the page."""

    def __init__(self):
        self.sent: list[tuple] = []
        self.files: list[tuple] = []

    async def bridge_send(self, peer_id, text, access_hash=None):
        self.sent.append((peer_id, text, access_hash))
        return {"ok": True, "method": "invokeApi", "msg_id": 1}

    async def bridge_file_send(self, peer_id, caption="", access_hash=None):
        self.files.append((peer_id, caption, access_hash))
        return {"ok": True, "method": "sendMedia"}

    async def bridge_file_init(self, path, caption=""):
        return {"ok": True}

    async def bridge_file_ready(self):
        return True


# ---------------------------------------------------------------------------
# the map, and the wiring
# ---------------------------------------------------------------------------

def test_access_hash_map_is_the_existing_helper():
    """The bridge must use the SAME map builder as the direct engine, not its
    own parsing of the contacts list."""
    items = [
        {"peer_id": "111", "access_hash": "999", "title": "a"},
        {"peer_id": "222", "access_hash": "888", "title": "b"},
        {"peer_id": "333", "title": "no hash"},
        {"title": "no peer id", "access_hash": "777"},
    ]
    m = transports.access_hash_map(items)
    check("the helper maps peer_id -> access_hash",
          m.get("111") == "999" and m.get("222") == "888", str(m))
    check("entries without a hash are left out", "333" not in m, str(m))


def test_bridge_transport_passes_the_hash():
    d = FakeDriver()
    t = transports.BridgeTransport(d, {"111": "999"})
    res = run(t.send_text("111", "hello"))
    check("a text send carries the access_hash to the page",
          d.sent == [("111", "hello", "999")], str(d.sent))
    check("and the result is passed back", res.get("ok") is True, str(res))

    run(t.send_file("111", "caption"))
    check("a file send carries it too",
          d.files == [("111", "caption", "999")], str(d.files))


def test_unknown_peer_gets_none_not_a_crash():
    """An externally supplied recipient has no peer_id in the contacts cache.
    The page then falls back to its own resolution, so None must be passed
    rather than raising or inventing a hash."""
    d = FakeDriver()
    t = transports.BridgeTransport(d, {"111": "999"})
    run(t.send_text("444", "hi"))
    check("an unknown peer is sent with access_hash=None",
          d.sent == [("444", "hi", None)], str(d.sent))
    run(t.send_text("", "hi"))
    check("an empty peer_id does not raise", d.sent[-1][2] is None, str(d.sent[-1]))


def test_bridge_transport_without_a_map_still_works():
    """Older callers construct BridgeTransport(driver) with no map; that must
    keep working, since the page can still resolve on an older Eitaa build."""
    d = FakeDriver()
    t = transports.BridgeTransport(d)
    run(t.send_text("111", "hello"))
    check("the map is optional", d.sent == [("111", "hello", None)], str(d.sent))


# ---------------------------------------------------------------------------
# the two layers must describe the SAME peer
# ---------------------------------------------------------------------------

def test_the_two_layers_agree_on_the_peer():
    """direct/peers.py encodes inputPeerUser as bytes; the page builds the same
    thing as a TL object. If the field names ever diverge, sends break in a way
    only a live run would show - so assert they match here."""
    src = open(os.path.join(os.path.dirname(transports.__file__), "..",
                            "eitaa", "bridge_send.js"), encoding="utf-8").read()
    check("the page builds an inputPeerUser", '_: "inputPeerUser"' in src)
    check("with user_id and access_hash, the same two fields peers.py encodes",
          "user_id: pid" in src and "access_hash: ah" in src,
          "ctor 0xdde8a54c + user_id:long + access_hash:long")

    from direct import peers as dpeers
    blob = dpeers.peer_bytes(68526377, 12345678901234567)
    check("peers.py still produces the 20-byte peer", len(blob) == 20,
          f"{len(blob)} bytes")
    # ctor is little-endian 0xdde8a54c
    check("and it still starts with the inputPeerUser ctor",
          blob[:4] == (0xdde8a54c).to_bytes(4, "little"), blob[:4].hex())


def test_page_prefers_the_given_hash_then_falls_back():
    """Order matters: the handed-in hash works on every build, the old global
    only on the previous one, and the worker-backed manager needs awaiting."""
    src = open(os.path.join(os.path.dirname(transports.__file__), "..",
                            "eitaa", "bridge_send.js"), encoding="utf-8").read()
    body = src[src.index("async function getInputPeer"):]
    body = body[:body.index("var pidNum")] if "var pidNum" in body else body
    i_hash = body.find("if (ah)")
    i_global = body.find("window.appPeersManager")
    i_proxy = body.find("__MKWL_mgr")
    check("the given access_hash is tried first",
          0 <= i_hash < i_global, f"hash={i_hash} global={i_global}")
    check("the old global comes before the worker-backed manager",
          i_global < i_proxy, f"global={i_global} proxy={i_proxy}")
    check("the worker-backed manager call is awaited",
          "await M.getInputPeerById" in body,
          "it returns a promise through the proxy, unlike the old global")


def test_failure_reason_names_the_missing_hash():
    """The old failure said only 'no bridge method available', which hid the
    cause for a long time."""
    src = open(os.path.join(os.path.dirname(transports.__file__), "..",
                            "eitaa", "bridge_send.js"), encoding="utf-8").read()
    check("an unresolvable peer explains WHY",
          "no access_hash for this peer and no appPeersManager" in src)


def test_file_bridge_got_the_same_treatment():
    src = open(os.path.join(os.path.dirname(transports.__file__), "..",
                            "eitaa", "bridge_file_send.js"), encoding="utf-8").read()
    check("the file bridge takes an access_hash",
          "async function (peerId, caption, accessHash)" in src)
    check("it builds the same inputPeerUser", '_: "inputPeerUser"' in src)
    check("its self-id lookup awaits the new async sources",
          "await window.rootScope.getMyId()" in src
          and "await M.getSelf()" in src,
          "getSelf() is synchronous only on the old build")
    check("a zero self id is not accepted as valid",
          "+x !== 0" in src,
          "getMyId() answers 0 while the app is still booting")


def test_file_init_resolves_the_manager_through_the_proxy():
    """The live 'no sendFile' failure: __MKWL_fileInit read the old global
    window.appMessagesManager, which this Eitaa build dropped, so file upload
    (and the 'test to me' dry-run) died before uploading anything."""
    src = open(os.path.join(os.path.dirname(transports.__file__), "..",
                            "eitaa", "bridge_file_send.js"), encoding="utf-8").read()
    i_init = src.index("__MKWL_fileInit = async")
    body = src[i_init:i_init + 900]
    check("fileInit falls back to the worker proxy for appMessagesManager",
          "__MKWL_mgr" in body and "appMessagesManager" in body,
          "must resolve the manager, not read the dropped global")
    check("fileInit still guards on a usable sendFile",
          "typeof AMM.sendFile" in body or "AMM.sendFile" in body)
    # the actual sendFile call tries the object form the matrix proved works
    i_call = src.index("MKWLUP")
    call = src[i_call:i_call + 400]
    check("the upload tries the object-form sendFile first",
          "peerId:" in call and "caption:" in call,
          "object form delivered on this build; positional is the fallback")
    check("the positional form is kept as a fallback",
          "sendFile(selfNum" in call)


def main() -> int:
    print("=" * 68)
    print("BRIDGE PEER RESOLUTION TESTS")
    print("=" * 68)
    try:
        print("\n-- the map and the wiring --")
        test_access_hash_map_is_the_existing_helper()
        test_bridge_transport_passes_the_hash()
        test_unknown_peer_gets_none_not_a_crash()
        test_bridge_transport_without_a_map_still_works()
        print("\n-- the two layers agree --")
        test_the_two_layers_agree_on_the_peer()
        test_page_prefers_the_given_hash_then_falls_back()
        test_failure_reason_names_the_missing_hash()
        test_file_bridge_got_the_same_treatment()
        test_file_init_resolves_the_manager_through_the_proxy()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + "=" * 68)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("ALL BRIDGE PEER TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
