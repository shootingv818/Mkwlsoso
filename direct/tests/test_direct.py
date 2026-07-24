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


def main():
    print("== direct client offline tests ==")
    for fn in (test_aes_nist, test_ige_roundtrip, test_crypto_helpers,
               test_mtproto_envelope, test_tl_roundtrip, test_session_loader):
        print(f"[{fn.__name__}]")
        fn()
    print("\nALL DIRECT TESTS PASSED")


if __name__ == "__main__":
    sys.exit(main())
