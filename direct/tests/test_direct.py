"""Offline unit tests for the isolated direct client foundation.

Run:  python -m direct.tests.test_direct
No network, no external deps -- verifies AES against a NIST vector, IGE
round-trip, the MTProto 2.0 envelope round-trip (with msg_key verification and
tamper detection), TL primitive round-trips, and the flexible session loader.
"""

from __future__ import annotations

import os
import sys

from direct import aes, crypto, tl, mtproto, session


def _check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        raise AssertionError(name)


def test_aes_nist():
    # NIST FIPS-197 / SP800-38A AES-256-ECB known-answer vector.
    key = bytes.fromhex("603deb1015ca71be2b73aef0857d7781"
                        "1f352c073b6108d72d9810a30914dff4")
    pt = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    ct = bytes.fromhex("f3eed1bdb5d2a03c064b5a7e3db181f8")
    _check(f"AES-256-ECB NIST vector (backend={aes.backend()})",
           aes.ecb_encrypt(key, pt) == ct and aes.ecb_decrypt(key, ct) == pt)


def test_ige_roundtrip():
    key = os.urandom(32)
    iv = os.urandom(32)
    data = os.urandom(16 * 20)
    enc = crypto.ige_encrypt(data, key, iv)
    _check("IGE round-trip", crypto.ige_decrypt(enc, key, iv) == data and enc != data)


def test_crypto_helpers():
    ak = os.urandom(256)
    _check("auth_key_id length", len(crypto.auth_key_id(ak)) == 8)
    pt = os.urandom(64)
    mk = crypto.msg_key(ak, pt, from_client=True)
    _check("msg_key length", len(mk) == 16)
    k, v = crypto.kdf(ak, mk, from_client=True)
    _check("kdf key/iv lengths", len(k) == 32 and len(v) == 32)
    _check("kdf deterministic", crypto.kdf(ak, mk, from_client=True) == (k, v))


def test_mtproto_envelope():
    ak = os.urandom(256)
    salt = os.urandom(8)
    sid = os.urandom(8)
    body = tl.int_bytes(0x12345678, signed=False) + tl.string_bytes("hello eitaa")
    msg_id = mtproto.gen_msg_id()
    # We encrypt as "client" then decrypt with from_client=True to exercise the
    # exact same key branch (round-trip correctness of envelope + msg_key check).
    payload = mtproto.encrypt_message(ak, salt, sid, msg_id, 0, body, from_client=True)
    _check("payload has key_id+msg_key+cipher", len(payload) >= 24 and (len(payload) - 24) % 16 == 0)
    dec = mtproto.decrypt_message(ak, payload, from_client=True)
    _check("envelope round-trip body", dec["body"] == body)
    _check("envelope salt/session preserved", dec["salt"] == salt and dec["session_id"] == sid)
    _check("envelope msg_id preserved", dec["msg_id"] == msg_id)

    # Tamper -> msg_key mismatch must raise.
    bad = bytearray(payload)
    bad[-1] ^= 0x01
    try:
        mtproto.decrypt_message(ak, bytes(bad), from_client=True)
        _check("tamper detected", False)
    except Exception:
        _check("tamper detected (raised)", True)


def test_tl_roundtrip():
    buf = (tl.int_bytes(-5) + tl.long_bytes(2**40) + tl.bool_bytes(True)
           + tl.string_bytes("سلام") + tl.bytes_bytes(b"\x00\x01\x02")
           + tl.double_bytes(3.5)
           + tl.vector_bytes([1, 2, 3], lambda x: tl.int_bytes(x)))
    r = tl.Reader(buf)
    _check("tl int", r.int() == -5)
    _check("tl long", r.long() == 2**40)
    _check("tl bool", r.bool() is True)
    _check("tl string utf8", r.string() == "سلام")
    _check("tl bytes", r.bytes() == b"\x00\x01\x02")
    _check("tl double", abs(r.double() - 3.5) < 1e-9)
    _check("tl vector", r.vector(lambda rr: rr.int()) == [1, 2, 3])
    _check("tl fully consumed", r.remaining() == 0)


def test_session_loader(tmp_path="/tmp/_mkwl_sess.json"):
    import json
    # The CONFIRMED Eitaa/tweb layout: session in localStorage, per-DC auth
    # keys as 512-hex strings, salts as 16-hex strings, user_auth as a dict.
    # (dummy key bytes here -- the real auth_key never leaves the server.)
    export = {
        "localStorage": {
            "dc": 2,
            "user_auth": {"dcID": 2, "date": 1784581460, "id": 777000},
            "dc1_auth_key": "cd" * 256,
            "dc1_server_salt": "b7304c2e7cf08c82",
            "dc2_auth_key": "ab" * 256,
            "dc2_server_salt": "b7304c2e7cf08c82",
            "dc4_auth_key": "ef" * 256,
            "eitaa_auth": "x", "token": "y", "imei": "z", "state_id": "123",
        },
        "indexeddb": {"tweb": {"stores": {}, "skipped": {"users": 1493}}},
        "hints": {},
    }
    from pathlib import Path
    Path(tmp_path).write_text(json.dumps(export), encoding="utf-8")
    sess, report = session.load_export(tmp_path)
    _check("loader source is localStorage", report.get("source") == "localStorage")
    _check("home dc = 2", sess.dc_id == 2)
    _check("home auth_key 256B", len(sess.auth_key) == 256)
    _check("home auth_key is dc2", sess.auth_key == bytes.fromhex("ab" * 256))
    _check("user_id from user_auth dict", sess.user_id == 777000)
    _check("home salt 8B", len(sess.server_salt) == 8)
    _check("multi-dc keys captured (1,2,4)", sorted(sess.auth_keys_by_dc) == [1, 2, 4])
    _check("session valid", sess.is_valid())
    rt = session.Session.from_json(sess.to_json())
    _check("json round-trip auth_key", rt.auth_key == sess.auth_key)
    # user_auth may also arrive as a JSON string -> must still parse.
    export["localStorage"]["user_auth"] = json.dumps({"dcID": 2, "id": 888})
    Path(tmp_path).write_text(json.dumps(export), encoding="utf-8")
    sess2, _ = session.load_export(tmp_path)
    _check("user_auth JSON-string parsed", sess2.user_id == 888)
    os.remove(tmp_path)


def test_service_parser():
    import gzip as _gz
    from direct import service, tl

    # rpc_error inside an rpc_result
    err = tl.int_bytes(service.RPC_ERROR, signed=False) + tl.int_bytes(420) + tl.string_bytes("FLOOD_WAIT_30")
    rr = tl.int_bytes(service.RPC_RESULT, signed=False) + tl.long_bytes(111, signed=False) + err
    ev = service.parse_body(rr)
    _check("rpc_result->rpc_error parsed", ev[0]["type"] == "rpc_error" and ev[0]["error_message"] == "FLOOD_WAIT_30")
    _check("rpc_error req_msg_id", ev[0]["req_msg_id"] == 111)

    # bad_server_salt
    bss = (tl.int_bytes(service.BAD_SERVER_SALT, signed=False) + tl.long_bytes(9, signed=False)
           + tl.int_bytes(1) + tl.int_bytes(48) + b"\x11\x22\x33\x44\x55\x66\x77\x88")
    ev = service.parse_body(bss)
    _check("bad_server_salt parsed", ev[0]["type"] == "bad_server_salt"
           and ev[0]["new_server_salt"] == b"\x11\x22\x33\x44\x55\x66\x77\x88")

    # rpc_result with a plain result payload
    res = tl.int_bytes(0xDEADBEEF, signed=False) + b"\x01\x02\x03\x04"
    rr2 = tl.int_bytes(service.RPC_RESULT, signed=False) + tl.long_bytes(222, signed=False) + res
    ev = service.parse_body(rr2)
    _check("rpc_result plain", ev[0]["type"] == "rpc_result" and ev[0]["result_cid"] == 0xDEADBEEF)

    # gzip_packed rpc_result
    gz = tl.int_bytes(service.GZIP_PACKED, signed=False) + tl.bytes_bytes(_gz.compress(res))
    rr3 = tl.int_bytes(service.RPC_RESULT, signed=False) + tl.long_bytes(333, signed=False) + gz
    ev = service.parse_body(rr3)
    _check("rpc_result gzip inner", ev[0]["type"] == "rpc_result" and ev[0]["result_cid"] == 0xDEADBEEF)

    # msg_container with a new_session + bad_salt
    ns = (tl.int_bytes(service.NEW_SESSION_CREATED, signed=False) + tl.long_bytes(1, signed=False)
          + b"\x00" * 8 + b"\xaa" * 8)
    def sub(mid, body):
        return tl.long_bytes(mid, signed=False) + tl.int_bytes(0) + tl.int_bytes(len(body), signed=False) + body
    cont = (tl.int_bytes(service.MSG_CONTAINER, signed=False) + tl.int_bytes(2, signed=False)
            + sub(1, ns) + sub(2, bss))
    ev = service.parse_body(cont)
    types = [e["type"] for e in ev]
    _check("container unwrapped 2 msgs", types == ["new_session_created", "bad_server_salt"])


def test_schema_wrap():
    from direct import schema, tl
    q = schema.help_get_config()
    _check("help.getConfig id", tl.Reader(q).int(signed=False) == schema.HELP_GET_CONFIG)
    wrapped = schema.wrap_initial(1025907, 135, q)
    r = tl.Reader(wrapped)
    _check("outer invokeWithLayer", r.int(signed=False) == schema.INVOKE_WITH_LAYER)
    _check("layer=135", r.int() == 135)
    _check("inner initConnection", r.int(signed=False) == schema.INIT_CONNECTION)
    r.int(signed=False)   # flags
    _check("api_id in initConnection", r.int() == 1025907)
    users = schema.users_get_users_self()
    _check("users.getUsers id", tl.Reader(users).int(signed=False) == schema.USERS_GET_USERS)


def test_transport_url():
    import os as _os
    from direct import dc
    _os.environ.pop("MKWL_DC_HOSTS", None)
    _check("default dc url https+/eitaa/", dc.dc_url(2).startswith("https://") and "/eitaa/" in dc.dc_url(2))
    _check("bagher.eitaa.ir is first candidate",
           dc.candidate_urls(2)[0] == "https://bagher.eitaa.ir/eitaa/")
    _os.environ["MKWL_DC_HOSTS"] = "2=https://majid.eitaa.com/eitaa/,4=https://vahid.eitaa.com/eitaa/"
    _check("env override dc2", dc.candidate_urls(2) == ["https://majid.eitaa.com/eitaa/"])
    _check("env override dc4", dc.candidate_urls(4) == ["https://vahid.eitaa.com/eitaa/"])
    _os.environ.pop("MKWL_DC_HOSTS", None)
    from direct.transport import HttpTransport
    t = HttpTransport("https://majid.eitaa.com/eitaa/")
    _check("transport parses host", t.host == "majid.eitaa.com" and t.port == 443 and t.path == "/eitaa/")


def test_eitaa_envelope():
    # Real bytes captured from Eitaa's MTProto worker (config + msgs_ack + a
    # query request). wrap_eitaa must reproduce them byte-for-byte, and
    # unwrap_eitaa must round-trip. This pins the CONFIRMED transport framing:
    #   ed77be7a | len1 token1 | len2 token2 | BElen | body | trailer(11).
    from direct.transport import wrap_eitaa, unwrap_eitaa, EITAA_MAGIC, EITAA_TRAILER
    real = [
        ("ed77be7a1f393137392e633735366132643130662e6534316334655f353032363731"
         "3933146d727470676d69327939666d3232325f5f776562000000046b18f9c400000087"
         "00000020000000"),
        ("ed77be7a1f393137392e633735366132643130662e6534316334655f353032363731"
         "3933146d727470676d69327939666d3232325f5f7765620000003400ef2e734ca5e8dd"
         "3904ff0200000000ec7a9c440000000015c4b51c05000000e4f0e95688f1dd9e87ddf0"
         "7e9eb45137a4177c7a0000008700000020000000"),
    ]
    for h in real:
        raw = bytes.fromhex(h)
        p = unwrap_eitaa(raw)
        _check("envelope magic", raw[:4] == EITAA_MAGIC)
        _check("envelope trailer constant", p["trailer"] == EITAA_TRAILER)
        _check("envelope body length ok", p["ok"])
        _check("token1 is route token", p["token1"].startswith("9179."))
        _check("token2 is __web session id", p["token2"].endswith("__web"))
        _check("wrap reproduces real bytes", wrap_eitaa(p["token1"], p["token2"], p["body"]) == raw)


def test_eitaa_methods():
    # Serializers must reproduce REAL captured method bodies byte-for-byte.
    from direct import eitaa_tl as E
    from direct.transport import wrap_eitaa

    peer = E.input_peer_self(50267193, 0x00000000449C7AEC)
    _check("self peer is 20 bytes", len(peer) == 20)

    real_sm = ("70380c52800000004ca5e8dd3904ff0200000000ec7a9c4400000000104d4b574c"
               "545831373834393338383133000000bc0e582f47989200")
    built = E.send_message(peer, "MKWLTX1784938813", random_id=0x009298472F580EBC)
    _check("sendMessage reproduces capture", built.hex() == real_sm)

    real_ic = ("e50b802c15c4b51c01000000f4b792f30000000000000000102b393820393030203030"
               "302030303030000000084d6b776c5465737400000000000000")
    built = E.import_contacts([("+98 900 000 0000", "MkwlTest", "")])
    _check("importContacts reproduces capture", built.hex() == real_ic)

    real_sfp = ("21a604b342e01f3b6b1d120000000000306d6b776c736f736f20636170747572652d61"
                "6c6c2066696c652074657374204d4b574c5458313738343933383831330a000000")
    built = E.save_file_part(0x00121D6B3B1FE042, 0,
                             b"mkwlsoso capture-all file test MKWLTX1784938813\n")
    _check("upload.saveFilePart reproduces capture", built.hex() == real_sfp)

    # context extraction from a realistically-wrapped request
    raw = wrap_eitaa("9179.c756a2d10f.e41c4e_50267193", "mrtpgmi2y9fm222__web", real_sm and built)  # noqa
    raw = wrap_eitaa("9179.c756a2d10f.e41c4e_50267193", "mrtpgmi2y9fm222__web",
                     E.send_message(peer, "x", random_id=1))
    ctx = E.extract_context({"text": [{"kind": "fetch", "reqLen": len(raw), "reqHead": raw.hex()}]})
    _check("extract token1", ctx["token1"].startswith("9179."))
    _check("extract token2", ctx["token2"].endswith("__web"))
    _check("extract self peer", ctx["self_peer"] == peer)
    _check("extract user_id", ctx["user_id"] == 50267193)

    ok = E.classify_response(bytes.fromhex("19ca4421") + b"\x00" * 8)  # rpc_error on wire (LE)
    _check("rpc_error classified not ok", ok["ok"] is False)
    ok = E.classify_response(bytes.fromhex("01e015909abcdef0"))
    _check("normal ctor classified ok", ok["ok"] is True)
    # Eitaa's own error wrapper must be decoded (code + message), not passed as ok
    eerr = E.classify_response(bytes.fromhex("bbf9b9c490010000") + bytes([0x0d]) + b"RETRY_LIMIT10\x00\x00")
    _check("eitaa_error decoded not ok", eerr["ok"] is False and eerr["code"] == 400
           and eerr["message"] == "RETRY_LIMIT10")
    _check("build_file_send uses positive file_id",
           E.build_file_send(peer, b"x" * 5, "a.txt", 1)["file_id"] > 0)

    # --- file send: saveFilePart(+eitaa trailer) and sendMedia byte-exact ---
    data = b"mkwlsoso capture-all file test MKWLTX1784938813\n"
    real_sfp_full = ("21a604b342e01f3b6b1d120000000000306d6b776c736f736f20636170747572652d61"
                     "6c6c2066696c652074657374204d4b574c5458313738343933383831330a000000"
                     "03000000221751593904ff02000000003000000000000000")
    built = E.save_file_part_eitaa(0x00121D6B3B1FE042, 0, data, 50267193, len(data))
    _check("saveFilePart+eitaa trailer reproduces capture", built.hex() == real_sfp_full)

    real_media = ("a9eb9134880000004ca5e8dd3904ff0200000000ec7a9c4400000000c1c6385b10000000"
                  "7ff22ff542e01f3b6b1d1200010000000e646f63756d656e742e706c61696e0000000000"
                  "0a746578742f706c61696e0015c4b51c01000000680059151a6d6b776c5f636170616c6c"
                  "5f313738343933383832342e7478740014636170204d4b574c5458313738343933383831"
                  "33000000cb1bdda850082e0015c4b51c00000000")
    in_file = E.input_file(0x00121D6B3B1FE042, 1, "document.plain", "")
    media = E.input_media_uploaded_document(in_file, "text/plain", "mkwl_capall_1784938824.txt")
    sm = E.send_media(peer, media, message="cap MKWLTX1784938813", random_id=0x002E0850A8DD1BCB)
    _check("sendMedia reproduces capture", sm.hex() == real_media)

    plan = E.build_file_send(peer, data, "note.txt", 50267193, caption="hi")
    _check("build_file_send single part", plan["total_parts"] == 1 and len(plan["parts"]) == 1)

    # media host extraction: a sendMedia request in a capture reveals the media host
    from direct.transport import wrap_eitaa as _wrap
    mediabody = E.send_media(peer, E.input_media_uploaded_document(
        E.input_file(1, 1, "document.txt", ""), "text/plain", "a.txt"), random_id=1)
    mraw = _wrap("9179.x_1", "y__web", mediabody)
    cap = {"file": [
        {"kind": "fetch", "url": "https://bagher.eitaa.ir/eitaa/", "reqLen": 10, "reqHead": "ed77be7a00"},
        {"kind": "fetch", "url": "https://fateme.eitaa.com/eitaa/", "reqLen": len(mraw), "reqHead": mraw.hex()},
    ]}
    _check("extract_media_url finds media host",
           E.extract_media_url(cap) == "https://fateme.eitaa.com/eitaa/")
    _check("build_file_send mime txt", E.guess_mime("a.txt") == "text/plain")
    _check("build_file_send mime zip", E.guess_mime("a.zip") == "application/zip")
    _check("build_file_send mime apk",
           E.guess_mime("a.apk") == "application/vnd.android.package-archive")
    big = E.build_file_send(peer, b"x" * (E.UPLOAD_PART_SIZE + 10), "b.zip", 50267193)
    _check("build_file_send multi part", big["total_parts"] == 2 and len(big["parts"]) == 2)


def test_apk_mode():
    # Isolated, opt-in APK send-mode. OFF = byte-identical to before; ON = an
    # .apk goes on the wire as application/octet-stream (Eitaa blocks the real
    # apk MIME) while the real .apk name stays in documentAttributeFilename.
    from direct import eitaa_tl as E
    from direct import apk_mode as A

    peer = E.input_peer_self(50267193, 0x00000000449C7AEC)
    saved = os.environ.get(A.APK_OCTET_ENV)

    def _wire_mime_of(name):
        plan = E.build_file_send(peer, b"AK" * 50, name, 50267193)
        return plan["send_media"], plan

    try:
        # ---- is_apk edge cases (must never raise) ----
        _check("is_apk .apk", A.is_apk("app.apk") is True)
        _check("is_apk .APK uppercase", A.is_apk("App.APK") is True)
        _check("is_apk .zip false", A.is_apk("a.zip") is False)
        _check("is_apk no extension", A.is_apk("apk") is False)
        _check("is_apk empty", A.is_apk("") is False)
        _check("is_apk non-str safe", A.is_apk(12345) is False)

        # ---- OFF (default): nothing changes ----
        A.set_env(False)
        _check("off: enabled() False", A.enabled() is False)
        _check("off: effective_mime keeps apk mime",
               A.effective_mime("a.apk", "application/vnd.android.package-archive")
               == "application/vnd.android.package-archive")
        _check("off: guess_mime apk unchanged",
               E.guess_mime("a.apk") == "application/vnd.android.package-archive")
        media_off, _ = _wire_mime_of("game.apk")
        _check("off: wire carries the real apk mime",
               b"application/vnd.android.package-archive" in media_off)
        _check("off: apk filename present", "game.apk".encode("utf-8") in media_off)

        # ---- ON: apk is smuggled as octet-stream, name preserved ----
        A.set_env(True)
        _check("on: enabled() True", A.enabled() is True)
        _check("on: effective_mime apk -> octet-stream",
               A.effective_mime("a.apk", "application/vnd.android.package-archive")
               == "application/octet-stream")
        _check("on: non-apk untouched (zip)",
               A.effective_mime("a.zip", "application/zip") == "application/zip")
        _check("on: guess_mime itself still unchanged (isolation)",
               E.guess_mime("a.apk") == "application/vnd.android.package-archive")
        media_on, plan_on = _wire_mime_of("2_یادگاری_من.apk")
        _check("on: wire carries octet-stream", b"application/octet-stream" in media_on)
        _check("on: wire must NOT carry the blocked apk mime",
               b"vnd.android.package-archive" not in media_on)
        _check("on: real .apk filename still rides in the attribute",
               "2_یادگاری_من.apk".encode("utf-8") in media_on)

        # a big apk still splits into parts AND keeps the octet-stream trick
        big_media, big_plan = _wire_mime_of("big.apk")  # 100 bytes -> 1 part here
        _check("on: apk media has no blocked mime (multi-safe)",
               b"vnd.android.package-archive" not in big_media)

        # non-apk on the wire is unaffected while mode is ON
        z_media, _ = E.build_file_send(peer, b"x" * 5, "a.zip", 50267193)["send_media"], None
        _check("on: zip still zip mime on wire", b"application/zip" in z_media)

        # ---- truthy/falsey env parsing ----
        for v in ("1", "true", "YES", "On"):
            os.environ[A.APK_OCTET_ENV] = v
            _check(f"env '{v}' enables", A.enabled() is True)
        for v in ("0", "off", "no", ""):
            os.environ[A.APK_OCTET_ENV] = v
            _check(f"env '{v}' disables", A.enabled() is False)

        # ---- defensive: never raises, always returns the base mime ----
        _check("defensive: effective_mime returns base on odd input",
               A.effective_mime(None, "application/pdf") == "application/pdf")

        # ---- BRIDGE-path policy: mirror the exact 2 lines bridge_file_init runs
        #      (OS mime, then apk_mode). On a Debian/Ubuntu host the OS returns
        #      the apk mime, which Eitaa blocks; the toggle must rewrite it. ----
        import mimetypes as _mt2
        os_apk = _mt2.guess_type("app.apk")[0] or "application/octet-stream"
        A.set_env(True)
        bridge_on = A.effective_mime("app.apk", os_apk)
        _check("bridge policy ON: .apk -> octet regardless of OS mime",
               bridge_on == "application/octet-stream")
        A.set_env(False)
        bridge_off = A.effective_mime("app.apk", os_apk)
        _check("bridge policy OFF: unchanged (== OS mime)", bridge_off == os_apk)
        # a non-apk file is never rewritten, on or off
        A.set_env(True)
        _check("bridge policy: pdf untouched even when ON",
               A.effective_mime("a.pdf", "application/pdf") == "application/pdf")
    finally:
        if saved is None:
            os.environ.pop(A.APK_OCTET_ENV, None)
        else:
            os.environ[A.APK_OCTET_ENV] = saved


def test_transport_cookiejar():
    # The cookie jar pins the load-balanced backend node: it sends preloaded
    # cookies AND absorbs Set-Cookie from responses so upload+sendMedia stick.
    from direct.transport import HttpTransport

    class _Msg:
        def __init__(self, sc): self._sc = sc
        def get_all(self, name):
            return self._sc if name == "Set-Cookie" else None

    class _Resp:
        def __init__(self, sc): self.msg = _Msg(sc)

    t = HttpTransport("https://majid.eitaa.com/eitaa/", cookies={"a": "1"})
    _check("cookie header preloaded", t._cookie_header() == "a=1")
    t._absorb_set_cookie(_Resp(["SERVERID=nodeA; path=/; HttpOnly", "x=y; Secure"]))
    _check("absorbed SERVERID", t._cookies.get("SERVERID") == "nodeA")
    _check("absorbed x", t._cookies.get("x") == "y")
    _check("cookie header merged", "SERVERID=nodeA" in t._cookie_header()
           and "a=1" in t._cookie_header())
    # no Set-Cookie -> jar unchanged
    before = dict(t._cookies)
    t._absorb_set_cookie(_Resp([]))
    _check("no set-cookie keeps jar", t._cookies == before)


def main():
    print("== direct client offline tests ==")
    for fn in (test_aes_nist, test_ige_roundtrip, test_crypto_helpers,
               test_mtproto_envelope, test_tl_roundtrip, test_session_loader,
               test_service_parser, test_schema_wrap, test_transport_url,
               test_eitaa_envelope, test_eitaa_methods, test_apk_mode,
               test_transport_cookiejar):
        print(f"[{fn.__name__}]")
        fn()
    print("\nALL DIRECT TESTS PASSED")


if __name__ == "__main__":
    sys.exit(main())
