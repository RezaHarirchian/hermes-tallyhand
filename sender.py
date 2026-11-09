"""Posting to Twitch chat without a running adapter, for cron jobs and other deliveries made in a process where the
gateway's Twitch adapter does not run: one short-lived connection that validates the token, logs in, joins the
channel, sends the lines at Twitch's pace, parts and closes."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from gateway.platforms._shared import extra_or_secret, get_scoped_secret, send_error

from . import auth, irc

logger = logging.getLogger(__name__)

HOST, PORT = "irc.chat.twitch.tv", 6697
STEP_TIMEOUT = 10.0  # seconds for each of connect, login, join and every line's confirmation
# Twitch allows a bot 20 messages per 30 s where it is not broadcaster, moderator or VIP: one reply or delivery never
# takes more lines than that.
MAX_LINES = 20
ELEVATED_BADGES = ("broadcaster", "moderator", "vip")
AUTH_FAILURES = ("login authentication failed", "improperly formatted auth")
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def is_elevated(badges: Mapping[str, str], tags: Mapping[str, str]) -> bool:
    """The bot is broadcaster, moderator or VIP where these USERSTATE badges and tags were sent."""
    return any(badge in badges for badge in ELEVATED_BADGES) or tags.get("mod") == "1"


def ssl_context(tls: bool) -> ssl.SSLContext | None:
    return ssl.create_default_context() if tls else None


def read_token(config: Any) -> str:
    """The bot token from TWITCH_BOT_TOKEN (or the platform extra), ``oauth:`` prefix removed."""
    extra = getattr(config, "extra", None) or {}
    return auth.strip_oauth_prefix(extra_or_secret(extra, "token", "TWITCH_BOT_TOKEN") or getattr(config, "token", ""))


def allowed_target(chat_id: str | None) -> str:
    """The channel a delivery may go to, ``#login``: TWITCH_HOME_CHANNEL, or a channel listed in TWITCH_CHANNEL. Empty
    for anything else, so a cron job or a tool call can never make the bot post in a channel it does not serve."""
    channel = irc.normalize_channel(str(chat_id or ""))
    home = irc.normalize_channel(str(get_scoped_secret("TWITCH_HOME_CHANNEL", "") or ""))
    if channel and (channel == home or channel in irc.parse_channels(get_scoped_secret("TWITCH_CHANNEL", "") or "")):
        return channel
    return ""


class SendError(Exception):
    """A failed one-shot delivery; ``message`` never carries the token."""

    def __init__(self, message: str, *, sent: int = 0, retryable: bool = False):
        super().__init__(message)
        self.message, self.sent, self.retryable = message, sent, retryable


@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str = HOST
    port: int = PORT
    tls: bool = True
    validate_url: str = auth.VALIDATE_URL


def chat_endpoint(extra: Mapping) -> Endpoint:
    """Chat host, port, TLS flag and validate URL. The ``_test_*`` keys exist for the local test fakes and are
    honoured only for loopback hosts, so a config edit can never send the token anywhere but Twitch."""
    host, port, tls = HOST, PORT, True
    if (test_host := str(extra.get("_test_host") or "")) and test_host in _LOOPBACK:
        host, port, tls = test_host, int(extra.get("_test_port") or PORT), bool(extra.get("_test_tls", True))
    elif test_host:
        logger.warning("Twitch: ignoring _test_host %r; only loopback hosts are allowed", test_host)
    return Endpoint(host, port, tls, loopback_url(extra, "_test_validate_url", auth.VALIDATE_URL))


def loopback_url(extra: Mapping, key: str, default: str) -> str:
    """``extra[key]`` when it names a loopback host (a test fake), else ``default``."""
    test_url = str(extra.get(key) or "")
    if test_url and urllib.parse.urlsplit(test_url).hostname in _LOOPBACK:
        return test_url
    if test_url:
        logger.warning("Twitch: ignoring %s; only loopback hosts are allowed", key)
    return default


class _Session:
    """The client end of one chat connection, just enough of the protocol for a one-shot delivery."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer

    async def send(self, line: str) -> None:
        self.writer.write((irc.clean_line(line) + "\r\n").encode("utf-8"))
        await self.writer.drain()

    async def until(self, wanted, what: str) -> irc.Line:
        """Read lines (answering PINGs) until ``wanted(line)`` is true; raise SendError on a refusal or timeout."""
        try:
            async with asyncio.timeout(STEP_TIMEOUT):
                while raw := await self.reader.readline():
                    line = irc.parse_line(raw.decode("utf-8", errors="replace"))
                    if line is None:
                        continue
                    if line.command == "PING":
                        await self.send(f"PONG :{line.param(0, 'tmi.twitch.tv')}")
                    elif line.command == "NOTICE" and (refusal := _refusal(line)):
                        raise SendError(refusal)
                    elif line.command == "RECONNECT":
                        raise SendError("Twitch chat asked to reconnect", retryable=True)
                    elif wanted(line):
                        return line
        except TimeoutError:
            raise SendError(f"Twitch chat did not confirm the {what} in time", retryable=True) from None
        raise SendError(f"Twitch chat closed the connection during the {what}", retryable=True)

    async def close(self) -> None:
        with contextlib.suppress(OSError, RuntimeError):
            self.writer.close()
            try:
                await asyncio.wait_for(self.writer.wait_closed(), timeout=5.0)
            except TimeoutError:
                self.writer.transport.abort()


def _refusal(line: irc.Line) -> str:
    text, msg_id = line.param(-1).strip(), line.tags.get("msg-id", "")
    if not msg_id and text.lower() in AUTH_FAILURES:
        return f"Twitch chat rejected the login: {auth.INVALID_TOKEN}"
    if msg_id.startswith("msg_") or msg_id == "tos_ban":
        return f"Twitch did not deliver the message ({msg_id})"
    return ""


TWITCH = Endpoint()


async def post(token: str, channel: str, text: str, *, max_lines: int = 3, endpoint: Endpoint = TWITCH) -> list[str]:
    """Deliver ``text`` to ``channel`` (``#login``) as at most ``max_lines`` chat lines (one in slow mode unless the
    bot is broadcaster, moderator or VIP there, as the adapter does) on a connection of its own. Returns the message
    ids Twitch gave the lines. Raises SendError; a failure after the first line says how many went out."""
    if not irc.format_reply(text, max_lines=1):
        raise SendError("nothing to send after formatting")
    try:
        info = await auth.validate_token(token, url=endpoint.validate_url)
    except auth.AuthError as exc:
        raise SendError(exc.message, retryable=exc.retryable) from None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(
            endpoint.host, endpoint.port, ssl=ssl_context(endpoint.tls)), timeout=STEP_TIMEOUT)
    except (TimeoutError, OSError) as exc:
        raise SendError(f"could not reach Twitch chat ({type(exc).__name__})", retryable=True) from None
    session = _Session(reader, writer)
    sent: list[str] = []
    try:
        await session.send("CAP REQ :twitch.tv/commands twitch.tv/tags")
        await session.send(f"PASS oauth:{auth.strip_oauth_prefix(token)}")
        await session.send(f"NICK {info.login}")
        await session.until(lambda line: line.command == "001", "login")
        await session.send(f"JOIN {channel}")
        seen: dict[str, irc.Line] = {}

        def joined(line: irc.Line) -> bool:  # USERSTATE (the bot's badges) comes before ROOMSTATE (slow mode)
            if line.command in ("USERSTATE", "ROOMSTATE") and line.param(0).lower() == channel:
                seen[line.command] = line
            return line.command == "ROOMSTATE" and line.param(0).lower() == channel

        await session.until(joined, "join")
        own = seen.get("USERSTATE")
        badges = irc.parse_badges(own.tags.get("badges") if own else "")
        elevated = is_elevated(badges, own.tags if own else {})
        slow = seen["ROOMSTATE"].tags.get("slow", "0") not in ("", "0")
        token = auth.strip_oauth_prefix(token)
        lines = [auth.redact(line, token) for line in irc.format_reply(
            auth.redact(text, token), max_lines=1 if slow and not elevated else min(max_lines, MAX_LINES))]
        if not lines:
            raise SendError("nothing to send after formatting")
        for index, line in enumerate(lines):
            if index and not elevated:
                await asyncio.sleep(1.05)  # one message a second where the bot is not broadcaster, moderator or VIP
            await session.send(irc.privmsg(channel, line))
            echo = await session.until(lambda line: line.command == "USERSTATE" and line.param(0).lower() == channel,
                                       "message")
            sent.append(echo.tags.get("id", ""))
        await session.send(f"PART {channel}")
    except SendError as exc:
        exc.sent = len(sent)
        raise
    except (OSError, ConnectionError) as exc:
        raise SendError(f"Twitch chat closed the connection ({type(exc).__name__})", sent=len(sent),
                        retryable=True) from None
    finally:
        await session.close()
    return sent


# Hermes' hooks for deliveries

TOTAL_TIMEOUT = 45.0  # under cron's default 60 s budget for a standalone send (cron.standalone_send_timeout_seconds)
NOT_SERVED = "not a channel this bot serves; use TWITCH_HOME_CHANNEL or a channel in TWITCH_CHANNEL"


def parse_target_ref(target_ref: str) -> tuple[str, None] | None:
    """PlatformEntry.parse_target_ref_fn: ``#login`` or ``login`` -> ("#login", None); None for anything else, which
    Hermes then refuses (a plugin parser keeps even cron targets strict)."""
    channel = irc.normalize_channel(str(target_ref or "").strip())
    return (channel, None) if channel else None


def validate_target_ref(chat_id: str) -> bool | str:
    """PlatformEntry.validate_target_ref_fn: True for a channel the bot serves, else the reason."""
    return True if allowed_target(chat_id) else NOT_SERVED


async def send_standalone(pconfig: Any, chat_id: str, message: str, *, thread_id: str | None = None,
                          media_files: Any = None, force_document: bool = False, max_lines: int = 3,
                          **_: Any) -> dict:
    """PlatformEntry.standalone_sender_fn: Hermes calls it when no Twitch adapter runs in the process (``hermes send``,
    a cron job fired outside the gateway). Posts the text only, to TWITCH_HOME_CHANNEL or a channel in
    TWITCH_CHANNEL, on a connection of its own. Returns {"success": True, "message_id": ...} or an error envelope."""
    channel = allowed_target(chat_id)
    if not channel:
        return send_error(f"{str(chat_id or '').strip()!r} is {NOT_SERVED}")
    token = read_token(pconfig)
    if not token:
        return send_error("TWITCH_BOT_TOKEN is not set")
    try:
        ids = await asyncio.wait_for(post(token, channel, message, max_lines=max_lines,
                                          endpoint=chat_endpoint(getattr(pconfig, "extra", None) or {})), TOTAL_TIMEOUT)
    except TimeoutError:
        return send_error(f"Twitch chat did not take the message within {TOTAL_TIMEOUT:.0f} s")
    except SendError as exc:
        done = f" after {exc.sent} line{'s' if exc.sent != 1 else ''} went out" if exc.sent else ""
        return send_error(f"{exc.message}{done}")
    return {"success": True, "message_id": ids[0] if ids else ""}
