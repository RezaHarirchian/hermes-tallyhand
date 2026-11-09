"""OBS control through obs-websocket 5.x (RPC version 1): a small RFC 6455 client on asyncio streams, the Hello,
Identify and Identified login, single requests, and the OBS operations Tallyhand's tools and chat commands use.
Every call opens its own short-lived connection. Stdlib only."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import struct
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from gateway.platforms._shared import get_scoped_secret

from .privacy import truthy
from .triggers import one_line

logger = logging.getLogger(__name__)

URL_ENV, PASSWORD_ENV, ALLOW_REMOTE_ENV = "OBS_WEBSOCKET_URL", "OBS_WEBSOCKET_PASSWORD", "TALLYHAND_OBS_ALLOW_REMOTE"
BRB_SCENE_ENV, LIVE_SCENE_ENV = "TALLYHAND_BRB_SCENE", "TALLYHAND_LIVE_SCENE"
CAPTION_SOURCE_ENV = "TALLYHAND_CAPTION_SOURCE"
DEFAULT_URL = "ws://127.0.0.1:4455"
DEFAULT_CAPTION_SOURCE = "tallyhand_caption"
SUBPROTOCOL = "obswebsocket.json"
RPC_VERSION = 1
CONNECT_TIMEOUT, IDENTIFY_TIMEOUT, REQUEST_TIMEOUT, CLOSE_TIMEOUT = 5.0, 5.0, 10.0, 1.0
MAX_MESSAGE = 16 * 1024 * 1024

# obs-websocket opcodes ("Message Types (OpCodes)") and the requestStatus codes this module acts on.
HELLO, IDENTIFY, IDENTIFIED, EVENT, REQUEST, REQUEST_RESPONSE = 0, 1, 2, 5, 6, 7
NOT_READY, OUTPUT_NOT_RUNNING = 207, 501
RESOURCE_NOT_FOUND, INVALID_RESOURCE_TYPE, INVALID_RESOURCE_STATE = 600, 602, 604
OBS_SOURCE_AUDIO = 1 << 1  # libobs output flag carried in inputKindCaps

UNREACHABLE = "OBS is not running or its WebSocket server is off"
NOT_OBS = f"{URL_ENV} does not point at an OBS WebSocket server"
BAD_PASSWORD = "OBS rejected the password"
REPLAY_OFF = "the replay buffer is turned off in OBS (Settings, Output, Replay Buffer)"
REPLAY_STOPPED = "the replay buffer is not running; start it in OBS first"
_CLOSE_ERRORS = {
    1006: ("closed", "OBS dropped the connection"),
    4009: ("auth", BAD_PASSWORD),
    4010: ("protocol", "this OBS does not accept obs-websocket RPC version 1"),
    4011: ("closed", "OBS ended this WebSocket session"),
}
_STATUS_TEXT = {
    203: "request type missing", 204: "unknown request type", 205: "error", 207: "OBS is not ready",
    300: "a required field is missing", 301: "request data missing", 400: "invalid field",
    401: "a field has the wrong type", 402: "a number is out of range", 403: "a field is empty",
    500: "the output is running", 501: "the output is not running", 502: "the output is paused",
    503: "the output is not paused", 504: "the output is disabled", 600: "not found", 601: "already exists",
    602: "wrong kind of source", 604: "not possible in its current state", 702: "processing failed",
}
_ACCEPT_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_CONT, _TEXT, _BINARY, _CLOSE, _PING, _PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
_HOST_RE = re.compile(r"[A-Za-z0-9._:%-]+")
_TARGET_RE = re.compile(r"/[!-~]*")


class ObsError(Exception):
    """A failed OBS call. ``message`` is the reason in the user's words and safe for public chat; ``detail`` adds
    where or why (the endpoint, OBS's own comment) for the owner. Neither ever holds the password or auth string."""

    def __init__(self, code: str, message: str, *, detail: str = "", status: int | None = None):
        super().__init__(f"{message} ({detail})" if detail else message)
        self.code, self.message, self.detail, self.status = code, message, detail, status


class ConnectionClosed(ObsError):
    """The WebSocket closed under us: a close frame from OBS, or the stream just ended (1006). ``reason`` is
    OBS's raw text; ObsClient scrubs it before it reaches any message."""

    def __init__(self, close_code: int, reason: str = ""):
        super().__init__("closed", "OBS closed the connection", detail=f"close code {close_code}")
        self.close_code, self.reason = close_code, reason


class RequestFailed(ObsError):
    """OBS answered a request with a failed requestStatus; the detail carries its code and comment."""

    def __init__(self, request_type: str, status: int | None, comment: str):
        text = comment or _STATUS_TEXT.get(status or 0, "no reason given")
        if status == NOT_READY:
            message = "OBS is busy (changing scene collection or closing); try again in a moment"
        else:
            message = f"OBS refused {request_type}"
        super().__init__("request", message, detail=f"status {status}: {text}", status=status)
        self.request_type, self.comment = request_type, comment


@dataclass(frozen=True, slots=True)
class ObsConfig:
    """Where OBS listens and the password to log in with (never in repr)."""

    host: str
    port: int
    secure: bool = False
    target: str = "/"
    allow_remote: bool = False
    password: str = field(default="", repr=False)

    @property
    def endpoint(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"


def clean(text: Any, limit: int = 200) -> str:
    """``text`` on one line (triggers.one_line) cut to ``limit`` characters, for text from OBS or a user inside a
    message."""
    text = one_line(str(text or ""))
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def setting(name: str, default: str = "") -> str:
    """One Tallyhand OBS setting through Hermes' profile-aware secret route; blank means unset."""
    return str(get_scoped_secret(name, "") or "").strip() or default


def is_loopback(host: str) -> bool:
    """``localhost`` or a loopback address, an IPv4-mapped one included."""
    host = host.strip("[]").split("%", 1)[0]
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (getattr(address, "ipv4_mapped", None) or address).is_loopback


def parse_url(url: str, *, password: str = "", allow_remote: bool = False) -> ObsConfig:
    """ObsConfig for a ws:// or wss:// URL; raises ObsError("config") for one this client will not use. Errors never
    repeat the URL, which could carry credentials, and keep the host out of the chat-safe message."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise ObsError("config", f"{URL_ENV} is not a valid URL") from None
    if parts.scheme.lower() not in ("ws", "wss"):
        raise ObsError("config", f"{URL_ENV} must start with ws:// or wss://")
    if parts.username is not None or parts.password is not None:
        raise ObsError("config", f"put the OBS password in {PASSWORD_ENV}, not in {URL_ENV}")
    host = parts.hostname or ""
    if not _HOST_RE.fullmatch(host):
        raise ObsError("config", f"{URL_ENV} has no valid host")
    if "#" in url:
        raise ObsError("config", f"{URL_ENV} must not have a #fragment")
    if port == 0:
        raise ObsError("config", f"{URL_ENV} has port 0")
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    if not _TARGET_RE.fullmatch(target):
        raise ObsError("config", f"{URL_ENV} has an invalid path")
    if not allow_remote and not is_loopback(host):
        raise ObsError("config", f"{URL_ENV} must point at this computer (127.0.0.1, ::1 or localhost) unless "
                       f"{ALLOW_REMOTE_ENV}=true", detail=f"it points at {host}")
    secure = parts.scheme.lower() == "wss"
    return ObsConfig(host=host, port=port or (443 if secure else 80), secure=secure, target=target,
                     allow_remote=allow_remote, password=password)


def load_config() -> ObsConfig:
    """The endpoint and password from OBS_WEBSOCKET_URL (default ws://127.0.0.1:4455), OBS_WEBSOCKET_PASSWORD and
    TALLYHAND_OBS_ALLOW_REMOTE, read through Hermes. Raises ObsError("config") for a URL this client will not use."""
    return parse_url(setting(URL_ENV, DEFAULT_URL), password=str(get_scoped_secret(PASSWORD_ENV, "") or ""),
                     allow_remote=truthy(get_scoped_secret(ALLOW_REMOTE_ENV, ""), False))


def configured() -> bool:
    """True when the owner set OBS up: OBS_WEBSOCKET_PASSWORD or OBS_WEBSOCKET_URL is set, and the URL is one this
    client will use. The OBS tools and chat commands are offered only then."""
    if not (setting(URL_ENV) or get_scoped_secret(PASSWORD_ENV, "")):
        return False
    try:
        load_config()
    except ObsError:
        return False
    return True


def accept_key(key: str) -> str:
    """Sec-WebSocket-Accept for a Sec-WebSocket-Key (RFC 6455 section 4.2.2)."""
    digest = hashlib.sha1(key.encode("ascii") + _ACCEPT_GUID, usedforsecurity=False).digest()
    return base64.b64encode(digest).decode("ascii")


def auth_string(password: str, salt: str, challenge: str) -> str:
    """The Identify ``authentication`` value: base64(sha256(base64(sha256(password + salt)) + challenge))."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest())
    return base64.b64encode(hashlib.sha256(secret + challenge.encode("utf-8")).digest()).decode("ascii")


def _mask(data: bytes, key: bytes) -> bytes:
    if not data:
        return data
    pad = (key * (len(data) // 4 + 1))[: len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(pad, "big")).to_bytes(len(data), "big")


def _parse_head(head: bytes) -> tuple[int, dict[str, str]]:
    """Status code and lowercased headers of an HTTP response head; status 0 when it is not HTTP/1.x."""
    lines = head.decode("latin-1").split("\r\n")
    version, _, rest = lines[0].partition(" ")
    if not version.startswith("HTTP/1.") or not (rest[:3].isascii() and rest[:3].isdigit()):
        return 0, {}
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            name = name.strip().lower()
            headers[name] = f"{headers[name]}, {value.strip()}" if name in headers else value.strip()
    return int(rest[:3]), headers


@functools.cache
def _tls() -> ssl.SSLContext:
    return ssl.create_default_context()


def _peer_host(writer: asyncio.StreamWriter) -> str:
    peer = writer.get_extra_info("peername")
    return str(peer[0]) if peer else ""


class WebSocket:
    """The client end of one RFC 6455 connection: masked frames out; text messages in, with fragments joined, pings
    answered and a size cap; the close handshake both ways."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer
        self.max_message = MAX_MESSAGE
        self.subprotocol = ""
        self.close_sent = self.close_received = False

    @classmethod
    async def connect(cls, config: ObsConfig) -> WebSocket:
        """TCP (TLS for wss://) plus the HTTP Upgrade, all within CONNECT_TIMEOUT seconds. Nothing is sent before
        the peer is known to be loopback, unless remote hosts are allowed."""
        deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT
        tls = _tls() if config.secure else None
        try:
            async with asyncio.timeout_at(deadline):
                reader, writer = await asyncio.open_connection(config.host, config.port, ssl=tls)
        except TimeoutError:
            raise ObsError("unreachable", UNREACHABLE, detail=f"no answer from {config.endpoint}") from None
        except socket.gaierror:
            raise ObsError("unreachable", "cannot find the OBS host", detail=config.host) from None
        except ssl.SSLError as exc:
            raise ObsError("tls", "the TLS handshake with OBS failed",
                           detail=f"{config.endpoint}: {exc.reason or type(exc).__name__}") from None
        except OSError:
            raise ObsError("unreachable", UNREACHABLE, detail=f"nothing answered at {config.endpoint}") from None
        ws = cls(reader, writer)
        try:
            if not config.allow_remote and not is_loopback(_peer_host(writer)):
                raise ObsError("config", f"{URL_ENV} must point at this computer unless {ALLOW_REMOTE_ENV}=true",
                               detail=f"{config.host} resolved to {_peer_host(writer)}")
            async with asyncio.timeout_at(deadline):
                await ws._upgrade(config)
        except TimeoutError:
            ws.abort()
            raise ObsError("timeout", "OBS did not finish the WebSocket handshake in time",
                           detail=config.endpoint) from None
        except BaseException:
            ws.abort()
            raise
        return ws

    async def _upgrade(self, config: ObsConfig) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self.writer.write(
            f"GET {config.target} HTTP/1.1\r\nHost: {config.endpoint}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: {SUBPROTOCOL}\r\n\r\n"
            .encode("ascii"))
        try:
            await self.writer.drain()
            head = await self.reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            raise ObsError("handshake", NOT_OBS, detail=f"{config.endpoint} hung up during the handshake") from None
        except asyncio.LimitOverrunError:
            raise ObsError("handshake", NOT_OBS, detail=f"{config.endpoint} sent an oversized answer") from None
        except OSError:
            raise ObsError("unreachable", UNREACHABLE, detail=f"{config.endpoint} dropped the connection") from None
        status, headers = _parse_head(head)
        if status != 101:
            raise ObsError("handshake", NOT_OBS, detail=f"HTTP {status} from {config.endpoint}" if status
                           else f"{config.endpoint} did not answer in HTTP")
        connection = {token.strip().lower() for token in headers.get("connection", "").split(",")}
        if headers.get("upgrade", "").lower() != "websocket" or "upgrade" not in connection:
            raise ObsError("handshake", NOT_OBS, detail="the answer is not a WebSocket upgrade")
        if headers.get("sec-websocket-accept") != accept_key(key):
            raise ObsError("handshake", NOT_OBS, detail="wrong Sec-WebSocket-Accept")
        if headers.get("sec-websocket-extensions"):
            raise ObsError("handshake", NOT_OBS, detail="the server forced a WebSocket extension")
        protocol = headers.get("sec-websocket-protocol", "")
        if protocol and protocol != SUBPROTOCOL:
            raise ObsError("handshake", NOT_OBS, detail=f"unexpected subprotocol {clean(protocol, 60)}")
        self.subprotocol = protocol

    async def send_text(self, text: str) -> None:
        await self._send_frame(_TEXT, text.encode("utf-8"))

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        size = len(payload)
        if size < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | size)
        elif size < 1 << 16:
            head = struct.pack("!BBH", 0x80 | opcode, 0xFE, size)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0xFF, size)
        key = os.urandom(4)
        try:
            self.writer.write(head + key + _mask(payload, key))
            await self.writer.drain()
        except (ConnectionError, OSError):
            raise ConnectionClosed(1006) from None

    async def recv(self) -> str:
        """The next text message. Raises ConnectionClosed when OBS closes, ObsError("protocol") on a broken frame."""
        chunks: list[bytes] = []
        size, kind = 0, None
        while True:
            fin, opcode, payload = await self._read_frame(size)
            if opcode >= 0x8:
                await self._control(opcode, payload)
                continue
            if opcode == _CONT and kind is None:
                raise await self.protocol_error(1002, "continuation frame with no message to continue")
            if opcode in (_TEXT, _BINARY):
                if kind is not None:
                    raise await self.protocol_error(1002, "new message inside a fragmented one")
                kind = opcode
            elif opcode != _CONT:
                raise await self.protocol_error(1002, f"unknown opcode {opcode}")
            chunks.append(payload)
            size += len(payload)
            if fin:
                break
        if kind == _BINARY:
            raise await self.protocol_error(1003, "binary message on the JSON subprotocol")
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError:
            raise await self.protocol_error(1007, "text message is not UTF-8") from None

    async def _read_frame(self, buffered: int) -> tuple[bool, int, bytes]:
        try:
            first, second = await self.reader.readexactly(2)
            length = second & 0x7F
            if length == 126:
                (length,) = struct.unpack("!H", await self.reader.readexactly(2))
            elif length == 127:
                (length,) = struct.unpack("!Q", await self.reader.readexactly(8))
            opcode = first & 0x0F
            if first & 0x70:
                raise await self.protocol_error(1002, "reserved bits set")
            if second & 0x80:
                raise await self.protocol_error(1002, "the server masked a frame")
            if opcode >= 0x8 and (length > 125 or not first & 0x80):
                raise await self.protocol_error(1002, "oversized or fragmented control frame")
            if opcode < 0x8 and buffered + length > self.max_message:
                raise await self.protocol_error(1009, f"message over {self.max_message} bytes")
            payload = await self.reader.readexactly(length) if length else b""
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            self.close_received = True
            raise ConnectionClosed(1006) from None
        return bool(first & 0x80), opcode, payload

    async def _control(self, opcode: int, payload: bytes) -> None:
        if opcode == _PING:
            await self._send_frame(_PONG, payload)
        elif opcode == _CLOSE:
            if len(payload) == 1:
                raise await self.protocol_error(1002, "one-byte close frame")
            code = struct.unpack("!H", payload[:2])[0] if payload else 1005
            self.close_received = True
            if not self.close_sent:
                self.close_sent = True
                with contextlib.suppress(ConnectionClosed):
                    await self._send_frame(_CLOSE, payload[:2])
            self.writer.close()
            raise ConnectionClosed(code, payload[2:].decode("utf-8", "replace"))
        elif opcode != _PONG:
            raise await self.protocol_error(1002, f"unknown control opcode {opcode}")

    async def protocol_error(self, code: int, why: str) -> ObsError:
        """Send a close frame with ``code``, end the connection and return the error for the caller to raise."""
        if not self.close_sent:
            self.close_sent = True
            with contextlib.suppress(ConnectionClosed):
                await self._send_frame(_CLOSE, struct.pack("!H", code))
        self.writer.close()
        if code == 1009:
            return ObsError("protocol", "OBS sent a message larger than this client accepts", detail=why)
        return ObsError("protocol", "OBS sent a malformed WebSocket message", detail=why)

    async def close(self, code: int = 1000) -> None:
        """Close handshake: our close frame, then up to a second for OBS's, then the connection goes."""
        if not self.close_sent:
            self.close_sent = True
            with contextlib.suppress(ConnectionClosed):
                await self._send_frame(_CLOSE, struct.pack("!H", code))
        if not self.close_received and not self.writer.is_closing():
            with contextlib.suppress(TimeoutError, ObsError):
                async with asyncio.timeout(CLOSE_TIMEOUT):
                    while True:
                        await self.recv()
        self.writer.close()
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT):
                await self.writer.wait_closed()
        except (TimeoutError, OSError):
            self.abort()

    def abort(self) -> None:
        self.writer.transport.abort()


class ObsClient:
    """One identified obs-websocket session (no event subscriptions). Use ``async with session() as obs``."""

    def __init__(self, ws: WebSocket, config: ObsConfig):
        self.ws, self.config = ws, config
        self._secrets: tuple[str, ...] = (config.password,) if config.password else ()

    @classmethod
    async def open(cls, config: ObsConfig) -> ObsClient:
        client = cls(await WebSocket.connect(config), config)
        try:
            await client._identify()
        except BaseException:
            if not client.ws.writer.is_closing():
                client.ws.abort()
            raise
        logger.debug("OBS: identified at %s", config.endpoint)
        return client

    async def close(self) -> None:
        await self.ws.close()

    def scrub(self, text: Any, limit: int = 200) -> str:
        """Text from OBS made fit for a message: one line, cut, the password and auth string blanked out."""
        text = str(text or "")
        for secret in self._secrets:
            text = text.replace(secret, "***")
        text = clean(text, limit)
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def _closed(self, exc: ConnectionClosed) -> ObsError:
        code, message = _CLOSE_ERRORS.get(exc.close_code, ("closed", "OBS closed the connection"))
        reason = self.scrub(exc.reason, 120)
        return ObsError(code, message, detail=f"close code {exc.close_code}" + (f": {reason}" if reason else ""))

    async def _identify(self) -> None:
        try:
            async with asyncio.timeout(IDENTIFY_TIMEOUT):
                hello = await self._expect(HELLO)
                identify: dict[str, Any] = {"rpcVersion": RPC_VERSION, "eventSubscriptions": 0}
                if (challenge := hello.get("authentication")) is not None:
                    if not self.config.password:
                        raise ObsError("auth", f"OBS asks for a password; set {PASSWORD_ENV}")
                    if not (isinstance(challenge, dict) and isinstance(challenge.get("salt"), str)
                            and isinstance(challenge.get("challenge"), str)):
                        raise await self.ws.protocol_error(1002, "Hello has a malformed authentication object")
                    auth = auth_string(self.config.password, challenge["salt"], challenge["challenge"])
                    self._secrets += (auth,)
                    identify["authentication"] = auth
                await self._send(IDENTIFY, identify)
                identified = await self._expect(IDENTIFIED)
        except TimeoutError:
            raise ObsError("timeout", "OBS did not finish the WebSocket login in time", detail=(
                f"{IDENTIFY_TIMEOUT:g} s at {self.config.endpoint}; OBS 28 or later is needed")) from None
        except ConnectionClosed as exc:
            raise self._closed(exc) from None
        if identified.get("negotiatedRpcVersion") != RPC_VERSION:
            raise await self.ws.protocol_error(1002, f"OBS negotiated RPC version "
                                                     f"{self.scrub(identified.get('negotiatedRpcVersion'), 20)}")

    async def _send(self, op: int, data: dict) -> None:
        await self.ws.send_text(json.dumps({"op": op, "d": data}, ensure_ascii=False))

    async def _expect(self, op: int) -> dict:
        """The ``d`` of the next message with opcode ``op``; events in between are skipped."""
        while True:
            try:
                message = json.loads(await self.ws.recv())
            except (ValueError, RecursionError):  # RecursionError: nesting deeper than the parser goes
                raise await self.ws.protocol_error(1002, "message is not JSON") from None
            got = message.get("op") if isinstance(message, dict) else None
            if type(got) is not int or not isinstance(message.get("d"), dict):
                raise await self.ws.protocol_error(1002, "message without an op and d object")
            if got == op:
                return message["d"]
            if got != EVENT:
                raise await self.ws.protocol_error(1002, f"opcode {got} while waiting for {op}")

    async def call(self, request_type: str, data: dict | None = None) -> dict:
        """Send one request and return its responseData; RequestFailed when OBS reports a failure."""
        request_id = str(uuid.uuid4())
        body: dict[str, Any] = {"requestType": request_type, "requestId": request_id}
        if data is not None:
            body["requestData"] = data
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                await self._send(REQUEST, body)
                while (response := await self._expect(REQUEST_RESPONSE)).get("requestId") != request_id:
                    pass
        except TimeoutError:
            raise ObsError("timeout", "OBS did not answer in time",
                           detail=f"{request_type} after {REQUEST_TIMEOUT:g} s") from None
        except ConnectionClosed as exc:
            raise self._closed(exc) from None
        status = response.get("requestStatus")
        if not isinstance(status, dict):
            raise await self.ws.protocol_error(1002, f"{request_type} response has no requestStatus")
        if status.get("result") is not True:
            code = status.get("code")
            raise RequestFailed(request_type, code if type(code) is int else None, self.scrub(status.get("comment")))
        result = response.get("responseData")
        return result if isinstance(result, dict) else {}


@contextlib.asynccontextmanager
async def session(config: ObsConfig | None = None) -> AsyncIterator[ObsClient]:
    """An identified session for the length of the block, closed with the close handshake afterwards."""
    client = await ObsClient.open(config or load_config())
    try:
        yield client
    finally:
        await client.close()


def match(wanted: str, names: list[str]) -> str | None:
    """``wanted`` as OBS spells it: the exact name, else the only case-insensitive match, else None."""
    if wanted in names:
        return wanted
    folded = {name for name in names if name.casefold() == wanted.casefold()}
    return folded.pop() if len(folded) == 1 else None


def _names(items: Any, key: str) -> list[str]:
    return [item[key] for item in items or () if isinstance(item, dict) and isinstance(item.get(key), str)]


def _count(value: Any) -> int:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0


def _number(value: Any, digits: int = 1) -> float | None:
    return round(float(value), digits) if isinstance(value, int | float) and not isinstance(value, bool) else None


async def scenes(obs: ObsClient) -> tuple[str, list[str]]:
    """The program scene and every scene name, top to bottom as in OBS's scene list."""
    data = await obs.call("GetSceneList")
    items = [item for item in data.get("scenes") or () if isinstance(item, dict)
             and isinstance(item.get("sceneName"), str)]
    # obs-websocket lists the bottom scene first; sceneIndex counts up from the bottom.
    items.sort(key=lambda item: _count(item.get("sceneIndex")), reverse=True)
    return str(data.get("currentProgramSceneName") or ""), [item["sceneName"] for item in items]


async def switch_scene(obs: ObsClient, name: str) -> tuple[str, str]:
    """Make ``name`` the program scene; returns (previous, new) as OBS spells them. Nothing changes when it is
    already live."""
    current, names = await scenes(obs)
    scene = match(name, names)
    if scene is None:
        raise ObsError("not_found", f'scene "{clean(name, 100)}" does not exist')
    if scene != current:
        try:
            await obs.call("SetCurrentProgramScene", {"sceneName": scene})
        except RequestFailed as exc:
            if exc.status in (RESOURCE_NOT_FOUND, INVALID_RESOURCE_TYPE):
                raise ObsError("not_found", f'scene "{clean(name, 100)}" does not exist') from None
            raise
    return current, scene


async def status(obs: ObsClient) -> dict:
    """Program scene, stream and recording state, fps, CPU use and dropped frames. Read-only."""
    scene = await obs.call("GetCurrentProgramScene")
    stream = await obs.call("GetStreamStatus")
    record = await obs.call("GetRecordStatus")
    stats = await obs.call("GetStats")
    result: dict[str, Any] = {
        # sceneName arrived in obs-websocket 5.5; older releases only send the currentProgram* fields.
        "live_scene": str(scene.get("sceneName") or scene.get("currentProgramSceneName") or ""),
        "streaming": stream.get("outputActive") is True, "reconnecting": stream.get("outputReconnecting") is True,
        "recording": record.get("outputActive") is True, "recording_paused": record.get("outputPaused") is True,
        "fps": _number(stats.get("activeFps")), "cpu_percent": _number(stats.get("cpuUsage")),
        "dropped_frames": {
            "network": {"dropped": _count(stream.get("outputSkippedFrames")),
                        "total": _count(stream.get("outputTotalFrames"))},
            "encoding": {"dropped": _count(stats.get("outputSkippedFrames")),
                         "total": _count(stats.get("outputTotalFrames"))},
            "rendering": {"dropped": _count(stats.get("renderSkippedFrames")),
                          "total": _count(stats.get("renderTotalFrames"))},
        },
    }
    if result["streaming"]:
        result["stream_time"] = clean(stream.get("outputTimecode"), 32)
    if result["recording"]:
        result["record_time"] = clean(record.get("outputTimecode"), 32)
    return result


async def audio_inputs(obs: ObsClient) -> list[dict]:
    """Every input that carries audio, with its mute state, in OBS's order."""
    found = []
    for item in (await obs.call("GetInputList")).get("inputs") or ():
        if not isinstance(item, dict) or not isinstance(name := item.get("inputName"), str):
            continue
        caps = item.get("inputKindCaps")
        if type(caps) is int and not caps & OBS_SOURCE_AUDIO:
            continue
        try:
            muted = (await obs.call("GetInputMute", {"inputName": name})).get("inputMuted")
        except RequestFailed as exc:
            if exc.status in (INVALID_RESOURCE_STATE, RESOURCE_NOT_FOUND):  # no audio, or removed meanwhile
                continue
            raise
        found.append({"name": name, "muted": muted is True})
    return found


async def set_mute(obs: ObsClient, name: str, muted: bool | None) -> tuple[str, bool]:
    """Mute (True), unmute (False) or toggle (None) an audio input; returns its OBS name and new state."""
    input_name = match(name, _names((await obs.call("GetInputList")).get("inputs"), "inputName"))
    if input_name is None:
        raise ObsError("not_found", f'input "{clean(name, 100)}" does not exist')
    try:
        if muted is None:
            muted = (await obs.call("ToggleInputMute", {"inputName": input_name})).get("inputMuted") is True
        else:
            await obs.call("SetInputMute", {"inputName": input_name, "inputMuted": muted})
    except RequestFailed as exc:
        if exc.status == INVALID_RESOURCE_STATE:
            raise ObsError("no_audio", f'input "{input_name}" has no audio') from None
        if exc.status in (RESOURCE_NOT_FOUND, INVALID_RESOURCE_TYPE):
            raise ObsError("not_found", f'input "{input_name}" does not exist') from None
        raise
    return input_name, muted


async def text_source(obs: ObsClient, source: str) -> dict:
    """The settings of text source ``source``; ObsError when it is missing or not a Text source."""
    try:
        data = await obs.call("GetInputSettings", {"inputName": source})
    except RequestFailed as exc:
        if exc.status in (RESOURCE_NOT_FOUND, INVALID_RESOURCE_TYPE):
            raise ObsError("not_found", f'the caption source "{clean(source, 100)}" does not exist; add a Text '
                           f'source with that name in OBS or set {CAPTION_SOURCE_ENV}') from None
        raise
    kind = str(data.get("inputKind") or "")
    if not kind.startswith("text_"):  # text_gdiplus* on Windows, text_ft2_source* on macOS and Linux
        raise ObsError("not_text", f'the caption source "{clean(source, 100)}" is not a Text source',
                       detail=f"its kind is {clean(kind, 60)}")
    settings = data.get("inputSettings")
    return settings if isinstance(settings, dict) else {}


async def set_text(obs: ObsClient, source: str, text: str) -> None:
    """Put ``text`` in text source ``source`` (an empty string clears it)."""
    await text_source(obs, source)
    await obs.call("SetInputSettings", {"inputName": source, "inputSettings": {"text": text}, "overlay": True})


async def clear_text_if(obs: ObsClient, source: str, text: str) -> bool:
    """Clear ``source`` only while it still shows ``text``, so a newer caption stays up; True when cleared."""
    if (await text_source(obs, source)).get("text") != text:
        return False
    await obs.call("SetInputSettings", {"inputName": source, "inputSettings": {"text": ""}, "overlay": True})
    return True


async def set_source_visible(obs: ObsClient, scene: str, source: str, visible: bool) -> tuple[str, str]:
    """Show or hide every item of ``source`` in ``scene``; returns both names as OBS spells them."""
    scene_name = match(scene, (await scenes(obs))[1])
    if scene_name is None:
        raise ObsError("not_found", f'scene "{clean(scene, 100)}" does not exist')
    try:
        items = (await obs.call("GetSceneItemList", {"sceneName": scene_name})).get("sceneItems") or []
    except RequestFailed as exc:
        if exc.status in (RESOURCE_NOT_FOUND, INVALID_RESOURCE_TYPE):
            raise ObsError("not_found", f'scene "{scene_name}" does not exist') from None
        raise
    source_name = match(source, _names(items, "sourceName"))
    if source_name is None:
        raise ObsError("not_found", f'source "{clean(source, 100)}" is not in scene "{scene_name}"')
    for item in items:
        if isinstance(item, dict) and item.get("sourceName") == source_name and type(item.get("sceneItemId")) is int:
            await obs.call("SetSceneItemEnabled", {"sceneName": scene_name, "sceneItemId": item["sceneItemId"],
                                                   "sceneItemEnabled": visible})
    return scene_name, source_name


async def save_replay(obs: ObsClient) -> None:
    """Save the replay buffer; ObsError when it is off in OBS's settings or not running."""
    try:
        active = (await obs.call("GetReplayBufferStatus")).get("outputActive")
    except RequestFailed as exc:
        if exc.status == INVALID_RESOURCE_STATE:
            raise ObsError("replay_off", REPLAY_OFF) from None
        raise
    if active is not True:
        raise ObsError("replay_stopped", REPLAY_STOPPED)
    try:
        await obs.call("SaveReplayBuffer")
    except RequestFailed as exc:
        if exc.status == OUTPUT_NOT_RUNNING:
            raise ObsError("replay_stopped", REPLAY_STOPPED) from None
        raise
