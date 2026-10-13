"""Minimal TL method/constructor encoders for the direct client's first calls.

Only what the first live probe needs. Constructor ids marked (core/stable) are
layer-independent; ids marked (layer) are layer-sensitive and are the most
likely thing to need a tweak once we see a real request (the transport capture
gives us the exact bytes if any id is off).
"""

from __future__ import annotations

from . import tl

# --- ids -----------------------------------------------------------------
INVOKE_WITH_LAYER = 0xDA9B0D0D    # invokeWithLayer#da9b0d0d layer:int query:!X (stable)
INIT_CONNECTION = 0xC1CD5EA9      # initConnection (layer ~130s; may need tweak)
HELP_GET_CONFIG = 0xC4F9186B      # help.getConfig#c4f9186b = Config (stable)
GET_STATE = 0xEDD4882A            # updates.getState#edd4882a = updates.State (stable-ish)
INPUT_PEER_SELF = 0x7DA07EC9      # inputPeerSelf#7da07ec9 (stable)
INPUT_USER_SELF = 0xF7C1B13F      # inputUserSelf#f7c1b13f (stable)
USERS_GET_USERS = 0x0D91A548      # users.getUsers#0d91a548 id:Vector<InputUser> (stable)
PING = 0x7ABE77EC                 # ping#7abe77ec ping_id:long (core)


def help_get_config() -> bytes:
    return tl.int_bytes(HELP_GET_CONFIG, signed=False)


def updates_get_state() -> bytes:
    return tl.int_bytes(GET_STATE, signed=False)


def ping(ping_id: int) -> bytes:
    return tl.int_bytes(PING, signed=False) + tl.long_bytes(ping_id, signed=False)


def input_user_self() -> bytes:
    return tl.int_bytes(INPUT_USER_SELF, signed=False)


def users_get_users_self() -> bytes:
    """users.getUsers([inputUserSelf])."""
    return (tl.int_bytes(USERS_GET_USERS, signed=False)
            + tl.vector_bytes([input_user_self()], lambda x: x))


def init_connection(api_id: int, query: bytes,
                    device_model: str = "MkwlDirect",
                    system_version: str = "1.0",
                    app_version: str = "1.0",
                    system_lang_code: str = "en",
                    lang_pack: str = "",
                    lang_code: str = "en") -> bytes:
    """initConnection wrapper. flags=0 (no proxy/params).

    Layout (layer ~131-143): flags:# api_id:int device_model:string
    system_version:string app_version:string system_lang_code:string
    lang_pack:string lang_code:string query:!X
    """
    return (
        tl.int_bytes(INIT_CONNECTION, signed=False)
        + tl.int_bytes(0, signed=False)                 # flags
        + tl.int_bytes(api_id)
        + tl.string_bytes(device_model)
        + tl.string_bytes(system_version)
        + tl.string_bytes(app_version)
        + tl.string_bytes(system_lang_code)
        + tl.string_bytes(lang_pack)
        + tl.string_bytes(lang_code)
        + query
    )


def invoke_with_layer(layer: int, query: bytes) -> bytes:
    return (tl.int_bytes(INVOKE_WITH_LAYER, signed=False)
            + tl.int_bytes(layer) + query)


def wrap_initial(api_id: int, layer: int, query: bytes) -> bytes:
    """The standard first-call wrapper: invokeWithLayer(initConnection(query))."""
    return invoke_with_layer(layer, init_connection(api_id, query))
