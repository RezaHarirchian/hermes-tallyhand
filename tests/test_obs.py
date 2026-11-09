"""obs-websocket client: URL rules and settings, the RFC 6455 handshake and framing, the Hello/Identify login,
requests and the OBS operations, all against FakeObs on 127.0.0.1."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import struct
import uuid
from pathlib import Path

import pytest

from fake_obs import BINARY, CLOSE, CONT, PING, TEXT, FakeObs, close_frame, expected_auth, frame, unmask
from harness import closed_port, until
from tallyhand import obs
from tallyhand.obs import ObsError, RequestFailed

ROOT = Path(obs.__file__).resolve().parent
PASSWORD = "c0rrect-h0rse"


@pytest.fixture
async def fake():
    server = await FakeObs().start()
    yield server
    await server.stop()


@pytest.fixture
def quick(monkeypatch):
    """Short timeouts for the tests that wait on a silent fake."""
    for name in ("CONNECT_TIMEOUT", "IDENTIFY_TIMEOUT", "REQUEST_TIMEOUT"):
        monkeypatch.setattr(obs, name, 0.3)


def config(fake: FakeObs, password: str = "") -> obs.ObsConfig:
    return obs.parse_url(fake.url, password=password)


async def call(fake: FakeObs, request_type: str, data: dict | None = None, password: str = "") -> dict:
    async with obs.session(config(fake, password)) as client:
        return await client.call(request_type, data)


async def opened(cfg: obs.ObsConfig) -> None:
    async with obs.session(cfg):
        pass


async def error_of(coro) -> ObsError:
    with pytest.raises(ObsError) as caught:
        await coro
    return caught.value


# URL, loopback rule and settings

@pytest.mark.parametrize("url, host, port, secure, target", [
    ("ws://127.0.0.1:4455", "127.0.0.1", 4455, False, "/"),
    ("  ws://localhost:4455/  ", "localhost", 4455, False, "/"),
    ("WS://LOCALHOST:4455", "localhost", 4455, False, "/"),
    ("ws://[::1]:4455", "::1", 4455, False, "/"),
    ("ws://127.0.0.2:1234/obs?x=1", "127.0.0.2", 1234, False, "/obs?x=1"),
    ("ws://[::ffff:127.0.0.1]:4455", "::ffff:127.0.0.1", 4455, False, "/"),
    ("ws://127.0.0.1", "127.0.0.1", 80, False, "/"),
    ("wss://127.0.0.1", "127.0.0.1", 443, True, "/"),
])
def test_parse_url(url, host, port, secure, target):
    parsed = obs.parse_url(url)
    assert (parsed.host, parsed.port, parsed.secure, parsed.target, parsed.allow_remote) == (
        host, port, secure, target, False)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:4455", "https://127.0.0.1", "127.0.0.1:4455", "ws://", "ws://:4455", "ws://127.0.0.1:99999",
    "ws://127.0.0.1:port", "ws://127.0.0.1:0", "ws://127.0.0.1:4455/#top", "ws://127.0.0.1:4455/a b",
    "ws://[::1:4455", "ws://user:pw-in-url@127.0.0.1:4455",
])
def test_parse_url_refuses(url):
    with pytest.raises(ObsError) as caught:
        obs.parse_url(url)
    assert caught.value.code == "config" and obs.URL_ENV in caught.value.message
    assert "pw-in-url" not in str(caught.value)


@pytest.mark.parametrize("host", ["192.168.1.20", "10.0.0.5", "8.8.8.8", "[2001:db8::1]", "example.com",
                                  "obs.local", "127.0.0.1.nip.io", "localhost.example.com", "[::ffff:10.0.0.1]"])
def test_remote_hosts_need_the_opt_in(host):
    with pytest.raises(ObsError) as caught:
        obs.parse_url(f"ws://{host}:4455")
    assert caught.value.code == "config" and obs.ALLOW_REMOTE_ENV in caught.value.message
    assert host.strip("[]") not in caught.value.message and host.strip("[]") in caught.value.detail
    assert obs.parse_url(f"ws://{host}:4455", allow_remote=True).allow_remote


def test_load_config_defaults(monkeypatch):
    loaded = obs.load_config()
    assert (loaded.host, loaded.port, loaded.secure, loaded.password, loaded.allow_remote) == (
        "127.0.0.1", 4455, False, "", False)


@pytest.mark.parametrize("value, allowed", [("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
                                            ("false", False), ("0", False), ("", False), ("maybe", False)])
def test_load_config_remote_opt_in(monkeypatch, value, allowed):
    monkeypatch.setenv(obs.URL_ENV, "ws://192.168.1.20:4455")
    monkeypatch.setenv(obs.ALLOW_REMOTE_ENV, value)
    if allowed:
        assert obs.load_config().host == "192.168.1.20"
    else:
        with pytest.raises(ObsError):
            obs.load_config()


def test_load_config_password_stays_out_of_repr(monkeypatch):
    monkeypatch.setenv(obs.URL_ENV, " ws://localhost:4460 ")
    monkeypatch.setenv(obs.PASSWORD_ENV, PASSWORD)
    loaded = obs.load_config()
    assert (loaded.host, loaded.port, loaded.password) == ("localhost", 4460, PASSWORD)
    assert PASSWORD not in repr(loaded) and PASSWORD not in str(loaded)


def test_settings_come_through_the_hermes_secret_route(monkeypatch):
    asked = []

    def scoped(name, default=None, **kwargs):
        asked.append(name)
        return {obs.URL_ENV: "ws://localhost:4461", obs.PASSWORD_ENV: "from-the-scope",
                obs.BRB_SCENE_ENV: " Pause "}.get(name, default)

    monkeypatch.setattr(obs, "get_scoped_secret", scoped)
    monkeypatch.setenv(obs.URL_ENV, "ws://127.0.0.1:1")
    loaded = obs.load_config()
    assert (loaded.port, loaded.password) == (4461, "from-the-scope")
    assert obs.setting(obs.BRB_SCENE_ENV) == "Pause"
    assert obs.setting(obs.CAPTION_SOURCE_ENV, obs.DEFAULT_CAPTION_SOURCE) == "tallyhand_caption"
    assert set(asked) == {obs.URL_ENV, obs.PASSWORD_ENV, obs.ALLOW_REMOTE_ENV, obs.BRB_SCENE_ENV,
                          obs.CAPTION_SOURCE_ENV}


@pytest.mark.parametrize("name", ["obs.py", "obs_tools.py", "obs_commands.py"])
def test_modules_never_read_the_environment_directly(name):
    source = (ROOT / name).read_text(encoding="utf-8")
    assert "os.environ" not in source and "getenv" not in source and "dotenv" not in source and '".env' not in source


@pytest.mark.parametrize("name", ["obs.py", "obs_tools.py", "obs_commands.py"])
def test_no_stream_or_recording_control(name):
    source = (ROOT / name).read_text(encoding="utf-8")
    for request in ("StartStream", "StopStream", "ToggleStream", "StartRecord", "StopRecord", "ToggleRecord",
                    "PauseRecord", "ResumeRecord", "StartReplayBuffer", "StopReplayBuffer", "ToggleReplayBuffer"):
        assert request not in source


# pure helpers

def test_accept_key_rfc6455_example():
    assert obs.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_auth_string_follows_the_protocol_recipe():
    salt, challenge = "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI=", "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY="
    made = obs.auth_string("supersecretpassword", salt, challenge)
    assert made == "1Ct943GAT+6YQUUX47Ia/ncufilbe6+oD6lY+5kaCu4="
    assert made == expected_auth("supersecretpassword", salt, challenge)
    assert obs.auth_string("supersecretpassword", salt, challenge + "x") != made


def test_mask_rfc6455_example_and_round_trip():
    key = bytes.fromhex("37fa213d")
    assert obs._mask(b"Hello", key) == bytes.fromhex("7f9f4d5158")
    for data in (b"", b"H", b"Hello", bytes(range(256)) * 300):
        assert obs._mask(data, key) == unmask(data, key)
        assert obs._mask(obs._mask(data, key), key) == data


@pytest.mark.parametrize("host, loopback", [
    ("127.0.0.1", True), ("127.8.9.10", True), ("::1", True), ("[::1]", True), ("localhost", True),
    ("LocalHost.", True), ("::ffff:127.0.0.1", True), ("::1%lo", True), ("0.0.0.0", False), ("10.0.0.1", False),
    ("::ffff:10.0.0.1", False), ("example.com", False), ("", False),
])
def test_is_loopback(host, loopback):
    assert obs.is_loopback(host) is loopback


def test_clean():
    assert obs.clean("a\nb\t c\x00d\u202ee\r\n") == "a b cde"
    assert obs.clean("a\u3164b\U000e0000") == "ab"  # chat clients' invisible fillers go too
    assert obs.clean("x" * 300, 50) == "x" * 47 + "..."
    assert obs.clean(None) == ""


@pytest.mark.parametrize("names, wanted, found", [
    (["Main", "BRB"], "BRB", "BRB"),
    (["Main", "BRB"], "brb", "BRB"),
    (["Main", "BRB", "brb"], "brb", "brb"),
    (["Main", "BRB", "brb"], "Brb", None),
    (["Camera", "Game Capture", "Camera"], "camera", "Camera"),
    (["Main"], "Mai", None),
])
def test_match(names, wanted, found):
    assert obs.match(wanted, names) == found


# handshake

async def test_handshake_and_login(fake):
    async with obs.session(config(fake)) as client:
        assert client.ws.subprotocol == "obswebsocket.json"
    upgrade = fake.upgrades[0]
    assert upgrade["request"] == "GET / HTTP/1.1"
    assert upgrade["host"] == f"127.0.0.1:{fake.port}"
    assert upgrade["upgrade"] == "websocket" and upgrade["connection"] == "Upgrade"
    assert upgrade["sec-websocket-version"] == "13" and upgrade["sec-websocket-protocol"] == "obswebsocket.json"
    assert len(base64.b64decode(upgrade["sec-websocket-key"], validate=True)) == 16
    assert fake.identifies == [{"rpcVersion": 1, "eventSubscriptions": 0}]
    assert fake.client_closes == [1000] and fake.unmasked == 0


async def test_each_connection_has_its_own_key(fake):
    for _ in range(3):
        await opened(config(fake))
    keys = [upgrade["sec-websocket-key"] for upgrade in fake.upgrades]
    assert len(set(keys)) == 3


async def test_url_path_goes_into_the_request_line(fake):
    await opened(obs.parse_url(f"{fake.url}/obs?v=5"))
    assert fake.upgrades[0]["request"] == "GET /obs?v=5 HTTP/1.1"


@pytest.mark.parametrize("mode, detail", [
    ("404", "HTTP 404"), ("bad_accept", "Sec-WebSocket-Accept"), ("no_upgrade", "not a WebSocket upgrade"),
    ("protocol", "subprotocol"), ("extension", "extension"), ("hangup", "hung up"),
])
async def test_bad_handshakes_are_refused(fake, mode, detail):
    fake.handshake = mode
    err = await error_of(opened(config(fake)))
    assert err.code == "handshake" and err.message == obs.NOT_OBS and detail in err.detail
    assert fake.identifies == []


async def test_a_plain_http_server_is_not_obs():
    async def answer(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"SSH-2.0-OpenSSH_9.6\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        err = await error_of(opened(obs.parse_url(f"ws://127.0.0.1:{port}")))
    finally:
        server.close()
        await server.wait_closed()
    assert err.code == "handshake" and "did not answer in HTTP" in err.detail


async def test_missing_subprotocol_header_means_json(fake):
    fake.handshake = "no_protocol"
    assert "activeFps" in await call(fake, "GetStats")


async def test_handshake_timeout(fake, quick):
    fake.handshake = "silent"
    err = await error_of(opened(config(fake)))
    assert err.code == "timeout" and "handshake" in err.message


async def test_obs_not_running():
    port = closed_port()
    err = await error_of(opened(obs.parse_url(f"ws://127.0.0.1:{port}")))
    assert err.code == "unreachable" and err.message == "OBS is not running or its WebSocket server is off"
    assert f"127.0.0.1:{port}" in err.detail


async def test_nothing_is_sent_to_a_peer_that_is_not_loopback(fake, monkeypatch):
    monkeypatch.setattr(obs, "_peer_host", lambda writer: "203.0.113.7")
    err = await error_of(opened(config(fake)))
    assert err.code == "config" and obs.ALLOW_REMOTE_ENV in err.message
    assert err.detail == "127.0.0.1 resolved to 203.0.113.7" and "203.0.113.7" not in err.message
    await until(lambda: fake.connections == 1)
    await asyncio.sleep(0.05)
    assert fake.upgrades == []
    await opened(obs.parse_url(fake.url, allow_remote=True))
    assert len(fake.upgrades) == 1


# login

async def test_password_login(fake):
    fake.password = PASSWORD
    assert "activeFps" in await call(fake, "GetStats", password=PASSWORD)
    identify = fake.identifies[-1]
    assert identify == {"rpcVersion": 1, "eventSubscriptions": 0,
                        "authentication": expected_auth(PASSWORD, fake.salt, fake.challenge)}


async def test_wrong_password(fake):
    fake.password = PASSWORD
    err = await error_of(call(fake, "GetStats", password="Wr0ng-pass"))
    assert err.code == "auth" and err.message == "OBS rejected the password" and "4009" in err.detail
    assert fake.requests == []
    await until(lambda: fake.client_closes == [4009])


async def test_password_needed_but_not_set(fake):
    fake.password = PASSWORD
    err = await error_of(call(fake, "GetStats"))
    assert err.code == "auth" and err.message == f"OBS asks for a password; set {obs.PASSWORD_ENV}"
    assert fake.identifies == []


async def test_password_set_but_not_needed(fake):
    assert "activeFps" in await call(fake, "GetStats", password=PASSWORD)
    assert "authentication" not in fake.identifies[0]


async def test_malformed_authentication_challenge(fake):
    fake.password, fake.salt = PASSWORD, None
    err = await error_of(call(fake, "GetStats", password=PASSWORD))
    assert err.code == "protocol" and "authentication object" in err.detail


async def test_identify_timeout_on_a_server_without_hello(fake, quick):
    fake.hello = False
    err = await error_of(call(fake, "GetStats"))
    assert err.code == "timeout" and "login" in err.message and "OBS 28" in err.detail


async def test_negotiated_rpc_version_must_be_1(fake):
    fake.negotiated = 2
    err = await error_of(call(fake, "GetStats"))
    assert err.code == "protocol" and "RPC version 2" in err.detail


async def test_newer_server_rpc_version_still_identifies_as_1(fake):
    fake.rpc_version = 2
    assert "activeFps" in await call(fake, "GetStats")
    assert fake.identifies[0]["rpcVersion"] == 1


# requests

async def test_request_ids_are_fresh_uuids(fake):
    async with obs.session(config(fake)) as client:
        for _ in range(3):
            await client.call("GetStats")
    assert len(set(fake.request_ids)) == 3
    assert all(uuid.UUID(request_id) for request_id in fake.request_ids)
    assert fake.requests == [("GetStats", {})] * 3


async def test_failed_request_carries_the_obs_comment(fake):
    err = await error_of(call(fake, "SetCurrentProgramScene", {"sceneName": "Nope"}))
    assert isinstance(err, RequestFailed) and err.status == 600
    assert err.message == "OBS refused SetCurrentProgramScene"
    assert str(err) == "OBS refused SetCurrentProgramScene (status 600: No source was found by the name of `Nope`.)"


async def test_failure_without_comment_names_the_status(fake):
    fake.replay = False
    err = await error_of(call(fake, "SaveReplayBuffer"))
    assert err.status == 501 and err.detail == "status 501: the output is not running"


async def test_unknown_request_type(fake):
    err = await error_of(call(fake, "StartTheShow"))
    assert err.status == 204 and "Your request type is not valid." in err.detail


async def test_obs_not_ready(fake):
    fake.fail["GetStats"] = (207, None)
    err = await error_of(call(fake, "GetStats"))
    assert err.status == 207 and err.message.startswith("OBS is busy")


async def test_request_timeout(fake, quick):
    fake.hold = {"GetStats"}
    err = await error_of(call(fake, "GetStats"))
    assert err.code == "timeout" and err.message == "OBS did not answer in time" and "GetStats" in err.detail


async def test_events_and_other_request_ids_are_skipped(fake):
    fake.events = 3
    stray = {"op": 7, "d": {"requestType": "GetStats", "requestId": "someone-else",
                            "requestStatus": {"result": True, "code": 100}, "responseData": {"activeFps": 1}}}
    fake.prelude["GetStats"] = frame(TEXT, json.dumps(stray).encode())
    assert (await call(fake, "GetStats"))["activeFps"] == fake.stats["activeFps"]


async def test_response_without_request_status(fake):
    fake.on_request = lambda request_type, data: fake.raw.update(GetStats=frame(TEXT, json.dumps(
        {"op": 7, "d": {"requestType": "GetStats", "requestId": fake.request_ids[-1]}}).encode()))
    err = await error_of(call(fake, "GetStats"))
    assert err.code == "protocol" and "requestStatus" in err.detail


# framing

async def test_fragmented_messages_with_pings_in_between(fake):
    fake.fragment, fake.ping = 7, b"beat"
    async with obs.session(config(fake)) as client:
        current, names = await obs.scenes(client)
    assert (current, names) == ("Main", ["Main", "BRB", "Gaming"])
    assert fake.pongs == [b"beat"] * 6  # two pings in each of Hello, Identified and the answer


async def test_client_frames_are_masked_with_fresh_keys(fake):
    async with obs.session(config(fake)) as client:
        for _ in range(3):
            await client.call("GetStats")
    assert fake.unmasked == 0 and len(fake.mask_keys) == 5  # Identify, three requests, close
    assert len(set(fake.mask_keys)) == 5


async def test_long_messages_use_extended_lengths(fake):
    async with obs.session(config(fake)) as client:
        await client.call("SetInputSettings", {"inputName": "tallyhand_caption", "inputSettings": {"text": "a" * 300}})
        await client.call("SetInputSettings", {"inputName": "BRB Text", "inputSettings": {"text": "b" * 70000}})
        read = await client.call("GetInputSettings", {"inputName": "BRB Text"})
    assert fake.caption() == "a" * 300 and read["inputSettings"]["text"] == "b" * 70000


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    first, second = await reader.readexactly(2)
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", await reader.readexactly(2))
    elif length == 127:
        (length,) = struct.unpack("!Q", await reader.readexactly(8))
    return first & 0x0F, await reader.readexactly(length)


async def test_fake_drops_unmasked_client_frames_like_obs(fake):
    reader, writer = await asyncio.open_connection("127.0.0.1", fake.port)
    key = base64.b64encode(os.urandom(16)).decode()
    writer.write(f"GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}"
                 "\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
    assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 101")
    opcode, payload = await read_frame(reader)
    assert opcode == TEXT and json.loads(payload)["op"] == 0
    writer.write(frame(TEXT, b'{"op": 1, "d": {"rpcVersion": 1}}'))
    opcode, payload = await read_frame(reader)
    assert opcode == CLOSE and struct.unpack("!H", payload[:2])[0] == 1002
    assert fake.unmasked == 1 and fake.identifies == []
    writer.close()


async def test_server_close_is_answered(fake):
    fake.raw["GetStats"] = close_frame(4011, "Kicked")
    err = await error_of(call(fake, "GetStats"))
    assert err.code == "closed" and err.message == "OBS ended this WebSocket session"
    assert err.detail == "close code 4011: Kicked"
    await until(lambda: fake.client_closes == [4011])


async def test_server_close_without_status(fake):
    fake.raw["GetStats"] = frame(CLOSE)
    err = await error_of(call(fake, "GetStats"))
    assert err.code == "closed" and err.detail == "close code 1005"


async def test_server_close_with_rpc_refusal(fake):
    fake.raw["GetStats"] = close_frame(4010, "Your requested RPC version is not supported.")
    err = await error_of(call(fake, "GetStats"))
    assert err.code == "protocol" and "RPC version 1" in err.message


async def test_connection_dropped_mid_request(fake):
    fake.hangup = {"GetStats"}
    err = await error_of(call(fake, "GetStats"))
    assert err.code == "closed" and err.message == "OBS dropped the connection"


async def test_stream_ends_inside_a_frame(fake):
    fake.raw["GetStats"] = frame(TEXT, b"abc", length=10)
    fake.hangup = {"GetStats"}
    err = await error_of(call(fake, "GetStats"))
    assert err.code == "closed" and "1006" in err.detail


async def test_oversized_message_is_refused_before_reading_it(fake):
    fake.raw["GetStats"] = frame(TEXT, length=obs.MAX_MESSAGE + 1)
    err = await error_of(call(fake, "GetStats"))
    assert err.code == "protocol" and "larger" in err.message and str(obs.MAX_MESSAGE) in err.detail
    await until(lambda: fake.client_closes == [1009])


async def test_message_under_the_cap_is_read(fake, monkeypatch):
    monkeypatch.setattr(obs, "MAX_MESSAGE", 1000)
    fake.inputs["BRB Text"].settings["text"] = "x" * 700
    async with obs.session(config(fake)) as client:
        assert client.ws.max_message == 1000
        assert (await client.call("GetInputSettings", {"inputName": "BRB Text"}))["inputSettings"]["text"] == "x" * 700


async def test_size_cap_counts_every_fragment(fake, monkeypatch):
    monkeypatch.setattr(obs, "MAX_MESSAGE", 64)
    fake.fragment = 40
    err = await error_of(call(fake, "GetStats"))
    assert err.code == "protocol" and "over 64 bytes" in err.detail
    await until(lambda: fake.client_closes == [1009])


@pytest.mark.parametrize("raw, why, code", [
    (frame(TEXT, b"{}", rsv=0x40), "reserved bits", 1002),
    (frame(TEXT, b"{}", mask=b"abcd"), "masked a frame", 1002),
    (frame(CONT, b"{}"), "continuation", 1002),
    (frame(TEXT, b"{", fin=False) + frame(TEXT, b"}"), "new message inside", 1002),
    (frame(PING, b"x" * 126), "control frame", 1002),
    (frame(PING, b"x", fin=False), "control frame", 1002),
    (frame(0x3, b"x"), "unknown opcode 3", 1002),
    (frame(0xB), "unknown control opcode 11", 1002),
    (frame(CLOSE, b"\x03"), "one-byte close", 1002),
    (frame(BINARY, b"{}"), "binary", 1003),
    (frame(TEXT, b"\xff\xfe"), "UTF-8", 1007),
    (frame(TEXT, b"not json"), "not JSON", 1002),
    (frame(TEXT, b"[1, 2]"), "op and d", 1002),
    (frame(TEXT, b'{"op": "7", "d": {}}'), "op and d", 1002),
    (frame(TEXT, b'{"op": 7, "d": []}'), "op and d", 1002),
    (frame(TEXT, b'{"op": 2, "d": {}}'), "opcode 2 while waiting for 7", 1002),
])
async def test_protocol_violations_close_the_connection(fake, raw, why, code):
    fake.raw["GetStats"] = raw
    err = await error_of(call(fake, "GetStats"))
    assert err.code == "protocol" and why in err.detail
    await until(lambda: fake.client_closes == [code])


async def test_masked_server_frames_are_refused(fake):
    fake.mask_frames = True
    err = await error_of(call(fake, "GetStats"))
    assert err.code == "protocol" and "masked a frame" in err.detail


# OBS operations

async def run(fake: FakeObs, operation, *args):
    async with obs.session(config(fake)) as client:
        return await operation(client, *args)


async def test_status_while_live(fake):
    fake.streaming = fake.recording = True
    assert await run(fake, obs.status) == {
        "live_scene": "Main", "streaming": True, "reconnecting": False, "recording": True, "recording_paused": False,
        "fps": 60.0, "cpu_percent": 3.5, "stream_time": "01:02:03.456", "record_time": "00:10:00.000",
        "dropped_frames": {"network": {"dropped": 12, "total": 215000}, "encoding": {"dropped": 1, "total": 216000},
                           "rendering": {"dropped": 3, "total": 216500}}}
    assert fake.request_types() == ["GetCurrentProgramScene", "GetStreamStatus", "GetRecordStatus", "GetStats"]


async def test_status_while_idle_on_an_older_obs_websocket(fake):
    fake.legacy_scene_fields = True
    fake.stats["activeFps"] = "unknown"
    result = await run(fake, obs.status)
    assert result["live_scene"] == "Main" and result["fps"] is None
    assert not result["streaming"] and "stream_time" not in result and "record_time" not in result
    assert result["dropped_frames"]["network"] == {"dropped": 0, "total": 0}


async def test_scenes_top_to_bottom(fake):
    fake.program = "Gaming"
    assert await run(fake, obs.scenes) == ("Gaming", ["Main", "BRB", "Gaming"])


async def test_switch_scene(fake):
    assert await run(fake, obs.switch_scene, "BRB") == ("Main", "BRB")
    assert fake.program == "BRB"
    assert fake.request_types() == ["GetSceneList", "SetCurrentProgramScene"]
    assert fake.requests[-1] == ("SetCurrentProgramScene", {"sceneName": "BRB"})


async def test_switch_scene_matches_case_when_only_one_fits(fake):
    assert await run(fake, obs.switch_scene, "gaming") == ("Main", "Gaming")
    fake.scenes["gaming"] = []
    fake.scene_uuids["gaming"] = "u"
    err = await error_of(run(fake, obs.switch_scene, "GAMING"))
    assert err.code == "not_found" and err.message == 'scene "GAMING" does not exist'


async def test_switch_to_the_live_scene_changes_nothing(fake):
    assert await run(fake, obs.switch_scene, "Main") == ("Main", "Main")
    assert fake.request_types() == ["GetSceneList"]


async def test_switch_to_an_unknown_scene(fake):
    err = await error_of(run(fake, obs.switch_scene, "Nope\nlines"))
    assert err.code == "not_found" and err.message == 'scene "Nope lines" does not exist'
    assert "SetCurrentProgramScene" not in fake.request_types()


async def test_switch_to_a_scene_removed_meanwhile(fake):
    fake.fail["SetCurrentProgramScene"] = (600, "No source was found by the name of `BRB`.")
    err = await error_of(run(fake, obs.switch_scene, "BRB"))
    assert err.code == "not_found" and err.message == 'scene "BRB" does not exist'


AUDIO_LIST = [{"name": "Desktop Audio", "muted": False}, {"name": "Mic/Aux", "muted": True},
              {"name": "Camera", "muted": False}]


async def test_audio_inputs_use_input_caps(fake):
    assert await run(fake, obs.audio_inputs) == AUDIO_LIST
    assert [data["inputName"] for kind, data in fake.requests if kind == "GetInputMute"] == [
        "Desktop Audio", "Mic/Aux", "Camera"]


async def test_audio_inputs_without_input_caps(fake):
    fake.input_caps = False
    assert await run(fake, obs.audio_inputs) == AUDIO_LIST
    assert fake.request_types().count("GetInputMute") == len(fake.inputs)


async def test_audio_inputs_pass_other_failures_on(fake):
    fake.fail["GetInputMute"] = (205, "Something broke.")
    err = await error_of(run(fake, obs.audio_inputs))
    assert isinstance(err, RequestFailed) and "Something broke." in err.detail


@pytest.mark.parametrize("wanted, muted, result", [
    ("Mic/Aux", False, ("Mic/Aux", False)),
    ("mic/aux", True, ("Mic/Aux", True)),
    ("Desktop Audio", True, ("Desktop Audio", True)),
    ("Mic/Aux", None, ("Mic/Aux", False)),
    ("Desktop Audio", None, ("Desktop Audio", True)),
])
async def test_set_mute(fake, wanted, muted, result):
    assert await run(fake, obs.set_mute, wanted, muted) == result
    assert fake.inputs[result[0]].muted is result[1]


async def test_set_mute_failures(fake):
    err = await error_of(run(fake, obs.set_mute, "Webcam Frame", True))
    assert err.code == "no_audio" and err.message == 'input "Webcam Frame" has no audio'
    err = await error_of(run(fake, obs.set_mute, "Kazoo", True))
    assert err.code == "not_found" and err.message == 'input "Kazoo" does not exist'


async def test_text_source_set_and_clear_if_unchanged(fake):
    await run(fake, obs.set_text, "tallyhand_caption", "hello chat")
    assert fake.caption() == "hello chat"
    assert fake.requests[-1] == ("SetInputSettings", {"inputName": "tallyhand_caption",
                                                      "inputSettings": {"text": "hello chat"}, "overlay": True})
    assert await run(fake, obs.clear_text_if, "tallyhand_caption", "something else") is False
    assert fake.caption() == "hello chat"
    assert await run(fake, obs.clear_text_if, "tallyhand_caption", "hello chat") is True
    assert fake.caption() == ""


async def test_text_source_must_exist_and_be_text(fake):
    err = await error_of(run(fake, obs.set_text, "nothing_here", "hi"))
    assert err.code == "not_found" and obs.CAPTION_SOURCE_ENV in err.message
    err = await error_of(run(fake, obs.set_text, "Webcam Frame", "hi"))
    assert err.code == "not_text" and "image_source" in err.detail
    err = await error_of(run(fake, obs.set_text, "Main", "hi"))
    assert err.code == "not_found"
    assert "SetInputSettings" not in fake.request_types()


async def test_set_source_visible_covers_every_item_of_the_source(fake):
    assert await run(fake, obs.set_source_visible, "gaming", "camera", False) == ("Gaming", "Camera")
    assert [item.enabled for item in fake.scenes["Gaming"]] == [True, False, False]
    assert [data["sceneItemId"] for kind, data in fake.requests if kind == "SetSceneItemEnabled"] == [3, 2]
    await run(fake, obs.set_source_visible, "Gaming", "Camera", True)
    assert all(item.enabled for item in fake.scenes["Gaming"])


async def test_set_source_visible_failures(fake):
    err = await error_of(run(fake, obs.set_source_visible, "Nope", "Camera", False))
    assert err.message == 'scene "Nope" does not exist'
    err = await error_of(run(fake, obs.set_source_visible, "BRB", "Camera", False))
    assert err.message == 'source "Camera" is not in scene "BRB"'
    fake.fail["GetSceneItemList"] = (600, "No source was found by the name of `BRB`.")
    err = await error_of(run(fake, obs.set_source_visible, "BRB", "BRB Text", False))
    assert err.message == 'scene "BRB" does not exist'


async def test_save_replay(fake):
    fake.replay = True
    await run(fake, obs.save_replay)
    assert fake.replays_saved == 1
    assert fake.request_types() == ["GetReplayBufferStatus", "SaveReplayBuffer"]


@pytest.mark.parametrize("replay, fail, code, message", [
    (None, None, "replay_off", obs.REPLAY_OFF),
    (False, None, "replay_stopped", obs.REPLAY_STOPPED),
    (True, (501, None), "replay_stopped", obs.REPLAY_STOPPED),
])
async def test_save_replay_failures(fake, replay, fail, code, message):
    fake.replay = replay
    if fail:
        fake.fail["SaveReplayBuffer"] = fail
    err = await error_of(run(fake, obs.save_replay))
    assert (err.code, err.message) == (code, message) and fake.replays_saved == 0


# secrets

async def test_no_password_or_auth_string_in_errors_or_logs(fake, caplog):
    caplog.set_level(logging.DEBUG)
    fake.password, fake.echo_auth = PASSWORD, True
    wrong = await error_of(call(fake, "GetStats", password="Wr0ng-pass"))
    sent_wrong = fake.identifies[-1]["authentication"]
    failed = await error_of(call(fake, "SetCurrentProgramScene", {"sceneName": "Nope"}, password=PASSWORD))
    sent_right = fake.identifies[-1]["authentication"]
    assert sent_wrong and sent_right and sent_wrong != sent_right
    assert "***" in wrong.detail and failed.detail.count("***") == 2
    for err, secrets in ((wrong, ("Wr0ng-pass", sent_wrong)), (failed, (PASSWORD, sent_right))):
        for text in (str(err), repr(err), err.message, err.detail, getattr(err, "comment", "")):
            assert not any(secret in text for secret in secrets)
    for secret in ("Wr0ng-pass", sent_wrong, PASSWORD, sent_right):
        assert secret not in caplog.text
        assert not any(secret in value for upgrade in fake.upgrades for value in upgrade.values())


def test_a_status_line_needs_ascii_digits():
    assert obs._parse_head("HTTP/1.1 1\u00b23 Switching\r\nUpgrade: websocket".encode("latin-1")) == (0, {})


async def test_json_nested_past_the_parser_is_a_protocol_error():
    class Socket:
        async def recv(self) -> str:
            return "[" * 100_000 + "]" * 100_000

        async def protocol_error(self, code: int, why: str) -> ObsError:
            return ObsError("protocol", "OBS sent a malformed WebSocket message", detail=why)

    client = obs.ObsClient(Socket(), obs.ObsConfig("127.0.0.1", 4455))
    with pytest.raises(ObsError, match="malformed"):
        await client._expect(obs.HELLO)
