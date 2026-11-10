"""Twitch chat adapter: one TLS IRC connection to Twitch chat, trigger detection, a per-channel chat tail and
rate-limited plain-text replies. Stdlib asyncio only."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import random
import re
import sqlite3
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from gateway.config import Platform
from gateway.platforms._shared import (
    decode_json_list_literal,
    extra_or_secret,
    get_scoped_secret,
    platform_gate_env,
    seed_extra_from_env,
)
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome
from gateway.platforms.helpers import MessageDeduplicator, cancel_task

from . import auth, commands, events, irc, language, obs, obs_tools, privacy, roles, sender, twitch_tools, viewer_notes
from .autoclip import AutoClip
from .helix import BASE_URL as HELIX_URL
from .helix import CHANNEL_TTL, Helix, HelixError, login_of
from .obs_tools import OBS_TOOLSET
from .roles import RoleRegistry
from .triggers import (
    EMPTY_QUESTION_REPLY,
    ChatTail,
    Cooldowns,
    TriggerMatcher,
    TurnGate,
    defuse_references,
    one_line,
    scrub_name,
    scrub_untrusted,
)

logger = logging.getLogger(__name__)

CAPABILITIES = "twitch.tv/commands twitch.tv/tags twitch.tv/membership"
TIMEOUT = 30.0
CLOSE_GRACE = 1.0  # seconds a closing connection waits for the peer's side of the close
JOIN_LIMIT, JOIN_PERIOD = 20, 10.0
# Twitch counts per bot account: 20 messages per 30 s, 100 where the bot is broadcaster, moderator or VIP, and
# at most one message per second in a channel where it is none of those. The windows here are a little longer, so
# a line delayed on the way still lands inside Twitch's limits.
SEND_LIMIT, SEND_LIMIT_ELEVATED, SEND_PERIOD = 20, 100, 31.0
CHANNEL_LIMIT, CHANNEL_PERIOD = 1, 1.05
SLOW_MARGIN = 0.5  # seconds added to a channel's slow-mode interval
# The plugin's own lines (usage hints, command answers, thank-you lines, clip links) wait at most OWN_WAIT seconds
# and never take the last OWN_RESERVE messages of an account-wide window, which stay for Hermes' replies.
OWN_WAIT, OWN_RESERVE = 3.0, 5
HINT_COOLDOWN = 60.0
# How often the refusal of questions from anyone but the owner (profile memory on) is logged; the key is the log's own.
BARRED_LOG_PERIOD = 600.0
BARRED_KEY = ("", "")
DELIVERED_LINES = 200  # the bot's own recent lines kept per channel, to spot a redelivered reply
APPROVAL_LINES = 3
ALLOWED_ENV = "TWITCH_ALLOWED_USERS"
ALLOWLIST_TIMEOUT = 10.0  # seconds connect waits for Twitch to resolve the logins in TWITCH_ALLOWED_USERS
RECENT_COMMANDS = 50
# Hermes' English answers to an approval or a command's confirmation (locales/en.yaml gateway inputs, 0.22.7 through
# main). The reply to one of these from the owner is a command's result, never an answer to caption.
ANSWER_WORDS = frozenset({
    "approve", "yes", "ok", "okay", "confirm", "y", "deny", "no", "reject", "cancel", "n", "always",
    "approve always", "always approve", "session", "approve session", "session approve", "approve once", "once",
    "remember", "nevermind",
})
MAX_JOIN_LINE = 500
# NOTICE msg-ids that mean a configured channel cannot be used; logged as warnings, never fatal.
_CHANNEL_NOTICES = frozenset({"msg_channel_suspended", "msg_channel_blocked", "tos_ban", "msg_banned"})
# NOTICE msg-ids that mean one of our messages was not delivered.
_SEND_NOTICES = frozenset({
    "msg_ratelimit", "msg_duplicate", "msg_timedout", "msg_requires_verified_phone_number", "msg_verified_email",
    "msg_followersonly", "msg_followersonly_followed", "msg_followersonly_zero", "msg_subsonly", "msg_emoteonly",
    "msg_slowmode", "msg_r9k", "msg_rejected", "msg_rejected_mandatory", "msg_bad_characters", "msg_suspended",
})
# Toolsets per role. Viewers: "todo" (Hermes ignores an empty list) and "no_mcp" (no global MCP servers), so no files,
# terminal, code, browser, desktop, memory or past sessions. Moderators add twitch, which the hook narrows; obs is the
# owner's alone. Hermes adds every plugin toolset unless known_plugin_toolsets.twitch names it; the hook refuses those.
OWNER_TOOLSETS = (twitch_tools.TOOLSET, OBS_TOOLSET)
MODERATOR_TOOLSETS = ("todo", "no_mcp", twitch_tools.TOOLSET)
VIEWER_TOOLSETS = ("todo", "no_mcp")
# Hermes' delivery ledger resends a whole final reply after a failed or partial delivery, prefixed with a
# "Recovered reply" line (gateway/delivery_ledger.py RECOVERED_MARKER, RECONNECTED_MARKER, FLOOD_MARKER; sent by
# gateway/run_startup.py _redeliver_claimed_obligations). Lines chat already shows are skipped instead.
_REDELIVERY_RE = re.compile(r"\A\s*(?:\u267b\ufe0f?\s*)?Recovered reply\b[^\n]*\n\s*")
# The ledger retries 30 s after a failed delivery, then 120 s after a failed retry, each plus 2 s of timer slack
# (delivery_ledger.py _RETRY_BACKOFF_SECONDS), and a send can spend up to about 100 s waiting for rate-limit
# tokens. A line skipped or sent on a retry counts as seen again. Only marked redeliveries read this memory.
DUPLICATE_WINDOW = 300.0


class _SessionError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


@dataclass(slots=True)
class _Turn:
    """Signals from Hermes' processing hooks for one dispatched question."""

    user_id: str
    started: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None


class RateLimiter:
    """At most ``limit`` uses in any ``period`` seconds: a token comes back ``period`` seconds after its use.

    Twitch counts a sliding window, which a refilling bucket would overrun by up to ``limit`` messages."""

    def __init__(self, limit: int, period: float, clock: Callable[[], float] = time.monotonic):
        self.limit, self.period, self.clock = limit, period, clock
        self._used: deque[float] = deque(maxlen=limit)

    def delay(self, reserve: int = 0) -> float:
        """Seconds until a token is free with ``reserve`` tokens left over; 0 when one is free now."""
        now, cap = self.clock(), max(1, self.limit - reserve)
        while self._used and now - self._used[0] >= self.period:
            self._used.popleft()
        return 0.0 if len(self._used) < cap else self._used[len(self._used) - cap] + self.period - now

    def use(self) -> None:
        """Count one use now, free token or not."""
        self._used.append(self.clock())

    def take(self) -> bool:
        if self.delay() > 0:
            return False
        self.use()
        return True

    async def acquire(self, timeout: float, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> bool:
        return await acquire_all((self,), timeout, sleep)


async def acquire_all(limiters: tuple[RateLimiter, ...], timeout: float,
                      sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, *, reserve: int = 0) -> bool:
    """Take one token from every limiter together, waiting at most ``timeout`` seconds; False when they do not
    all free up in time. ``reserve`` tokens of each limiter stay untouched. The limiters must share a clock."""
    clock = limiters[0].clock
    deadline = clock() + timeout
    while (wait := max(limiter.delay(reserve) for limiter in limiters)) > 0:
        if clock() + wait > deadline:
            return False
        await sleep(wait)
    for limiter in limiters:
        limiter.use()
    return True


def backoff_delay(attempt: int, *, base: float = 1.0, cap: float = 60.0, jitter: float = 0.2,
                  rand: Callable[[], float] = random.random) -> float:
    """Reconnect delay: ``base * 2**attempt`` capped at ``cap``, with +/- ``jitter`` spread, never above ``cap``."""
    delay = min(cap, base * (2 ** min(attempt, 30)))
    return min(cap, delay * (1 + jitter * (2 * rand() - 1)))


def _raw(name: str, settings: dict, extra: dict) -> Any:
    """A setting's raw value: the plugin settings beat the platform extra."""
    return settings.get(name) if settings.get(name) is not None else extra.get(name)


def _bool_option(name: str, default: bool, settings: dict, extra: dict) -> bool:
    """Boolean option: the plugin settings beat the platform extra; true/false, yes/no, on/off or 1/0."""
    raw = _raw(name, settings, extra)
    if raw is None or isinstance(raw, bool):
        return default if raw is None else raw
    text = str(raw).strip().lower()
    if text in privacy.TRUE_WORDS:
        return True
    if text in privacy.FALSE_WORDS:
        return False
    logger.warning("Twitch: ignoring %s=%r, not true or false", name, raw)
    return default


def _whole(raw: Any) -> int:
    """A whole number from a setting: an int, or a float or text such as "20" or 20.0 (Hermes hands plugin settings
    over uncoerced, and its settings form sends numbers as floats). Raises ValueError otherwise."""
    if isinstance(raw, bool):
        raise ValueError(raw)
    text = str(raw).strip()
    try:
        return int(text)
    except ValueError:
        number = float(text)
    if not number.is_integer():
        raise ValueError(raw)
    return int(number)


def _int_option(name: str, default: int, settings: dict, extra: dict, env: str = "", minimum: int = 0,
                maximum: int | None = None) -> int:
    """Integer option: env (when given) beats the plugin settings, which beat the platform extra."""
    raw: Any = get_scoped_secret(env) if env else None
    if raw is None or not str(raw).strip():
        raw = _raw(name, settings, extra)
    try:
        value = max(minimum, _whole(raw)) if raw is not None and str(raw).strip() else default
        return value if maximum is None else min(maximum, value)
    except (ValueError, OverflowError):
        logger.warning("Twitch: ignoring non-integer %s=%r", env or name, raw)
        return default


def _mode_option(name: str, default: str, settings: dict, extra: dict) -> str:
    """The event_messages mode: the plugin settings beat the platform extra; "off" or "thanks"."""
    raw = _raw(name, settings, extra)
    if raw is None:
        return default
    if (mode := events.parse_mode(raw)) is None:
        logger.warning("Twitch: ignoring %s=%r, not one of %s", name, raw, ", ".join(events.MODES))
        return default
    return mode


def _reply_language_option(settings: dict, extra: dict) -> str:
    """Reply language: TALLYHAND_REPLY_LANGUAGE beats the plugin setting; channel, viewer, or an ISO 639-1 code."""
    raw: Any = get_scoped_secret("TALLYHAND_REPLY_LANGUAGE")
    if raw is None or not str(raw).strip():
        raw = _raw("reply_language", settings, extra)
    if raw is None or not str(raw).strip():
        return language.CHANNEL
    if (value := language.parse_reply_language(raw)) is not None:
        return value
    logger.warning("Twitch: ignoring reply_language=%r, not channel, viewer, or a two-letter language code", raw)
    return language.CHANNEL


def allowlist_logins(raw: str) -> list[str]:
    """The Twitch logins in a TWITCH_ALLOWED_USERS value, lowercased and without a leading @. Read the way Hermes
    reads the list (a JSON list, else comma-separated, entries stripped: gateway/authz_mixin.py _coerce_allow_set);
    numeric ids and "*" are left to Hermes, which compares entries as written."""
    value = decode_json_list_literal(raw)
    items = value if isinstance(value, list) else str(value or "").split(",")
    entries = [entry for item in items if (entry := str(item).strip())]
    return list(dict.fromkeys(login for entry in entries if entry != "*"
                              and (login := login_of(entry)) and not login.isdigit()))


def _sent() -> SendResult:
    """A delivered send, with the send time in milliseconds as its id."""
    return SendResult(success=True, message_id=str(int(time.time() * 1000)))


def read_channels(config: Any) -> list[str]:
    extra = getattr(config, "extra", None) or {}
    return irc.parse_channels(extra_or_secret(extra, "channel", "TWITCH_CHANNEL"))


class TwitchAdapter(BasePlatformAdapter):
    """Twitch chat as a Hermes platform: viewers address the bot with a trigger, a mention or a reply."""

    # Twitch cannot edit messages. With this flag the runner streams no deltas, and because edit_message stays the
    # base implementation the runner drains tool-progress and thinking lines instead of posting them.
    SUPPORTS_MESSAGE_EDITING = False
    splits_long_messages = True  # cron delivery hands send() the whole text; send() chunks and caps it
    # Hermes' command prefix in hints (base.py typed_command_prefix, 0.22.7 through main). Twitch keeps "/" for its own,
    # so the approval prompt (run.py _format_exec_approval_fallback) says "Reply !hermes approve ... or !hermes deny",
    # and "!hermes approve" from the owner arrives as "approve" for run_busy.py _route_plaintext_approval_while_busy.
    typed_command_prefix = "!hermes "

    # Timings in seconds; tests shorten them per instance.
    ping_interval = 60.0
    pong_timeout = 15.0
    revalidate_tick = 60.0
    send_wait = 15.0
    turn_start_grace = 5.0
    turn_settle = 5.0
    turn_timeout = 900.0
    backoff_base, backoff_cap = 1.0, 60.0

    def __init__(self, config: Any, settings: dict | None = None, **kwargs: Any):
        super().__init__(config=config, platform=Platform("twitch"))
        extra = getattr(config, "extra", None) or {}
        settings = settings or {}
        _private_notices(config)
        self._channels = read_channels(config)
        self._endpoint = sender.chat_endpoint(extra)
        self._validate_url = self._endpoint.validate_url
        self.helix = Helix(lambda: self._token, base_url=sender.loopback_url(extra, "_test_helix_url", HELIX_URL),
                           validate_url=self._validate_url)
        self._chat_commands = _bool_option("chat_commands", True, settings, extra)
        self._max_lines = _int_option("max_reply_lines", 3, settings, extra, minimum=1, maximum=sender.MAX_LINES)
        self._notes_on = _bool_option("viewer_notes", True, settings, extra)
        self._notes_days = _int_option("viewer_notes_days", viewer_notes.DEFAULT_DAYS, settings, extra, minimum=1)
        self._caption_answers = _bool_option("caption_answers", False, settings, extra)
        self._reply_language = _reply_language_option(settings, extra)
        self._extra_triggers = extra_or_secret(extra, "triggers", "TALLYHAND_TRIGGERS") or ""
        self._clock: Callable[[], float] = time.monotonic
        self._tail = ChatTail(_int_option("chat_tail", 0, settings, extra))
        self._cooldowns = Cooldowns(_int_option("user_cooldown", 20, settings, extra, env="TALLYHAND_USER_COOLDOWN"),
                                    clock=lambda: self._clock())
        self._gate = TurnGate(in_flight=1, queued=3, on_evict=self._evicted)
        self._roles = RoleRegistry()
        self._dedup = MessageDeduplicator(ttl_seconds=600)
        self._triggers = TriggerMatcher("", self._extra_triggers)
        self._expiry = auth.ExpiryNotice(clock=lambda: self._clock())
        self._event_mode = _mode_option("event_messages", events.OFF, settings, extra)
        self._min_raid = _int_option("min_raid_size", 1, settings, extra)
        self._event_lines: events.EventLines | None = None
        if self._event_mode == events.THANKS:
            self._event_lines = events.EventLines(self._min_raid, clock=lambda: self._clock())
        self._auto_clip = _bool_option("auto_clip", False, settings, extra)
        self._auto_clip_announce = _bool_option("auto_clip_announce", True, settings, extra)
        self._autoclip: AutoClip | None = None
        if self._auto_clip:
            self._autoclip = AutoClip(self.helix, lambda: self.room_id(self.owner_channel()),
                                      self._post_owner if self._auto_clip_announce else None,
                                      clock=lambda: self._clock())
        self._notes: viewer_notes.ViewerNotes | None = None
        self._notes_task: asyncio.Task | None = None
        self._language_task: asyncio.Task | None = None
        self._privacy = privacy.Report()
        # allow_profile_memory_for_viewers: answer viewers and moderators while the profile's memory is on.
        self._viewer_memory = _bool_option(privacy.OPT_IN, False, settings, extra)
        self._barred_log = Cooldowns(BARRED_LOG_PERIOD, clock=lambda: self._clock())
        self._allowed_ids: dict[str, str] = {}  # login -> user id, for TWITCH_ALLOWED_USERS
        self._unknown_logins: set[str] = set()
        self._allowlist_retry = False  # Twitch did not answer the last lookup; the token watch tries again
        self._handoffs: dict[str, asyncio.Future] = {}  # per channel and asker: the hand-off in progress
        self._room_ids: dict[str, str] = {}
        self._send_limiter = RateLimiter(SEND_LIMIT, SEND_PERIOD)
        self._elevated_limiter = RateLimiter(SEND_LIMIT_ELEVATED, SEND_PERIOD)
        self._channel_limiters: dict[str, RateLimiter] = {}
        self._slow: dict[str, int] = {}
        self._hints = Cooldowns(HINT_COOLDOWN, clock=lambda: self._clock())
        self._command_cooldowns = Cooldowns(commands.COMMAND_COOLDOWN, clock=lambda: self._clock())
        self._channel_command_cooldowns = Cooldowns(commands.CHANNEL_COOLDOWN, clock=lambda: self._clock())
        self._elevated: set[str] = set()
        self._join_limiter = RateLimiter(JOIN_LIMIT, JOIN_PERIOD)
        self._turns: dict[str, _Turn] = {}
        self._commands_running: set[tuple] = set()
        self._command_ids: deque[str] = deque(maxlen=RECENT_COMMANDS)  # the owner's slash commands, never captioned
        self._tasks: set[asyncio.Task] = set()
        self._delivered: dict[str, OrderedDict[str, float]] = {}
        # Known before connect: Hermes' profile multiplexer fingerprints adapters by their token attribute
        # (gateway/run_adapters.py _adapter_credential_fingerprint) and parks a second adapter with the same bot.
        self._token = sender.read_token(config)
        self._login = self._user_id = ""
        self._validated_at = 0.0
        self._revalidate_by = 0.0  # next forced validate; expires_in can pull this earlier than the hourly cadence
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._recv_task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._watch_task: asyncio.Task | None = None
        self._supervisor: asyncio.Task | None = None
        self._welcome: asyncio.Future | None = None
        self._pong = False
        self._closing = False

    # public state

    @property
    def bot_login(self) -> str:
        return self._login

    @property
    def channels(self) -> list[str]:
        return list(self._channels)

    @property
    def roles(self) -> RoleRegistry:
        return self._roles

    def owner_id(self) -> str:
        """User id of the owner: the broadcaster of the first configured channel, once ROOMSTATE named it."""
        return self._room_ids.get(self._channels[0], "") if self._channels else ""

    def owner_channel(self) -> str:
        return self._channels[0].lstrip("#") if self._channels else ""

    def room_id(self, channel: str) -> str:
        """The broadcaster id of a joined channel (``#`` optional); empty for one the bot has not joined."""
        return self._room_ids.get(irc.normalize_channel(channel), "")

    # lifecycle

    def _fail(self, code: str, message: str, *, retryable: bool) -> bool:
        self._set_fatal_error(code, message, retryable=retryable)
        return False

    def _redact(self, text: str) -> str:
        return auth.redact(text, self._token)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Validate the token, connect, log in and join the configured channels."""
        token = sender.read_token(self.config)
        if not token or not self._channels:
            logger.error("Twitch: TWITCH_BOT_TOKEN and TWITCH_CHANNEL must be set")
            return self._fail("config_missing", "TWITCH_BOT_TOKEN and TWITCH_CHANNEL must be set", retryable=False)
        try:
            info = await auth.validate_token(token, url=self._validate_url)
        except auth.AuthError as exc:
            logger.error("Twitch: %s", exc.message)
            return self._fail(exc.code, exc.message, retryable=exc.retryable)
        self._token, self._login, self._user_id = token, info.login, info.user_id
        self.helix.remember(info, token)
        self._validated_at = self._clock()
        self._schedule_revalidate(info)
        self._note_expiry(info)
        self._triggers = TriggerMatcher(info.login, self._extra_triggers)
        if not self._acquire_platform_lock("twitch", info.login, f"Twitch bot account {info.login}"):
            return False
        self._closing = False
        # Before the session: a !forgetme or !mynotes that arrives with the first chat lines finds the notes file.
        self._privacy = privacy.check(shared=not self.per_viewer_sessions(), opted_in=self._viewer_memory)
        await self._open_notes()
        try:
            await self._open_session()
        except _SessionError as exc:
            await self._close_transport()
            with contextlib.suppress(Exception):
                self._release_platform_lock()
            logger.error("Twitch: %s", exc.message)
            return self._fail(exc.code, exc.message, retryable=exc.retryable)
        self._mark_connected()
        self._wire_plugin_handlers(None)
        roles.attach(self)
        self._watch_task = asyncio.create_task(self._token_watch())
        self._supervisor = asyncio.create_task(self._supervise())
        if self._notes is not None and self._notes_task is None:
            self._notes_task = asyncio.create_task(self._purge_notes_daily(self._notes))
        await self._resolve_allowlist()
        if self._autoclip is not None:
            self._autoclip.scope_missing()
        self._spawn(self._refresh_languages())
        if self._language_task is None:
            self._language_task = asyncio.create_task(self._refresh_languages_loop())
        logger.info("Twitch: connected as %s, joined %s", self._login, ", ".join(self._channels))
        return True

    async def disconnect(self) -> None:
        """Close the connection and stop every background task."""
        self._closing = True
        roles.detach(self)
        with contextlib.suppress(Exception):
            self._release_platform_lock()
        self._mark_disconnected()
        obs_tools.CAPTION.cancel()
        # Drain until empty: a PRIVMSG handled during cancel can still _spawn after the first snapshot.
        while True:
            pending = [task for task in (
                self._supervisor, self._watch_task, self._notes_task, self._language_task, self._recv_task, *self._tasks
            ) if task is not None and not task.done()]
            if not pending:
                break
            for task in pending:
                await cancel_task(task)
        self._notes_task = None
        self._language_task = None
        self._commands_running.clear()
        if self._autoclip is not None:
            self._autoclip.reset()
        self._elevated.clear()
        self._slow.clear()
        self._gate.clear()
        self._turns.clear()
        await self._close_transport()  # last: a peer that never answers costs CLOSE_GRACE, inside Hermes' budget

    async def _open_session(self) -> None:
        """TLS connect, CAP/PASS/NICK, wait for the welcome, join. Raises _SessionError."""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._endpoint.host, self._endpoint.port,
                                        ssl=sender.ssl_context(self._endpoint.tls)), timeout=TIMEOUT)
        except (TimeoutError, OSError) as exc:
            endpoint = f"{self._endpoint.host}:{self._endpoint.port}"
            raise _SessionError("connect_failed", f"could not reach Twitch chat at {endpoint} "
                                f"({type(exc).__name__})", retryable=True) from None
        self._welcome = asyncio.get_running_loop().create_future()
        self._recv_task = asyncio.create_task(self._receive_loop(self._reader, self._welcome))
        try:
            await self._send_raw(f"CAP REQ :{CAPABILITIES}")
            await self._send_raw(f"PASS oauth:{self._token}")
            await self._send_raw(f"NICK {self._login}")
            await asyncio.wait_for(self._welcome, timeout=TIMEOUT)
            await self._join_channels()
        except TimeoutError:
            raise _SessionError("login_timeout", "Twitch chat did not confirm the login within 30 s",
                                retryable=True) from None
        except (OSError, ConnectionError) as exc:
            raise _SessionError("connection_lost", f"Twitch chat closed the connection during login "
                                f"({type(exc).__name__})", retryable=True) from None
        self._ping_task = asyncio.create_task(self._keepalive())

    async def _join_channels(self) -> None:
        pending = list(self._channels)
        while pending:
            batch: list[str] = []
            while pending and len(",".join([*batch, pending[0]])) <= MAX_JOIN_LINE and self._join_limiter.take():
                batch.append(pending.pop(0))
            if batch:
                await self._send_raw(f"JOIN {','.join(batch)}")
            if pending:
                await asyncio.sleep(max(self._join_limiter.delay(), 0.05))

    async def _close_transport(self) -> None:
        """Close the connection, giving the TLS shutdown CLOSE_GRACE seconds before the socket is cut."""
        await cancel_task(self._ping_task)
        writer, self._writer = self._writer, None
        if writer is not None:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=CLOSE_GRACE)
            except (OSError, RuntimeError):  # TimeoutError included: the peer never finished the close
                writer.transport.abort()
        if self._recv_task is not asyncio.current_task():
            await cancel_task(self._recv_task)
        self._reader = None

    def _drop_connection(self) -> None:
        """Cut the socket (the peer is already gone) so the receive loop ends and the supervisor reconnects."""
        if self._writer is not None:
            self._writer.transport.abort()

    async def _fatal(self, code: str, message: str, *, retryable: bool = False) -> None:
        logger.error("Twitch: %s", message)
        self._closing = True
        self._set_fatal_error(code, message, retryable=retryable)
        await self._close_transport()
        await self._notify_fatal_error()

    async def _supervise(self) -> None:
        """Reconnect after a drop, a missed keepalive or RECONNECT, backing off 1, 2, 4 ... 60 s with jitter."""
        attempt = 0
        while not self._closing:
            if self._recv_task is not None:
                await asyncio.wait({self._recv_task})
            if self._closing:
                return
            self._mark_disconnected()
            self._elevated.clear()
            self._slow.clear()
            await self._close_transport()
            while not self._closing:
                delay = backoff_delay(attempt, base=self.backoff_base, cap=self.backoff_cap)
                attempt += 1
                logger.info("Twitch: reconnecting in %.1fs", delay)
                await asyncio.sleep(delay)
                if self._closing:
                    return
                try:
                    await self._open_session()
                except _SessionError as exc:
                    await self._close_transport()
                    if not exc.retryable:
                        await self._fatal(exc.code, exc.message)
                        return
                    logger.warning("Twitch: reconnect failed: %s", exc.message)
                    continue
                if self._closing:  # a fatal error or disconnect() landed during the handshake
                    await self._close_transport()
                    return
                attempt = 0
                self._mark_connected()
                self._wire_plugin_handlers(None)
                logger.info("Twitch: reconnected as %s", self._login)
                self._spawn(self._resolve_allowlist())  # logins Twitch could not answer for last time
                self._spawn(self._refresh_languages())
                break

    async def _keepalive(self) -> None:
        """Client PING every ping_interval; no PONG within pong_timeout means the connection is gone, and lines
        written to it meanwhile may be lost, so it is noticed in about a minute rather than at the next send."""
        while True:
            await asyncio.sleep(self.ping_interval)
            self._pong = False
            with contextlib.suppress(OSError, ConnectionError):
                await self._send_raw("PING :tmi.twitch.tv")
            await asyncio.sleep(self.pong_timeout)
            if not self._pong:
                logger.warning("Twitch: no PONG within %.0f s; reconnecting", self.pong_timeout)
                self._drop_connection()
                return

    async def _token_watch(self) -> None:
        """Re-validate the token while connected; a rejected token ends the session for good. The next check is
        the sooner of the hourly cadence and a little before Twitch's expires_in."""
        while not self._closing:
            await asyncio.sleep(self.revalidate_tick)
            if self._allowlist_retry:
                await self._resolve_allowlist()
            started = self._clock()
            if started < self._revalidate_by:
                continue
            try:
                info = await auth.validate_token(self._token, url=self._validate_url)
            except auth.AuthError as exc:
                # An odd answer (a 403 or 404 from a proxy) is retried; only a token Twitch read and refused is fatal.
                if exc.retryable or exc.code == "token_rejected":
                    logger.warning("Twitch: token check failed, will retry: %s", exc.message)
                    continue
                await self._fatal(exc.code, exc.message)
                return
            self._validated_at = started
            self.helix.remember(info, self._token)
            self._schedule_revalidate(info)
            self._note_expiry(info)

    def _schedule_revalidate(self, info: auth.TokenInfo) -> None:
        # When Twitch's expires_in is shorter than the hourly check, revalidate at half that life (capped at 60 s).
        skew = min(60.0, max(0.0, info.expires_in / 2))
        by_expiry = self._validated_at + max(0.0, info.expires_in - skew) if info.expires_in > 0 else float("inf")
        self._revalidate_by = min(self._validated_at + auth.REVALIDATE_SECONDS, by_expiry)

    def _note_expiry(self, info: auth.TokenInfo) -> None:
        if warning := self._expiry.check(info):
            logger.warning("Twitch: %s", warning)

    async def _resolve_allowlist(self) -> None:
        """Look up the Twitch logins in TWITCH_ALLOWED_USERS that are not resolved yet; a login Twitch does not know
        is logged once. Until a login resolves it lets nobody in, so a lookup Twitch did not answer is tried again
        on every token watch tick; numeric ids always work."""
        logins = [login for login in allowlist_logins(platform_gate_env(ALLOWED_ENV)) if login not in self._allowed_ids]
        if not logins:
            self._allowlist_retry = False
            return
        try:
            found = await asyncio.wait_for(self.helix.users(logins), ALLOWLIST_TIMEOUT)
        except (HelixError, TimeoutError) as exc:
            if not self._allowlist_retry:
                reason = exc.message if isinstance(exc, HelixError) else "no answer in time"
                logger.warning("Twitch: could not look up the logins in %s (%s); they let nobody in until Twitch "
                               "answers, tried again every %.0f s", ALLOWED_ENV, reason, self.revalidate_tick)
            self._allowlist_retry = True
            return
        self._allowlist_retry = False
        self._allowed_ids.update({login: user.id for login, user in found.items()})
        for login in logins:
            if login not in found and login not in self._unknown_logins:
                self._unknown_logins.add(login)
                logger.warning("Twitch: %s names %s, but Twitch has no account with that login", ALLOWED_ENV, login)

    def resolved_allowlist_user_ids(self) -> set[str]:
        """User ids for the Twitch logins in TWITCH_ALLOWED_USERS. Hermes asks the adapter for these on every
        authorization check and adds them to the platform allowlist, which it compares by exact user id
        (gateway/authz_mixin.py _adapter_resolved_allowlist_ids, 0.22.7 through main). Only logins still in the
        list count, so removing one takes effect at once. No I/O."""
        return {self._allowed_ids[login] for login in allowlist_logins(platform_gate_env(ALLOWED_ENV))
                if login in self._allowed_ids}

    async def _open_notes(self) -> None:
        """Open the viewer notes file and purge expired notes; connect starts the daily purge. With viewer_notes
        off, a file left from before stays open for !mynotes, !forgetme and the purge, and nothing new is stored.
        A file that cannot be used turns the notes off and never fails the connection."""
        if self._notes is not None:
            return
        try:
            notes = viewer_notes.ViewerNotes(viewer_notes.default_path(), self._notes_days, enabled=self._notes_on)
            if not (self._notes_on or notes.path.exists()):
                return
            await asyncio.to_thread(notes.open)
            purged = await asyncio.to_thread(notes.purge)
        except (OSError, sqlite3.Error) as exc:
            logger.warning("Twitch: viewer notes are off, the notes file cannot be used (%s)", type(exc).__name__)
            return
        if purged:
            logger.info("Twitch: deleted %d expired viewer note(s)", purged)
        self._notes = notes

    async def _purge_notes_daily(self, notes: viewer_notes.ViewerNotes) -> None:
        while not self._closing:
            await asyncio.sleep(viewer_notes.PURGE_EVERY)
            try:
                if purged := await asyncio.to_thread(notes.purge):
                    logger.info("Twitch: deleted %d expired viewer note(s)", purged)
            except (OSError, sqlite3.Error) as exc:
                logger.warning("Twitch: could not purge expired viewer notes (%s)", type(exc).__name__)

    # inbound

    async def _receive_loop(self, reader: asyncio.StreamReader, welcome: asyncio.Future) -> None:
        try:
            while raw := await reader.readline():
                line = irc.parse_line(raw.decode("utf-8", errors="replace"))
                if line is None:
                    continue
                if line.command == "RECONNECT":
                    logger.info("Twitch: server asked for a reconnect")
                    break
                try:
                    await self._handle_line(line, welcome)
                except Exception as exc:  # one bad line must not end the session (same seam as the IRC adapter)
                    logger.warning("Twitch: error handling %s: %s", line.command, self._redact(str(exc)))
        except (OSError, ValueError, asyncio.IncompleteReadError) as exc:
            logger.warning("Twitch: connection error: %s", self._redact(str(exc)) or type(exc).__name__)
        except asyncio.CancelledError:
            welcome.cancel()
            raise
        finally:
            if not welcome.done():
                welcome.set_exception(_SessionError(
                    "connection_lost", "Twitch chat closed the connection during login", retryable=True))

    async def _handle_line(self, line: irc.Line, welcome: asyncio.Future) -> None:
        command = line.command
        if command == "PING":
            await self._send_raw(f"PONG :{line.param(0, 'tmi.twitch.tv')}")
        elif command == "PONG":
            self._pong = True
        elif command == "PRIVMSG":
            await self._on_privmsg(line)
        elif command == "001":
            if not welcome.done():
                welcome.set_result(True)
        elif command == "CAP" and line.param(1).upper() == "NAK":
            self._login_failed(welcome, "cap_nak", f"Twitch refused the chat capabilities ({line.param(-1)})")
        elif command == "NOTICE":
            self._on_notice(line, welcome)
        elif command == "USERNOTICE":
            if event := irc.parse_usernotice(line):
                self._on_channel_event(event)
        elif command == "GLOBALUSERSTATE":
            self._user_id = line.tags.get("user-id") or self._user_id
        elif command == "ROOMSTATE":
            self._on_roomstate(line)
        elif command == "USERSTATE":
            self._on_userstate(line)
        elif command == "CLEARCHAT":
            channel, login, user_id = line.param(0).lower(), line.param(1).lower(), line.tags.get("target-user-id", "")
            if login:
                self._tail.drop_user(channel, login, user_id)
                self._drop_waiting(channel, lambda event, asker: asker == login
                                   or bool(user_id) and event.source.user_id == user_id)
            else:
                self._tail.clear(channel)
                self._drop_waiting(channel, lambda event, asker: True)
        elif command == "CLEARMSG":
            channel, target = line.param(0).lower(), line.tags.get("target-msg-id", "")
            self._tail.drop_message(channel, target)
            self._drop_waiting(channel, lambda event, asker: bool(target) and event.message_id == target)

    def _evicted(self, item: object) -> None:
        """A viewer's queued question gave way to the streamer's or a moderator's: they may ask again at once."""
        event, _login = item
        self._cooldowns.clear((event.source.chat_id, event.source.user_id or _login))

    def _drop_waiting(self, channel: str, predicate: Callable[[MessageEvent, str], bool]) -> None:
        """Moderators removed these messages: their queued questions go too. ``predicate`` gets the queued
        event and the asker's login."""
        if dropped := self._gate.remove(channel, lambda item: predicate(*item)):
            logger.debug("Twitch: dropped %d moderated question(s) in %s", len(dropped), channel)

    def _on_roomstate(self, line: irc.Line) -> None:
        channel = line.param(0).lower()
        if room_id := line.tags.get("room-id"):
            self._room_ids[channel] = room_id
            if self.helix.language_stale(room_id):
                self._spawn(self.helix.refresh_channel_language(room_id))
        if "slow" in line.tags:  # partial ROOMSTATE updates carry only the changed setting
            try:
                self._slow[channel] = max(0, int(line.tags["slow"]))
            except ValueError:
                self._slow.pop(channel, None)
            self._channel_limiter(channel).period = self._channel_period(channel)

    def _login_failed(self, welcome: asyncio.Future, code: str, message: str) -> None:
        if welcome.done():
            # Auth failure after a successful login: stop rather than staying connected on a dead token.
            self._spawn(self._fatal(code, message, retryable=False))
        else:
            welcome.set_exception(_SessionError(code, message, retryable=False))

    def _on_notice(self, line: irc.Line, welcome: asyncio.Future) -> None:
        text, msg_id = line.param(-1), line.tags.get("msg-id", "")
        if not msg_id and text.strip().lower() in sender.AUTH_FAILURES:
            self._login_failed(welcome, "auth_failed", f"Twitch chat rejected the login ({text.strip()}): "
                               f"{auth.INVALID_TOKEN}")
        elif msg_id in _CHANNEL_NOTICES:
            logger.warning("Twitch: cannot use %s: %s", line.param(0), text)
        elif msg_id in _SEND_NOTICES:
            logger.warning("Twitch: message not delivered in %s (%s): %s", line.param(0), msg_id, text)
        else:
            logger.debug("Twitch: notice %s in %s: %s", msg_id or "-", line.param(0), text)

    def _on_channel_event(self, event: irc.ChannelEvent) -> None:
        """A raid, sub, resub or gift: a thank-you line when event_messages asks for one; nothing is kept."""
        if self._closing:
            return
        logger.debug("Twitch: %s event in %s", event.kind, event.channel)
        if self._event_lines is not None and (text := self._event_lines.line(event)):
            self._spawn(self._post(event.channel, text))

    def _on_userstate(self, line: irc.Line) -> None:
        channel = line.param(0).lower()
        badges = irc.parse_badges(line.tags.get("badges"))
        if sender.is_elevated(badges, line.tags):
            self._elevated.add(channel)
        else:
            self._elevated.discard(channel)
        if own_id := line.tags.get("id"):
            self._triggers.remember_own(own_id)

    async def _on_privmsg(self, line: irc.Line) -> None:
        if self._closing:
            return
        msg = irc.parse_privmsg(line)
        if msg is None:
            return
        if msg.login == self._login or (msg.user_id and msg.user_id == self._user_id):
            return
        # Shared chat repeats a message into every channel of the session, each copy with its own id. A copy whose
        # source room is another joined channel is skipped: the original arrives there, with that room's roles.
        source_room = msg.tags.get("source-room-id", "")
        if source_room and msg.room_id and source_room != msg.room_id and source_room in self._room_ids.values():
            return
        if self._dedup.is_duplicate(msg.tags.get("source-id") or msg.message_id):
            return
        room_id = msg.room_id or self._room_ids.get(msg.channel, "")
        if msg.room_id:
            self._room_ids.setdefault(msg.channel, msg.room_id)
        role = self._roles.observe(room_id, msg.user_id, msg.tags, msg.badges)
        if self._autoclip is not None and msg.channel == self._channels[0]:
            if reason := self._autoclip.observe(msg.user_id or msg.login, msg.text):
                self._spawn(self._autoclip.clip(reason))
        # A trigger keeps priority: "!hermes ..." is always a question, whatever commands exist. With chat_commands
        # off, only the commands a viewer needs to see and delete what is kept about them stay.
        body = self._triggers.body(msg.text, msg.reply_parent_login)
        if self._triggers.strip_prefix(body) is None and (found := commands.parse(body)) is not None and (
                self._chat_commands or found[0].always):
            self._on_command(msg, role, room_id, *found)
            return
        question = self._triggers.match(msg.text, msg.reply_parent_msg_id, msg.reply_parent_login)
        if question is None:
            at = msg.sent_ts / 1000 if msg.sent_ts else time.time()
            self._tail.add(msg.channel, at, msg.display_name, msg.text, login=msg.login, message_id=msg.message_id,
                           user_id=msg.user_id)
            logger.debug("Twitch: chat line in %s", msg.channel)  # bystanders' names and words stay out of logs
            return
        key = (msg.channel, msg.user_id or msg.login)
        if not (question := one_line(question)):
            self._send_hint(msg, key)
            return
        if self._is_sender_authorized(msg.user_id, chat_type="group", chat_id=msg.channel) is False:
            return  # Hermes would refuse it: no turn slot, no cooldown, and no log line naming the viewer
        if (barred := self._barred(msg.user_id)) is not None:
            self._log_barred(barred)
            return  # refused the same way: their turn would carry the owner's memory
        if not self._cooldowns.ready(key, exempt=role.privileged):
            logger.debug("Twitch: %s is cooling down in %s", msg.login, msg.channel)
            return
        event = self._build_event(msg, defuse_references(question), self._is_owner(msg.user_id))
        if event.allow_gateway_control and (event.text.startswith("/") or event.text.strip().lower() in ANSWER_WORDS):
            self._command_ids.append(event.message_id)
        if event.allow_gateway_control and self._turn_running_for(msg.channel, msg.user_id):
            # The owner's own turn holds the slot. Hermes routes their slash commands, approval words and clarify
            # answers around or into that busy session; queued here they would wait behind the turn they act on.
            # Anyone else's follow-up waits in the queue, which spares chat Hermes' "interrupting" notice.
            self._cooldowns.start(key, exempt=role.privileged)
            self._spawn(self._hand_off(event))
            return
        # Queued with the asker's login, which CLEARCHAT names and the event does not carry.
        verdict = self._gate.offer(msg.channel, (event, msg.login),
                                   priority=role.privileged or event.allow_gateway_control)
        if verdict == TurnGate.FULL:
            logger.debug("Twitch: %s is busy, dropping a question from %s", msg.channel, msg.login)
            return
        self._cooldowns.start(key, exempt=role.privileged)
        logger.debug("Twitch: question in %s from %s (%s)", msg.channel, msg.login, verdict)
        if verdict == TurnGate.RUN:
            self._spawn(self._run_turn(msg.channel, event))

    def _on_command(self, msg: irc.ChatMessage, role: roles.Role, room_id: str, spec: commands.Spec,
                    args: str) -> None:
        """Answer a chat command without a model call or a turn slot. Callers below the command's level are
        ignored, as are a viewer's repeats inside the per-viewer cooldown and a command the channel's viewers had
        answered moments ago."""
        cmd = commands.Command(name=spec.name, args=args, channel=msg.channel.lstrip("#"), user_id=msg.user_id,
                               login=msg.login, display_name=msg.display_name, role=role,
                               is_owner=self._is_owner(msg.user_id), message_id=msg.message_id, room_id=room_id,
                               helix=self.helix, notes=self._notes, chat_tail=self._tail.size, privacy=self._privacy,
                               may_ask=self._is_sender_authorized(msg.user_id, chat_type="group",
                                                                  chat_id=msg.channel) is True
                               and self._barred(msg.user_id) is None,
                               commands_on=self._chat_commands)
        if not commands.allowed(spec, cmd):
            return
        if spec.name == "forgetme":  # the viewer's lines still waiting in the chat tail go with the rest
            self._tail.drop_user(msg.channel, msg.login, msg.user_id)
        # A command about the caller's own data answers nobody else, so it has no channel-wide cooldown, and the
        # per-viewer one is its own. One with no cooldown (!forgetme) always runs, one at a time per viewer; only
        # its reply waits out that viewer's cooldown, so repeating it cannot drive the bot's chat output.
        exempt = cmd.level != roles.VIEWER
        viewer = msg.user_id or msg.login
        own = spec.cooldown in (commands.COOLDOWN_OWN, commands.COOLDOWN_NONE)
        key = (msg.channel, viewer, spec.name) if own else (msg.channel, viewer)
        channel_key = (msg.channel, spec.name)
        shared = spec.cooldown == commands.COOLDOWN_SHARED
        reply = self._command_cooldowns.ready(key, exempt=exempt) and (
            not shared or self._channel_command_cooldowns.ready(channel_key, exempt=exempt))
        if spec.cooldown == commands.COOLDOWN_NONE:
            if key in self._commands_running:
                return
        elif not reply:
            return
        if reply:
            self._command_cooldowns.start(key, exempt=exempt)
            if shared:
                self._channel_command_cooldowns.start(channel_key, exempt=exempt)
        logger.debug("Twitch: !%s in %s from %s", spec.name, msg.channel, msg.login)
        self._commands_running.add(key)
        self._spawn(self._run_command(spec, cmd, msg.channel, msg.message_id, key, reply=reply))

    async def _run_command(self, spec: commands.Spec, cmd: commands.Command, channel: str, message_id: str,
                           key: tuple, *, reply: bool) -> None:
        try:
            text = await spec.handler(cmd)
        finally:
            self._commands_running.discard(key)
        if text and reply:
            await self._post(channel, str(text), reply_to=message_id or None)

    def _is_owner(self, user_id: str | None) -> bool:
        """The owner is the broadcaster of the first configured channel, wherever they chat."""
        owner = self.owner_id()
        return bool(owner and user_id == owner)

    def _barred(self, user_id: str | None) -> privacy.MemoryState | None:
        """The memory state that bars this asker, None when they may ask. Anyone but the owner is barred while the
        profile's memory is on (memory.memory_enabled, memory.user_profile_enabled or an external memory.provider) or
        its config cannot be read: their turn would carry the owner's MEMORY.md and USER.md or reach a memory
        provider. allow_profile_memory_for_viewers lifts it. The settings are read again for every question, so a
        change to config.yaml counts at once, and the profile is the one that runs the bot."""
        if self._viewer_memory or self._is_owner(user_id):
            return None
        state = privacy.memory_state()
        return state if state.on else None

    def _log_barred(self, state: privacy.MemoryState) -> None:
        """Say why chat gets no answers, once every BARRED_LOG_PERIOD seconds and without naming the asker."""
        if self._barred_log.ready(BARRED_KEY):
            self._barred_log.start(BARRED_KEY)
            logger.warning("Twitch: %s", state.refusal())

    def toolsets_for_source(self, source: Any) -> list[str] | None:
        """Toolsets per asker: the owner gets platform_toolsets.twitch plus OWNER_TOOLSETS, a moderator (or the
        broadcaster) of the channel MODERATOR_TOOLSETS, everyone else VIEWER_TOOLSETS. Hermes drops the turn's
        override when this raises, so nothing here may raise for anyone but the owner."""
        user_id = getattr(source, "user_id", None)
        if self._is_owner(user_id):
            return owner_toolsets()
        room = self.room_id(str(getattr(source, "chat_id", "") or ""))
        if user_id and room and self._roles.is_privileged(room, user_id):
            return list(MODERATOR_TOOLSETS)
        return list(VIEWER_TOOLSETS)

    def _send_hint(self, msg: irc.ChatMessage, key: tuple[str, str]) -> None:
        """One-line usage hint for a bare trigger, without a model call. It has its own timer, so it never delays
        the real question, and it only goes to viewers Hermes would let ask."""
        if not self._hints.ready(key):
            return
        if self._is_sender_authorized(msg.user_id, chat_type="group", chat_id=msg.channel) is not True:
            return
        if self._barred(msg.user_id) is not None:
            return
        self._hints.start(key)
        self._spawn(self._post(msg.channel, EMPTY_QUESTION_REPLY.format(name=msg.display_name),
                               reply_to=msg.message_id or None))

    def _build_event(self, msg: irc.ChatMessage, question: str, owner: bool) -> MessageEvent:
        message_id = msg.message_id or f"local-{time.monotonic_ns()}"
        source = self.build_source(chat_id=msg.channel, chat_name=msg.channel, chat_type="group",
                                   user_id=msg.user_id, user_name=msg.display_name, message_id=message_id)
        own_parent = bool(msg.reply_parent_msg_id) and (
            self._triggers.is_own_message(msg.reply_parent_msg_id)
            or (bool(msg.reply_parent_user_id) and msg.reply_parent_user_id == self._user_id)
            or (bool(msg.reply_parent_login) and msg.reply_parent_login == self._login))
        askers_parent = bool(msg.reply_parent_msg_id) and (
            (bool(msg.user_id) and msg.reply_parent_user_id == msg.user_id)
            or (bool(msg.reply_parent_login) and msg.reply_parent_login == msg.login))
        # Hermes stores the quoted parent with the question; another viewer's words are not the asker's to share.
        quote = own_parent or askers_parent
        sent = datetime.datetime.fromtimestamp(msg.sent_ts / 1000) if msg.sent_ts else datetime.datetime.now()
        # No metadata: Hermes' native admission (0.22) refuses any event that carries adapter metadata.
        return MessageEvent(
            text=question, message_type=MessageType.TEXT, source=source, message_id=message_id,
            user_id=msg.user_id or None, user_name=msg.display_name,
            reply_to_message_id=msg.reply_parent_msg_id or None,
            # Quoted parent text is untrusted viewer content, same as the question and chat tail.
            reply_to_text=(scrub_untrusted(msg.reply_parent_msg_body) or None) if quote else None,
            reply_to_author_id=(msg.reply_parent_user_id or None) if quote else None,
            reply_to_author_name=(scrub_name(msg.reply_parent_display_name) or None) if quote else None,
            reply_to_is_own_message=own_parent,
            channel_context=None,  # rendered at hand-off, so lines moderated meanwhile are left out
            timestamp=sent,
            # Gateway slash commands stay with the owner; everyone else's text is plain conversation.
            allow_gateway_control=owner)

    # turns

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        if self._closing:
            # Closing: never leave a live coroutine; cancel immediately so cancel-before-start cannot stick.
            task = asyncio.ensure_future(coro)
            task.cancel()
            return task
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            logger.error("Twitch: background task failed: %s", self._redact(repr(exc)))

    async def _hand_off(self, event: MessageEvent) -> None:
        """Attach the chat tail as it stands now, up to the question's own time, and the asker's own viewer notes,
        then pass the event to Hermes. One asker's messages in a channel reach Hermes in the order they came, though
        reading the notes file takes a moment."""
        channel = event.source.chat_id
        key = f"{channel} {event.source.user_id or ''}"
        previous, done = self._handoffs.get(key), asyncio.get_running_loop().create_future()
        self._handoffs[key] = done
        try:
            if previous is not None:
                await previous
            # Moderators' turns hold moderation tools, so other viewers' lines stay out of them.
            moderator = not self._is_owner(event.source.user_id) and self._roles.is_privileged(
                self.room_id(channel), event.source.user_id or "")
            tail = None if moderator else self._tail.render(channel, until=event.timestamp.timestamp())
            # Language guidance is always last: untrusted chat and notes stay above it, and a miss never waits.
            blocks = [tail, await self._asker_notes(event), self._language_guidance(channel)]
            event.channel_context = "\n".join(block for block in blocks if block) or None
            await self.handle_message(event)
        finally:
            done.set_result(None)
            if self._handoffs.get(key) is done:
                del self._handoffs[key]

    async def _asker_notes(self, event: MessageEvent) -> str | None:
        """The notes the asker stored in this channel with !remember; never anyone else's, and none at all while
        Hermes keeps one session for the whole channel, where every viewer's turn would read them."""
        notes, user_id, room = self._notes, event.source.user_id, self.room_id(event.source.chat_id)
        if notes is None or not notes.enabled or not (user_id and room) or self._privacy.shared_sessions:
            return None
        try:
            return await asyncio.to_thread(notes.render, room, user_id, event.source.user_name or "")
        except (OSError, sqlite3.Error) as exc:
            logger.warning("Twitch: could not read viewer notes (%s)", type(exc).__name__)
            return None

    def _language_guidance(self, channel: str) -> str:
        """Reply-language and chat-voice lines from the cache only; a miss or stale entry never delays the turn."""
        room = self.room_id(channel)
        code, title, tags = self.helix.cached_channel_info(room) if room else (None, "", [])
        if room and self.helix.language_stale(room):
            self._spawn(self.helix.refresh_channel_language(room))
        if self._reply_language == language.CHANNEL:
            logger.debug("Twitch: reply language from %s", language.decision_source(code, title, tags))
        return language.guidance(self._reply_language, code, title, tags)

    async def _refresh_languages(self) -> None:
        """Fetch each joined channel's language, title and tags. Failures stay off the turn path."""
        for channel in self._channels:
            room = self._room_ids.get(channel)
            if not room:
                try:
                    user = await self.helix.user(channel.lstrip("#"))
                except HelixError as exc:
                    logger.warning("Twitch: channel language user lookup failed (%s)", exc.code)
                    continue
                if user is None:
                    continue
                room = user.id
            await self.helix.refresh_channel_language(room)

    async def _refresh_languages_loop(self) -> None:
        """Re-read each channel's stream language every CHANNEL_TTL seconds while connected."""
        while not self._closing:
            await asyncio.sleep(CHANNEL_TTL)
            if self._closing or not self._ready():
                continue
            await self._refresh_languages()

    async def _post_owner(self, text: str) -> SendResult:
        """A line of the plugin's own in the owner's channel (the auto clip link)."""
        return await self._post(self._channels[0], text)

    def _turn_running_for(self, channel: str, user_id: str) -> bool:
        return any(turn.user_id == user_id and turn.started.is_set() and not turn.done.is_set()
                   for key, turn in self._turns.items() if key.startswith(f"{channel} "))

    async def _run_turn(self, channel: str, event: MessageEvent) -> None:
        """Hand one question to Hermes and hold the channel's slot until its turn ends."""
        key = f"{channel} {event.message_id}"
        turn = self._turns[key] = _Turn(user_id=event.source.user_id or "")
        try:
            if (barred := self._barred(event.source.user_id)) is not None:
                self._log_barred(barred)  # it waited in the queue while the profile's memory was switched on
                return
            await self._hand_off(event)
            # No start within the grace period: dropped, merged into a busy session or answered inline.
            await asyncio.wait_for(turn.started.wait(), self.turn_start_grace)
            await asyncio.wait_for(turn.done.wait(), self.turn_timeout)
            # Hermes releases the session only when its processing task ends, a little after the hook fires.
            if turn.task is not None and turn.task is not asyncio.current_task():
                await asyncio.wait({turn.task}, timeout=self.turn_settle)
        except TimeoutError:
            logger.debug("Twitch: releasing %s without a finished turn", channel)
        finally:
            self._turns.pop(key, None)
            following = self._gate.finish(channel)
            if following is not None and not self._closing:
                self._spawn(self._run_turn(channel, following[0]))

    def _turn_for(self, event: MessageEvent) -> _Turn | None:
        return self._turns.get(f"{event.source.chat_id if event.source else ''} {event.message_id}")

    async def on_processing_start(self, event: MessageEvent) -> None:
        if turn := self._turn_for(event):
            turn.started.set()

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        if turn := self._turn_for(event):
            turn.task = asyncio.current_task()
            turn.started.set()
            turn.done.set()

    # outbound

    def _channel_period(self, channel: str) -> float:
        """Seconds between two messages in a channel where the bot is not elevated (slow mode stretches it)."""
        slow = self._slow.get(channel, 0)
        return max(CHANNEL_PERIOD, slow + SLOW_MARGIN) if slow else CHANNEL_PERIOD

    def _channel_limiter(self, channel: str) -> RateLimiter:
        if (limiter := self._channel_limiters.get(channel)) is None:
            limiter = self._channel_limiters[channel] = RateLimiter(CHANNEL_LIMIT, self._channel_period(channel))
        return limiter

    def is_elevated(self, channel: str) -> bool:
        """True when USERSTATE shows the bot is broadcaster, moderator or VIP in ``channel``."""
        return channel in self._elevated

    async def _acquire_send(self, channel: str, wait: float, *, own: bool = False) -> bool:
        """Wait up to ``wait`` seconds for send capacity in ``channel``; every message counts toward both
        account-wide windows. ``own`` lines leave OWN_RESERVE messages of each window to Hermes' replies."""
        if channel in self._elevated:
            waited: tuple[RateLimiter, ...] = (self._elevated_limiter,)
        else:
            waited = (self._send_limiter, self._channel_limiter(channel))
        if not await acquire_all(waited, wait, reserve=OWN_RESERVE if own else 0):
            return False
        for limiter in (self._send_limiter, self._elevated_limiter):
            if limiter not in waited:
                limiter.use()
        return True

    async def _send_raw(self, line: str) -> None:
        writer = self._writer
        if writer is None or writer.is_closing():
            raise ConnectionError("not connected to Twitch chat")
        writer.write((irc.clean_line(line) + "\r\n").encode("utf-8"))
        await writer.drain()

    async def send(self, chat_id: str, content: str, reply_to: str | None = None,
                   metadata: dict[str, Any] | None = None) -> SendResult:
        """Send a reply as plain-text chat lines; the reply tag goes on the first line only."""
        return await self._deliver(chat_id, content, reply_to, metadata)

    async def _post(self, channel: str, text: str, reply_to: str | None = None) -> SendResult:
        """One of the plugin's own lines (a usage hint, a command's answer, a thank-you, a clip link): it waits at
        most OWN_WAIT seconds and leaves OWN_RESERVE messages of each window to Hermes' replies."""
        return await self._deliver(channel, text, reply_to, None, own=True)

    async def _deliver(self, chat_id: str, content: str, reply_to: str | None, metadata: dict[str, Any] | None,
                       *, own: bool = False) -> SendResult:
        channel = irc.normalize_channel(chat_id)
        if not channel:
            return SendResult(success=False, error=f"not a Twitch channel: {chat_id!r}")
        if not self._serves(channel):
            # A cron job or a home channel setting cannot make the bot post where it was not asked to be.
            return SendResult(success=False, error=f"{channel} is {sender.NOT_SERVED}")
        content = content or ""
        redelivery = _REDELIVERY_RE.match(content)
        # After a gateway restart (the only time Hermes sends its "restarted" note) this adapter is new and
        # remembers nothing, so it cannot tell what chat got: the reply goes out with Hermes' note.
        if redelivery and "restarted" in redelivery.group(0):
            redelivery = None
        if redelivery:
            content = content[redelivery.end():]
        # In slow mode a reply from a bot that is not elevated is one line; the next would wait out the interval.
        # Hermes' approval prompt (metadata is_approval_prompt, gateway/run_turn_runner.py _render_approval_sync)
        # keeps up to three lines even then, so "!hermes deny" and the deadline are never cut off.
        max_lines = 1 if self._slow.get(channel) and channel not in self._elevated else self._max_lines
        if isinstance(metadata, dict) and metadata.get("is_approval_prompt"):
            max_lines = max(max_lines, APPROVAL_LINES)
        # Redacted again after formatting, which drops code spans and control characters that could split the token.
        lines = [self._redact(line) for line in
                 irc.format_reply(self._redact(content), limit=irc.MAX_LINE, max_lines=max_lines)]
        if not lines:
            return SendResult(success=False, error="nothing to send after formatting")
        if redelivery:
            # Before the connection check: a retry that finds chat down still marks what chat already shows.
            fresh = [line for line in lines if not self._was_delivered(channel, line)]
            logger.debug("Twitch: redelivery to %s, %d of %d lines already in chat", channel,
                         len(lines) - len(fresh), len(lines))
            if not fresh:
                return _sent()
            lines = fresh
        if not self._ready():
            return SendResult(success=False, error="Not connected", retryable=True)
        result = await self._send_lines(channel, lines, reply_to if irc.is_message_id(reply_to) else None, own=own)
        raw = result.raw_response if isinstance(result.raw_response, dict) else {}
        if ((result.success or raw.get("sent")) and not redelivery and reply_to not in self._command_ids
                and self._caption_wanted(channel, metadata)):
            self._spawn(obs_tools.mirror(" ".join(lines)))
        return result

    def _caption_wanted(self, channel: str, metadata: Any) -> bool:
        """caption_answers: a reply in the owner's channel that Hermes marks as one, with OBS set up. Hermes puts
        ``notify`` in the send metadata of a turn's final reply and of a command's inline reply
        (gateway/platforms/base.py _mark_notify_metadata, 0.22.7 through main); busy and progress notices and the
        plugin's own lines go without it."""
        return (self._caption_answers and bool(self._channels) and channel == self._channels[0]
                and isinstance(metadata, dict) and metadata.get("notify") is True and "job_id" not in metadata
                and obs.configured())

    def per_viewer_sessions(self) -> bool:
        """False when Hermes keeps one session per channel (group_sessions_per_user off). The runner copies the
        gateway's setting into this adapter's config, where the base adapter reads it for session keys
        (gateway/run_adapters.py _instantiate_adapter, gateway/platforms/base.py _source_session_key, 0.22.7
        through main)."""
        extra = getattr(self.config, "extra", None) or {}
        return bool(extra.get("group_sessions_per_user", True))

    def _serves(self, channel: str) -> bool:
        """The channel is one the bot joined, or the home channel."""
        if channel in self._channels:
            return True
        home = getattr(getattr(self.config, "home_channel", None), "chat_id", "") or ""
        return channel in (irc.normalize_channel(str(home)), sender.allowed_target(channel))

    def _was_delivered(self, channel: str, line: str) -> bool:
        """True when ``line`` went to ``channel`` in the last DUPLICATE_WINDOW seconds. A hit counts as seen again,
        so a reply Hermes keeps redelivering stays remembered."""
        delivered = self._delivered.get(channel)
        if not delivered:
            return False
        now = self._clock()
        while delivered and now - next(iter(delivered.values())) > DUPLICATE_WINDOW:
            delivered.popitem(last=False)
        if line not in delivered:
            return False
        delivered.pop(line)
        delivered[line] = now
        return True

    def _remember_delivered(self, channel: str, line: str) -> None:
        delivered = self._delivered.setdefault(channel, OrderedDict())
        delivered.pop(line, None)
        now = delivered[line] = self._clock()
        while len(delivered) > DELIVERED_LINES or now - next(iter(delivered.values())) > DUPLICATE_WINDOW:
            delivered.popitem(last=False)

    def _ready(self) -> bool:
        """Logged in and joined: the writer exists before that, while a reconnect is still handshaking."""
        return self.is_connected and self._writer is not None and not self._writer.is_closing()

    async def _send_lines(self, channel: str, lines: list[str], parent: str | None, *, sent: int = 0,
                          own: bool = False) -> SendResult:
        """Write ``lines``; ``sent`` lines of the same reply went out before. A failure after the first line reports
        partial delivery, so Hermes resumes with the rest instead of repeating what chat already shows."""
        for index, text in enumerate(lines):
            # Later lines of a reply wait out a whole window rather than leave the reply half-sent.
            wait = self.send_wait if sent == 0 else max(self.send_wait, SEND_PERIOD, self._channel_period(channel))
            if not await self._acquire_send(channel, OWN_WAIT if own else wait, own=own):
                logger.warning("Twitch: rate limited in %s, %d lines of this reply sent", channel, sent)
                return self._send_failure(channel, lines[index:], sent, "rate limited", error_kind="rate_limited")
            writer = self._writer
            if not self._ready() or writer is None:
                return self._send_failure(channel, lines[index:], sent, "Not connected", retryable=True)
            writer.write((irc.privmsg(channel, text, parent if sent == 0 else None) + "\r\n").encode("utf-8"))
            sent += 1
            self._remember_delivered(channel, text)
            try:
                await writer.drain()
            except (OSError, ConnectionError) as exc:
                # The line may have left already; resume after it rather than risk showing it twice.
                return self._send_failure(channel, lines[index + 1:], sent, self._redact(str(exc)) or "send failed",
                                          retryable=True)
            logger.debug("Twitch: sent a line to %s", channel)  # the text stays out: it can quote a viewer's notes
        return _sent()

    @staticmethod
    def _send_failure(channel: str, remaining: list[str], sent: int, error: str, **kwargs: Any) -> SendResult:
        # delivered_chunks and total_chunks are the keys Hermes' send_chunks sets; cron's delivery note reads them.
        raw = {"partial_overflow": True, "channel": channel, "remaining": remaining, "sent": sent,
               "delivered_chunks": sent, "total_chunks": sent + len(remaining)} if sent else None
        return SendResult(success=False, error=error, raw_response=raw, **kwargs)

    async def _resume_partial_send(self, chat_id: str, result: SendResult, *, reply_to: str | None,
                                   metadata: Any) -> SendResult | None:
        """Send only the lines a partly delivered reply still owes; they were never written to the socket."""
        raw = result.raw_response if isinstance(result.raw_response, dict) else {}
        remaining, channel = raw.get("remaining"), raw.get("channel")
        if not isinstance(remaining, list) or not channel:
            return None
        if not remaining:
            return _sent()
        if not self._ready():
            return self._send_failure(channel, remaining, int(raw.get("sent") or 1), "Not connected", retryable=True)
        return await self._send_lines(channel, remaining, None, sent=int(raw.get("sent") or 1))

    async def send_private_notice(self, chat_id: str, user_id: str | None, content: str, reply_to: str | None = None,
                                  metadata: dict[str, Any] | None = None) -> SendResult:
        """Hermes' own notices (the one-time "no home channel" prompt, subagent failures, credit lines, /login steps)
        are for the owner, and Twitch chat has no private place the bot can use: they go to the gateway log."""
        logger.info("Twitch: Hermes notice kept out of %s: %s", chat_id,
                    self._redact(one_line(self._redact(content)))[:300])
        return SendResult(success=True)

    async def send_typing(self, chat_id: str, metadata: Any = None) -> None:
        """Twitch has no typing indicator."""

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": irc.normalize_channel(chat_id) or chat_id, "type": "group"}

    def format_tool_event(self, event: Any, *, mode: str = "all", preview_max_len: int = 40) -> str | None:
        """Tool activity never reaches chat."""
        return None

    def supports_draft_streaming(self, chat_type: str | None = None, metadata: dict[str, Any] | None = None,
                                 chat_id: str | None = None) -> bool:
        return False


# registration helpers

def _private_notices(config: Any) -> None:
    """Hermes delivers its setup and operational notices through gateway/run_notifications.py
    _deliver_platform_notice, which reads ``notice_delivery`` from this adapter's config first and, when it is
    "private", hands the notice to send_private_notice instead of posting it (0.22.7 through main). On Twitch that
    is always the right mode: chat is public, viewers read it, and Twitch swallows the "/sethome" it suggests."""
    extra = getattr(config, "extra", None)
    if not isinstance(extra, dict):
        return
    if str(extra.get("notice_delivery", "private")).strip().lower() != "private":
        logger.warning("Twitch: ignoring notice_delivery=%r; Hermes notices never go to Twitch chat",
                       extra.get("notice_delivery"))
    extra["notice_delivery"] = "private"


def owner_toolsets() -> list[str] | None:
    """platform_toolsets.twitch plus OWNER_TOOLSETS. None keeps Hermes' own resolution, which turns every plugin
    toolset on unless known_plugin_toolsets.twitch names it: used when nothing is saved for twitch, when the saved
    list is Hermes' default composite, and when the config cannot be read."""
    from hermes_cli.config import load_config_readonly

    try:
        config = load_config_readonly() or {}
    except (OSError, ValueError, TypeError, AttributeError):
        logger.warning("Twitch: could not read platform_toolsets; the owner gets Hermes' default toolsets")
        return None
    if not privacy.explicit_toolsets(config):
        return None
    names = privacy.saved_toolsets(config) or []
    return names + [name for name in OWNER_TOOLSETS if name not in names]


def check_requirements() -> bool:
    """Stdlib only, so always importable."""
    return True


def validate_config(config: Any) -> bool:
    return bool(sender.read_token(config) and read_channels(config))


def is_connected(config: Any) -> bool:
    return validate_config(config)


def env_enablement() -> dict | None:
    """Seed ``extra`` from env when TWITCH_BOT_TOKEN and TWITCH_CHANNEL are set. The token is not copied. There is
    a home channel only when TWITCH_HOME_CHANNEL names one: cron output and restart notices never land in a live
    chat by default."""
    channels = irc.parse_channels(get_scoped_secret("TWITCH_CHANNEL", "") or "")
    if not (auth.strip_oauth_prefix(get_scoped_secret("TWITCH_BOT_TOKEN", "")) and channels):
        return None
    seed = seed_extra_from_env((), home_env="TWITCH_HOME_CHANNEL")
    if home := seed.get("home_channel"):
        if not (chat_id := irc.normalize_channel(home.get("chat_id", ""))):
            logger.warning("Twitch: ignoring TWITCH_HOME_CHANNEL=%r, not a channel login", home.get("chat_id"))
            seed.pop("home_channel")
        else:
            home["chat_id"] = chat_id
    return {"channel": ",".join(channels), **seed}
