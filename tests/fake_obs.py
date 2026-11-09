"""Local stand-in for OBS's WebSocket server (obs-websocket 5.x) on 127.0.0.1: the HTTP upgrade, Hello with or
without an authentication challenge, Identify checks, and the requests Tallyhand sends, answered with
obs-websocket's response fields, comments and failure codes. Like a real server it closes the connection on an
unmasked client frame. Attributes switch on misbehaviour for the failure tests; everything a client sends is kept."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import struct
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CONT, TEXT, BINARY, CLOSE, PING, PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
VIDEO, AUDIO = 1 << 0, 1 << 1  # libobs output flags, as inputKindCaps carries them


def unmask(data: bytes, key: bytes) -> bytes:
    return bytes(byte ^ key[index % 4] for index, byte in enumerate(data))


def frame(opcode: int, payload: bytes = b"", *, fin: bool = True, mask: bytes | None = None, rsv: int = 0,
          length: int | None = None) -> bytes:
    """One raw frame: unmasked as a server sends it, or masked with ``mask`` as a client must. ``length`` overrides
    the announced payload length."""
    size = len(payload) if length is None else length
    bit = 0x80 if mask else 0
    head = bytes([(0x80 if fin else 0) | rsv | opcode])
    if size < 126:
        head += bytes([bit | size])
    elif size < 1 << 16:
        head += bytes([bit | 126]) + struct.pack("!H", size)
    else:
        head += bytes([bit | 127]) + struct.pack("!Q", size)
    return head + (mask + unmask(payload, mask) if mask else payload)


def close_frame(code: int, reason: str = "") -> bytes:
    return frame(CLOSE, struct.pack("!H", code) + reason.encode("utf-8"))


def expected_auth(password: str, salt: str, challenge: str) -> str:
    """The authentication string a client must send (protocol.md, "Creating an authentication string")."""
    base64_secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest())
    return base64.b64encode(hashlib.sha256(base64_secret + challenge.encode("utf-8")).digest()).decode("ascii")


@dataclass
class Input:
    kind: str
    audio: bool = False
    video: bool = True
    muted: bool = False
    settings: dict = field(default_factory=dict)
    uuid: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class Item:
    source: str
    item_id: int
    enabled: bool = True


class Fail(Exception):
    def __init__(self, code: int, comment: str | None = None):
        super().__init__(code)
        self.code, self.comment = code, comment


class _Conn:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer
        self.closing = False
        self.auth = self.challenge = ""


class FakeObs:
    """obs-websocket fake. Scenes are kept top to bottom as OBS's scene list shows them; answers use the same
    reversed order and sceneIndex numbering obs-websocket uses."""

    def __init__(self, *, password: str | None = None):
        self.password = password
        self.salt = base64.b64encode(os.urandom(32)).decode()
        self.challenge = ""  # the newest connection's; every connection gets a fresh one, as in OBS
        self.scenes: dict[str, list[Item]] = {
            "Main": [Item("Camera", 1), Item("Webcam Frame", 2), Item("tallyhand_caption", 3)],
            "BRB": [Item("BRB Text", 1), Item("tallyhand_caption", 2)],
            "Gaming": [Item("Game Capture", 1), Item("Camera", 2), Item("Camera", 3)],
        }
        self.scene_uuids = {name: str(uuid.uuid4()) for name in self.scenes}
        self.program = "Main"
        self.inputs: dict[str, Input] = {
            "Desktop Audio": Input("coreaudio_output_capture", audio=True, video=False),
            "Mic/Aux": Input("coreaudio_input_capture", audio=True, video=False, muted=True),
            "Camera": Input("av_capture_input_v2", audio=True),
            "Game Capture": Input("screen_capture"),
            "Webcam Frame": Input("image_source", settings={"file": "frame.png"}),
            "BRB Text": Input("text_ft2_source_v2", settings={"text": "Be right back"}),
            "tallyhand_caption": Input("text_ft2_source_v2"),
        }
        self.streaming = self.reconnecting = self.recording = self.recording_paused = False
        self.replay: bool | None = None  # None: the replay buffer is turned off in OBS's settings
        self.replays_saved = 0
        self.stats = {"cpuUsage": 3.4567, "memoryUsage": 812.5, "availableDiskSpace": 120000.0,
                      "activeFps": 60.0000002, "averageFrameRenderTime": 1.25, "renderSkippedFrames": 3,
                      "renderTotalFrames": 216500, "outputSkippedFrames": 1, "outputTotalFrames": 216000,
                      "webSocketSessionIncomingMessages": 0, "webSocketSessionOutgoingMessages": 0}
        self.stream_frames = (12, 215000)
        # Misbehaviour switches. handshake: "ok", "404", "bad_accept", "no_upgrade", "protocol" (wrong subprotocol),
        # "no_protocol", "extension", "hangup" or "silent" (never answers the upgrade).
        self.handshake = "ok"
        self.hello = True  # False: never send Hello, like an obs-websocket 4.x server
        self.rpc_version = self.negotiated = 1
        self.fragment = 0  # split each outgoing message into frames of this many bytes
        self.ping = b""  # ping payload sent before each message and between its first two fragments
        self.mask_frames = False  # mask server frames, which RFC 6455 forbids
        self.events = 0  # events sent before every answer
        self.input_caps = True  # GetInputList carries inputKindCaps (newer obs-websocket)
        self.legacy_scene_fields = False  # GetCurrentProgramScene without sceneName (before obs-websocket 5.5)
        self.hold: set[str] = set()  # request types never answered
        self.fail: dict[str, tuple[int, str | None]] = {}  # request type -> forced (code, comment)
        self.prelude: dict[str, bytes] = {}  # request type -> raw bytes sent just before the answer
        self.raw: dict[str, bytes] = {}  # request type -> raw bytes sent instead of the answer
        self.hangup: set[str] = set()  # request types answered by dropping the connection
        self.echo_auth = False  # repeat the client's auth string in close reasons, and it and the password in comments
        self.on_request: Callable[[str, dict], None] | None = None
        # What clients did.
        self.connections = 0
        self.upgrades: list[dict[str, str]] = []
        self.identifies: list[dict] = []
        self.requests: list[tuple[str, dict]] = []
        self.request_ids: list[str] = []
        self.pongs: list[bytes] = []
        self.client_closes: list[int | None] = []
        self.mask_keys: list[bytes] = []
        self.unmasked = 0
        self.port = 0
        self._server: asyncio.base_events.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self._handlers = {
            "GetStats": self._get_stats, "GetSceneList": self._get_scene_list,
            "GetCurrentProgramScene": self._get_program_scene, "SetCurrentProgramScene": self._set_program_scene,
            "GetInputList": self._get_input_list, "GetInputMute": self._get_input_mute,
            "SetInputMute": self._set_input_mute, "ToggleInputMute": self._toggle_input_mute,
            "GetInputSettings": self._get_input_settings, "SetInputSettings": self._set_input_settings,
            "GetSceneItemList": self._get_scene_item_list, "SetSceneItemEnabled": self._set_scene_item_enabled,
            "GetStreamStatus": self._get_stream_status, "GetRecordStatus": self._get_record_status,
            "GetReplayBufferStatus": self._get_replay_status, "SaveReplayBuffer": self._save_replay,
        }

    async def start(self) -> FakeObs:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        for writer in self._writers:
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def request_types(self) -> list[str]:
        return [request_type for request_type, _ in self.requests]

    def caption(self, source: str = "tallyhand_caption") -> str | None:
        return self.inputs[source].settings.get("text")

    # connection

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.append(writer)
        conn = _Conn(reader, writer)
        try:
            if await self._upgrade(conn):
                await self._session(conn)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _upgrade(self, conn: _Conn) -> bool:
        head = (await conn.reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
        headers = {"request": head[0]}
        for line in head[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        self.upgrades.append(headers)
        key = headers.get("sec-websocket-key", "")
        try:
            key_ok = len(base64.b64decode(key, validate=True)) == 16
        except ValueError:
            key_ok = False
        valid = (head[0].startswith("GET ") and head[0].endswith(" HTTP/1.1") and key_ok
                 and headers.get("upgrade", "").lower() == "websocket"
                 and "upgrade" in headers.get("connection", "").lower()
                 and headers.get("sec-websocket-version") == "13")
        if self.handshake == "silent":
            await conn.reader.read()
            return False
        if self.handshake == "hangup":
            return False
        if not valid or self.handshake == "404":
            conn.writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            await conn.writer.drain()
            return False
        accept = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
        lines = ["HTTP/1.1 101 Switching Protocols",
                 "Upgrade: h2c" if self.handshake == "no_upgrade" else "Upgrade: websocket", "Connection: Upgrade",
                 "Sec-WebSocket-Accept: " + (base64.b64encode(os.urandom(20)).decode()
                                             if self.handshake == "bad_accept" else accept)]
        requested = [p.strip() for p in headers.get("sec-websocket-protocol", "").split(",")]
        if self.handshake == "protocol":
            lines.append("Sec-WebSocket-Protocol: obswebsocket.msgpack")
        elif "obswebsocket.json" in requested and self.handshake != "no_protocol":
            lines.append("Sec-WebSocket-Protocol: obswebsocket.json")
        if self.handshake == "extension":
            lines.append("Sec-WebSocket-Extensions: permessage-deflate")
        conn.writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await conn.writer.drain()
        return True

    async def _session(self, conn: _Conn) -> None:
        if not self.hello:
            await conn.reader.read()
            return
        hello: dict = {"obsStudioVersion": "31.1.2", "obsWebSocketVersion": "5.6.2", "rpcVersion": self.rpc_version}
        conn.challenge = self.challenge = base64.b64encode(os.urandom(32)).decode()
        if self.password is not None:
            hello["authentication"] = {"challenge": conn.challenge, "salt": self.salt}
        await self.send(conn, {"op": 0, "d": hello})
        identified = False
        while (text := await self._read_message(conn)) is not None:
            try:
                message = json.loads(text)
                op, data = message["op"], message.get("d") or {}
            except (ValueError, KeyError, TypeError):
                return await self._close(conn, 4002, "Unable to decode the message.")
            if not identified:
                if op != 1:
                    return await self._close(conn, 4007, "You must identify before sending other messages.")
                self.identifies.append(data)
                conn.auth = str(data.get("authentication") or "")
                if data.get("rpcVersion") != 1:
                    return await self._close(conn, 4010, "Your requested RPC version is not supported.")
                if self.password is not None and conn.auth != expected_auth(self.password, self.salt, conn.challenge):
                    reason = "Authentication failed."
                    if self.echo_auth:
                        reason += f" You sent {conn.auth}."
                    return await self._close(conn, 4009, reason)
                identified = True
                await self.send(conn, {"op": 2, "d": {"negotiatedRpcVersion": self.negotiated}})
            elif op == 6:
                if not await self._request(conn, data):
                    return
            elif op == 1:
                return await self._close(conn, 4008, "You are already identified.")
            else:
                return await self._close(conn, 4006, "Unknown OpCode.")

    async def _request(self, conn: _Conn, data: dict) -> bool:
        request_type, request_id = data.get("requestType"), data.get("requestId")
        request_data = data.get("requestData") or {}
        self.requests.append((request_type, request_data))
        self.request_ids.append(request_id)
        if self.on_request is not None:
            self.on_request(request_type, request_data)
        if request_type in self.hold:
            return True
        if request_type in self.raw:
            conn.writer.write(self.raw[request_type])
            await conn.writer.drain()
        if request_type in self.hangup:
            conn.writer.transport.abort()
            return False
        if request_type in self.raw:
            return True
        if request_type in self.prelude:
            conn.writer.write(self.prelude[request_type])
        for _ in range(self.events):
            await self.send(conn, {"op": 5, "d": {"eventType": "StudioModeStateChanged", "eventIntent": 1,
                                                  "eventData": {"studioModeEnabled": False}}})
        status: dict = {"result": True, "code": 100}
        response = None
        try:
            if request_type in self.fail:
                raise Fail(*self.fail[request_type])
            if (handler := self._handlers.get(request_type)) is None:
                raise Fail(204, "Your request type is not valid.")
            response = handler(request_data)
        except Fail as exc:
            status = {"result": False, "code": exc.code}
            comment = exc.comment
            if self.echo_auth:
                comment = f"{comment or 'Failed'} ({conn.auth} / {self.password})"
            if comment:
                status["comment"] = comment
        body = {"requestType": request_type, "requestId": request_id, "requestStatus": status}
        if response is not None:
            body["responseData"] = response
        await self.send(conn, {"op": 7, "d": body})
        return True

    # frames

    async def send(self, conn: _Conn, message: dict) -> None:
        """One text message, fragmented and with pings in between when those switches are on."""
        payload = json.dumps(message).encode()
        size = self.fragment or len(payload) or 1
        pieces = [payload[start:start + size] for start in range(0, len(payload), size)] or [b""]
        for index, piece in enumerate(pieces):
            if self.ping and index < 2:
                conn.writer.write(frame(PING, self.ping))
            conn.writer.write(frame(TEXT if index == 0 else CONT, piece, fin=index == len(pieces) - 1,
                                    mask=os.urandom(4) if self.mask_frames else None))
        await conn.writer.drain()

    async def _read_message(self, conn: _Conn) -> str | None:
        """The next text message from the client; None when it closed or broke the protocol."""
        chunks: list[bytes] = []
        while True:
            first, second = await conn.reader.readexactly(2)
            length = second & 0x7F
            if length == 126:
                (length,) = struct.unpack("!H", await conn.reader.readexactly(2))
            elif length == 127:
                (length,) = struct.unpack("!Q", await conn.reader.readexactly(8))
            if not second & 0x80:
                self.unmasked += 1
                await self._close(conn, 1002, "Received unmasked frame from client.", wait=False)
                return None
            key = await conn.reader.readexactly(4)
            self.mask_keys.append(key)
            payload = unmask(await conn.reader.readexactly(length), key)
            opcode = first & 0x0F
            if opcode == PING:
                conn.writer.write(frame(PONG, payload))
            elif opcode == PONG:
                self.pongs.append(payload)
            elif opcode == CLOSE:
                self.client_closes.append(struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None)
                if not conn.closing:
                    conn.closing = True
                    conn.writer.write(frame(CLOSE, payload[:2]))
                    await conn.writer.drain()
                return None
            else:
                chunks.append(payload)
                if first & 0x80:
                    return b"".join(chunks).decode("utf-8")

    async def _close(self, conn: _Conn, code: int, reason: str, *, wait: bool = True) -> None:
        """Server-initiated close: send the frame, then wait briefly for the client's answering close unless the
        client broke the framing, after which nothing it sends can be parsed."""
        conn.closing = True
        conn.writer.write(close_frame(code, reason))
        await conn.writer.drain()
        if not wait:
            return
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError, OSError, TimeoutError):
            async with asyncio.timeout(1.0):
                await self._read_message(conn)

    # requests (field checks, comments and codes as obs-websocket's Request::Validate* helpers give them)

    def _source(self, data: dict, key: str) -> str:
        name = data.get(key)
        if name is None:
            uuid_key = key.replace("Name", "Uuid")
            raise Fail(300, f"Your request must contain at least one of the following fields: `{key}` or "
                            f"`{uuid_key}`.")
        if not isinstance(name, str):
            raise Fail(401, f"The field value of `{key}` must be a string.")
        if not name:
            raise Fail(403, f"The field value of `{key}` must not be empty.")
        if name not in self.scenes and name not in self.inputs:
            raise Fail(600, f"No source was found by the name of `{name}`.")
        return name

    def _scene(self, data: dict) -> str:
        name = self._source(data, "sceneName")
        if name not in self.scenes:
            raise Fail(602, "The specified source is not a scene.")
        return name

    def _input(self, data: dict) -> Input:
        name = self._source(data, "inputName")
        if name not in self.inputs:
            raise Fail(602, "The specified source is not an input.")
        return self.inputs[name]

    @staticmethod
    def _bool(data: dict, key: str) -> bool:
        if key not in data:
            raise Fail(300, f"Your request is missing the `{key}` field.")
        if not isinstance(data[key], bool):
            raise Fail(401, f"The field value of `{key}` must be a boolean.")
        return data[key]

    @staticmethod
    def _no_audio(source: Input) -> None:
        if not source.audio:
            raise Fail(604, "The specified input does not support audio.")

    def _get_stats(self, data: dict) -> dict:
        return dict(self.stats)

    def _get_scene_list(self, data: dict) -> dict:
        names = list(self.scenes)
        scenes = [{"sceneIndex": len(names) - index - 1, "sceneName": name, "sceneUuid": self.scene_uuids[name]}
                  for index, name in enumerate(names)]
        scenes.reverse()
        return {"currentProgramSceneName": self.program, "currentProgramSceneUuid": self.scene_uuids[self.program],
                "currentPreviewSceneName": None, "currentPreviewSceneUuid": None, "scenes": scenes}

    def _get_program_scene(self, data: dict) -> dict:
        found = {"currentProgramSceneName": self.program, "currentProgramSceneUuid": self.scene_uuids[self.program]}
        if not self.legacy_scene_fields:
            found.update(sceneName=self.program, sceneUuid=self.scene_uuids[self.program])
        return found

    def _set_program_scene(self, data: dict) -> None:
        self.program = self._scene(data)

    def _get_input_list(self, data: dict) -> dict:
        inputs = []
        for name, source in self.inputs.items():
            if data.get("inputKind") and source.kind != data["inputKind"]:
                continue
            entry = {"inputKind": source.kind, "inputName": name, "inputUuid": source.uuid,
                     "unversionedInputKind": re.sub(r"_v\d+$", "", source.kind)}
            if self.input_caps:
                entry["inputKindCaps"] = (VIDEO if source.video else 0) | (AUDIO if source.audio else 0)
            inputs.append(entry)
        return {"inputs": inputs}

    def _get_input_mute(self, data: dict) -> dict:
        source = self._input(data)
        self._no_audio(source)
        return {"inputMuted": source.muted}

    def _set_input_mute(self, data: dict) -> None:
        source = self._input(data)
        muted = self._bool(data, "inputMuted")
        self._no_audio(source)
        source.muted = muted

    def _toggle_input_mute(self, data: dict) -> dict:
        source = self._input(data)
        self._no_audio(source)
        source.muted = not source.muted
        return {"inputMuted": source.muted}

    def _get_input_settings(self, data: dict) -> dict:
        source = self._input(data)
        return {"inputSettings": dict(source.settings), "inputKind": source.kind}

    def _set_input_settings(self, data: dict) -> None:
        source = self._input(data)
        settings = data.get("inputSettings")
        if settings is None:
            raise Fail(300, "Your request is missing the `inputSettings` field.")
        if not isinstance(settings, dict):
            raise Fail(401, "The field value of `inputSettings` must be an object.")
        source.settings = {**source.settings, **settings} if data.get("overlay", True) else dict(settings)

    def _get_scene_item_list(self, data: dict) -> dict:
        scene = self._scene(data)
        items = []
        for index, item in enumerate(reversed(self.scenes[scene])):  # bottom first, like obs-websocket
            source = self.inputs.get(item.source)
            items.append({"inputKind": source.kind if source else None, "isGroup": None,
                          "sceneItemBlendMode": "OBS_BLEND_NORMAL", "sceneItemEnabled": item.enabled,
                          "sceneItemId": item.item_id, "sceneItemIndex": index, "sceneItemLocked": False,
                          "sceneItemTransform": {"positionX": 0.0, "positionY": 0.0, "rotation": 0.0},
                          "sourceName": item.source, "sourceType": "OBS_SOURCE_TYPE_INPUT",
                          "sourceUuid": source.uuid if source else None})
        return {"sceneItems": items}

    def _set_scene_item_enabled(self, data: dict) -> None:
        scene = self._scene(data)
        item_id = data.get("sceneItemId")
        if item_id is None:
            raise Fail(300, "Your request is missing the `sceneItemId` field.")
        if not isinstance(item_id, int) or isinstance(item_id, bool):
            raise Fail(401, "The field value of `sceneItemId` must be a number.")
        if item_id < 0:
            raise Fail(402, "The field value of `sceneItemId` is below the minimum of `0`")
        enabled = self._bool(data, "sceneItemEnabled")
        for item in self.scenes[scene]:
            if item.item_id == item_id:
                item.enabled = enabled
                return
        raise Fail(600, f"No scene items were found in scene `{scene}` with the ID `{item_id}`.")

    def _get_stream_status(self, data: dict) -> dict:
        skipped, total = self.stream_frames if self.streaming else (0, 0)
        return {"outputActive": self.streaming, "outputReconnecting": self.reconnecting,
                "outputTimecode": "01:02:03.456" if self.streaming else "00:00:00.000",
                "outputDuration": 3723456 if self.streaming else 0, "outputCongestion": 0.0,
                "outputBytes": 1_234_567_890 if self.streaming else 0, "outputSkippedFrames": skipped,
                "outputTotalFrames": total}

    def _get_record_status(self, data: dict) -> dict:
        return {"outputActive": self.recording, "outputPaused": self.recording_paused,
                "outputTimecode": "00:10:00.000" if self.recording else "00:00:00.000",
                "outputDuration": 600000 if self.recording else 0, "outputBytes": 98765 if self.recording else 0}

    def _get_replay_status(self, data: dict) -> dict:
        if self.replay is None:
            raise Fail(604, "Replay buffer is not available.")
        return {"outputActive": self.replay}

    def _save_replay(self, data: dict) -> None:
        if not self.replay:
            raise Fail(501)
        self.replays_saved += 1
