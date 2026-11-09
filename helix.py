"""Twitch Helix API client: urllib in the default executor, authorised by the bot token and the client id from its
own validation. Errors are short sentences that never carry the token, the client id or response headers."""

from __future__ import annotations

import asyncio
import datetime
import http.client
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import auth, irc

logger = logging.getLogger(__name__)

BASE_URL = "https://api.twitch.tv/helix"
TIMEOUT = 10.0
USER_TTL = 3600.0
MAX_USERS = 2000
STREAM_TTL = 30.0  # stream info is reused this long, so chat asking !uptime costs one Helix call per channel
MAX_STREAMS = 200
CHANNEL_TTL = 120.0  # channel info is reused this long; a changed title or tag takes up to two minutes to show
MAX_CHANNEL_LANGS = 200
MAX_RESET_WAIT = 60.0  # a 429 whose bucket resets later than this fails at once
DEFAULT_RESET_WAIT = 1.0  # a 429 without a usable Ratelimit-Reset waits this long
RETRY_DELAY = 1.0  # before a GET's one retry after a 5xx or a network error
# A POST or DELETE that got a 5xx or lost its connection may still have taken effect on Twitch's side, so it is not
# repeated (that would mean a second announcement, clip or shoutout); the caller is told to look first instead.
MAYBE_DONE = "; it may still have gone through, so check before trying again"
MAX_TIMEOUT = 1_209_600  # two weeks, Twitch's longest timeout
TEXT_LIMIT = 500  # announcements and ban reasons
_MAX_BODY = 256 * 1024
_LOGIN_RE = re.compile(r"[a-z0-9_]{1,25}")
_ID_RE = re.compile(r"[0-9]{1,20}")
_SPACE_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

SCOPE_CLIPS = "clips:edit"
SCOPE_BANS = "moderator:manage:banned_users"
SCOPE_CHAT_MESSAGES = "moderator:manage:chat_messages"
SCOPE_ANNOUNCEMENTS = "moderator:manage:announcements"
SCOPE_SHOUTOUTS = "moderator:manage:shoutouts"
ANNOUNCEMENT_COLORS = ("primary", "blue", "green", "orange", "purple")
# The features each scope turns on, as docs/twitch-tools.md lists them under Token scopes.
FEATURES = (
    ("Chat", auth.REQUIRED_SCOPES),
    ("Clips: !clip, twitch_create_clip, automatic clips", (SCOPE_CLIPS,)),
    ("Timeouts, bans and unbans", (SCOPE_BANS,)),
    ("Deleting messages", (SCOPE_CHAT_MESSAGES,)),
    ("Announcements", (SCOPE_ANNOUNCEMENTS,)),
    ("Shoutouts: !so, twitch_shoutout", (SCOPE_SHOUTOUTS,)),
)

# What a 403 means per scope. Every call checks its scope first and Twitch reports a missing scope as a 401, so a
# 403 is about the account or the channel, never the token.
_FORBIDDEN_HINTS = {
    SCOPE_CLIPS: "the channel may limit clips to followers or subscribers",
    SCOPE_BANS: "the bot account must be a moderator of the channel",
    SCOPE_CHAT_MESSAGES: "the bot account must be a moderator of the channel",
    SCOPE_ANNOUNCEMENTS: "the bot account must be a moderator of the channel",
    SCOPE_SHOUTOUTS: "the bot account must be a moderator of the channel",
}


class HelixError(Exception):
    """A failed Helix call. ``message`` is safe to show the owner and the model."""

    def __init__(self, code: str, message: str, *, status: int = 0, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.status, self.retryable = code, message, status, retryable


@dataclass(frozen=True, slots=True)
class User:
    id: str
    login: str
    display_name: str


@dataclass(frozen=True, slots=True)
class Stream:
    user_id: str
    login: str
    title: str
    game: str
    viewers: int
    started_at: float  # epoch seconds, 0 when Twitch gave no start time

    def uptime(self, now: float | None = None) -> int:
        """Seconds the stream has been live."""
        return max(0, int((time.time() if now is None else now) - self.started_at)) if self.started_at else 0


@dataclass(frozen=True, slots=True)
class Clip:
    id: str
    edit_url: str

    @property
    def url(self) -> str:
        return f"https://clips.twitch.tv/{self.id}"


def missing_by_feature(scopes: tuple[str, ...] | list[str]) -> list[tuple[str, list[str]]]:
    """Each feature with the scopes the token lacks for it (an empty list: the feature works)."""
    return [(feature, [scope for scope in needed if scope not in scopes]) for feature, needed in FEATURES]


def login_of(value: str | None) -> str:
    """``@Foo`` / ``#foo`` / ``Foo`` -> ``foo``; empty string when it is not a valid Twitch login."""
    login = (value or "").strip().lstrip("@#").lower()
    return login if _LOGIN_RE.fullmatch(login) else ""


def plain_text(value: str | None, limit: int = TEXT_LIMIT) -> str:
    """One line of chat-safe text: control characters dropped, whitespace folded, cut to ``limit``."""
    return _SPACE_RE.sub(" ", _CONTROL_RE.sub(" ", value or "")).strip()[:limit].rstrip()


def format_duration(seconds: int) -> str:
    """``8100`` -> ``2h 15m``; under a minute -> ``45s``."""
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if not (days or hours or minutes):
        return f"{secs}s"
    parts = [f"{days}d"] if days else []
    if hours or days:
        parts.append(f"{hours}h")
    return " ".join([*parts, f"{minutes}m"])


def _parse_time(value: Any) -> float:
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _request(method: str, url: str, headers: dict[str, str], body: bytes | None,
             timeout: float) -> tuple[int, str, str, bytes]:
    """One HTTP exchange: status, the Ratelimit-Reset and Ratelimit-Remaining values and the body. No other header
    leaves this function."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with auth.open_url(request, timeout) as response:
            limits = response.headers
            return (response.status, limits.get("Ratelimit-Reset", ""), limits.get("Ratelimit-Remaining", ""),
                    response.read(_MAX_BODY))
    except urllib.error.HTTPError as exc:
        with exc:
            limits = exc.headers if exc.headers is not None else {}
            return (exc.code, limits.get("Ratelimit-Reset", ""), limits.get("Ratelimit-Remaining", ""),
                    exc.read(_MAX_BODY) if exc.fp else b"")


class Helix:
    """Helix calls as the bot account. The token's validation (client id, user id, scopes) is cached and reused for
    up to an hour, as Twitch asks; a 401 re-validates once before the call fails. ``clock`` times the caches,
    ``wall`` reads Twitch's Ratelimit-Reset. Safe to share between event loops and threads."""

    def __init__(self, token: str | Callable[[], str], *, base_url: str = BASE_URL,
                 validate_url: str = auth.VALIDATE_URL, timeout: float = TIMEOUT,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep):
        self._token_source = token if callable(token) else (lambda: token)
        self.base_url, self.validate_url, self.timeout = base_url.rstrip("/"), validate_url, timeout
        self.clock, self.wall, self.sleep, self.retry_delay = clock, wall, sleep, RETRY_DELAY
        self._info: auth.TokenInfo | None = None
        self._info_token = ""
        self._info_at = 0.0
        self._users: OrderedDict[str, tuple[float, User]] = OrderedDict()
        self._streams: OrderedDict[str, tuple[float, Stream | None]] = OrderedDict()
        # broadcaster_id -> (fetched_at, broadcaster_language or "", title, tags). The title and tags come from
        # the same Get Channel Information response, so markers cost no extra call.
        self._languages: OrderedDict[str, tuple[float, str, str, tuple[str, ...]]] = OrderedDict()
        self._refreshing: set[str] = set()  # broadcaster ids with a language refresh already running
        self._lock = threading.Lock()

    # token

    def _token(self) -> str:
        return auth.strip_oauth_prefix(self._token_source())

    def remember(self, info: auth.TokenInfo, token: str | None = None) -> None:
        """Reuse a validation done elsewhere (the adapter's connect and hourly checks)."""
        with self._lock:
            self._info, self._info_token = info, auth.strip_oauth_prefix(token) if token else self._token()
            self._info_at = self.clock()

    @property
    def token_info(self) -> auth.TokenInfo | None:
        """The cached validation of the current token, if any."""
        with self._lock:
            return self._info if self._info is not None and self._info_token == self._token() else None

    async def validate(self, *, refresh: bool = False) -> auth.TokenInfo:
        """The token's validation, from the cache unless ``refresh``. Raises HelixError."""
        token = self._token()
        if not token:
            raise HelixError("config_missing", "TWITCH_BOT_TOKEN is not set")
        if not refresh and (cached := self.token_info) is not None and (
                self.clock() - self._info_at < auth.REVALIDATE_SECONDS):
            return cached
        try:
            info = await auth.validate_token(token, url=self.validate_url, timeout=self.timeout)
        except auth.AuthError as exc:
            if exc.code in ("token_invalid", "token_rejected"):
                with self._lock:
                    self._info = None
            raise HelixError(exc.code, exc.message, retryable=exc.retryable) from None
        self.remember(info, token)
        return info

    @staticmethod
    def _missing(scope: str) -> HelixError:
        return HelixError("missing_scope", f"the bot token lacks the {scope} scope; generate a new TWITCH_BOT_TOKEN "
                          f"with {scope} and restart the gateway")

    # transport

    @staticmethod
    def _redacted(text: str, token: str, info: auth.TokenInfo) -> str:
        for secret in (token, info.client_id):
            if secret:
                text = text.replace(secret, "[redacted]")
        return text

    def _scrub(self, text: str, token: str, info: auth.TokenInfo) -> str:
        return plain_text(self._redacted(text, token, info), 200)

    def _twitch_message(self, body: bytes, token: str, info: auth.TokenInfo) -> str:
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return ""
        return self._scrub(str(data.get("message") or ""), token, info) if isinstance(data, dict) else ""

    async def _call(self, method: str, path: str, params: dict[str, str | list[str]], *, scope: str = "",
                    payload: dict | None = None) -> Any:
        """Send one request with the retry rules (401 re-validates once, 429 waits for the reset once, a GET retries
        once after a 5xx or a network error, nothing else repeats) and return the decoded JSON body (None for an
        empty one). A list value repeats the parameter. Raises HelixError."""
        for key, values in params.items():
            for value in values if isinstance(values, list) else [values]:
                if key == "login":
                    valid = _LOGIN_RE.fullmatch(value)
                else:
                    valid = irc.is_message_id(value) if key == "message_id" else _ID_RE.fullmatch(value)
                if not valid:
                    kind = "login" if key == "login" else "id"
                    raise HelixError("bad_request", f"{key} is not a valid Twitch {kind}")
        info = await self.validate()
        if scope and scope not in info.scopes:
            raise self._missing(scope)
        url = f"{self.base_url}/{path}?{urllib.parse.urlencode(params, doseq=True)}"
        loop = asyncio.get_running_loop()
        revalidated = waited = retried = False
        repeatable = method == "GET"
        while True:
            token = self._token()
            headers = {"Authorization": f"Bearer {token}", "Client-Id": info.client_id, "Accept": "application/json"}
            body = None
            if payload is not None:
                # Free text (a reason, an announcement) never carries the token or the client id to Twitch.
                body = self._redacted(json.dumps(payload), token, info).encode("utf-8")
                headers["Content-Type"] = "application/json"
            try:
                status, reset, remaining, raw = await loop.run_in_executor(None, _request, method, url, headers,
                                                                           body, self.timeout)
            except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
                if repeatable and not retried:
                    retried = True
                    await self.sleep(self.retry_delay)
                    continue
                reason = self._scrub(str(getattr(exc, "reason", None) or type(exc).__name__), token, info)
                raise HelixError("unavailable", f"could not reach the Twitch API ({reason})"
                                 f"{'' if repeatable else MAYBE_DONE}", retryable=repeatable) from None
            logger.debug("Twitch: Helix %s %s -> %d", method, path, status)
            if 200 <= status < 300:
                try:
                    return json.loads(raw.decode("utf-8")) if raw.strip() else None
                except (UnicodeDecodeError, ValueError):
                    raise HelixError("unavailable", f"unreadable Twitch API response to {path}",
                                     status=status, retryable=True) from None
            if status == 401 and not revalidated:
                revalidated = True
                info = await self.validate(refresh=True)
                if scope and scope not in info.scopes:
                    raise self._missing(scope)
                continue
            if status == 401:
                raise HelixError("token_invalid", auth.INVALID_TOKEN, status=status)
            if status == 403:
                hint = _FORBIDDEN_HINTS.get(scope, "the bot account is not allowed to do this")
                reason = self._twitch_message(raw, token, info)
                raise HelixError("forbidden", f"Twitch refused this{f' ({reason})' if reason else ''}; {hint}",
                                 status=status)
            # Remaining points mean a per-feature cooldown (shoutouts), not an empty bucket: waiting cannot help.
            if status == 429 and not waited and remaining.strip() in ("", "0"):
                waited = True
                try:
                    wait = float(reset) - self.wall() if reset else DEFAULT_RESET_WAIT
                except ValueError:
                    wait = DEFAULT_RESET_WAIT
                if wait <= MAX_RESET_WAIT:
                    await self.sleep(max(0.0, wait))
                    continue
            if status == 429:
                detail = self._twitch_message(raw, token, info)
                raise HelixError("rate_limited", detail or "Twitch API rate limit reached; try again in a minute",
                                 status=status, retryable=True)
            if status >= 500 and repeatable and not retried:
                retried = True
                await self.sleep(self.retry_delay)
                continue
            if status >= 500:
                raise HelixError("unavailable", f"the Twitch API failed (HTTP {status})"
                                 f"{'' if repeatable else MAYBE_DONE}", status=status, retryable=repeatable)
            detail = self._twitch_message(raw, token, info)
            code = {400: "bad_request", 404: "not_found", 409: "conflict", 422: "unprocessable"}.get(status, "failed")
            raise HelixError(code, detail or f"Twitch rejected the request (HTTP {status})", status=status)

    @staticmethod
    def _rows(data: Any) -> list[dict]:
        rows = data.get("data") if isinstance(data, dict) else None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    # endpoints

    async def bot_id(self) -> str:
        """The bot account's user id (the moderator_id of every moderation call)."""
        return (await self.validate()).user_id

    async def user(self, login: str) -> User | None:
        """GET users by login, cached for USER_TTL seconds; None when no such account exists."""
        if not (login := login_of(login)):
            return None
        now = self.clock()
        with self._lock:
            if (hit := self._users.get(login)) is not None and now - hit[0] < USER_TTL:
                self._users.move_to_end(login)
                return hit[1]
        rows = self._rows(await self._call("GET", "users", {"login": login}))
        if not rows or not _ID_RE.fullmatch(str(rows[0].get("id") or "")):
            return None
        return self._cache_user(login, rows[0], now)

    async def users(self, logins: list[str]) -> dict[str, User]:
        """GET users for many logins, up to 100 per call, cached like ``user``; login -> User for the ones that
        exist. Invalid logins are skipped."""
        wanted = list(dict.fromkeys(login for value in logins if (login := login_of(value))))
        found: dict[str, User] = {}
        now = self.clock()
        with self._lock:
            for login in wanted:
                if (hit := self._users.get(login)) is not None and now - hit[0] < USER_TTL:
                    found[login] = hit[1]
        missing = [login for login in wanted if login not in found]
        for start in range(0, len(missing), 100):
            batch = missing[start:start + 100]
            for row in self._rows(await self._call("GET", "users", {"login": batch})):
                login = str(row.get("login") or "").lower()
                if login in batch and _ID_RE.fullmatch(str(row.get("id") or "")):
                    found[login] = self._cache_user(login, row, now)
        return found

    def _cache_user(self, login: str, row: dict, now: float) -> User:
        user = User(id=str(row["id"]), login=str(row.get("login") or login).lower(),
                    display_name=plain_text(str(row.get("display_name") or login), 25) or login)
        with self._lock:
            self._users.pop(login, None)
            self._users[login] = (now, user)
            while len(self._users) > MAX_USERS:
                self._users.popitem(last=False)
        return user

    async def stream(self, broadcaster_id: str) -> Stream | None:
        """GET streams for one broadcaster, reused for STREAM_TTL seconds; None when they are offline."""
        now = self.clock()
        with self._lock:
            if (hit := self._streams.get(broadcaster_id)) is not None and now - hit[0] < STREAM_TTL:
                return hit[1]
        rows = self._rows(await self._call("GET", "streams", {"user_id": broadcaster_id}))
        live = [row for row in rows if row.get("type", "live") == "live"]
        stream = None
        if live:
            row = live[0]
            try:
                viewers = int(row.get("viewer_count") or 0)
            except (TypeError, ValueError):
                viewers = 0
            stream = Stream(user_id=str(row.get("user_id") or broadcaster_id), login=str(row.get("user_login") or ""),
                            title=plain_text(str(row.get("title") or ""), 200),
                            game=plain_text(str(row.get("game_name") or ""), 100),
                            viewers=viewers, started_at=_parse_time(row.get("started_at")))
        with self._lock:
            self._streams.pop(broadcaster_id, None)
            self._streams[broadcaster_id] = (now, stream)
            while len(self._streams) > MAX_STREAMS:
                self._streams.popitem(last=False)
        return stream

    def cached_channel_language(self, broadcaster_id: str) -> str | None:
        """Last ``broadcaster_language`` for this channel, or None when never fetched. Sync; a turn never waits."""
        if not _ID_RE.fullmatch(broadcaster_id or ""):
            return None
        with self._lock:
            hit = self._languages.get(broadcaster_id)
        return None if hit is None else hit[1]

    def cached_channel_info(self, broadcaster_id: str) -> tuple[str | None, str, list[str]]:
        """Last (``broadcaster_language``, title, tags) for this channel; empties when never fetched. Sync."""
        if not _ID_RE.fullmatch(broadcaster_id or ""):
            return None, "", []
        with self._lock:
            hit = self._languages.get(broadcaster_id)
        if hit is None:
            return None, "", []
        return hit[1], hit[2], list(hit[3])

    def language_stale(self, broadcaster_id: str) -> bool:
        """True when this channel's language was never fetched or the cache is older than CHANNEL_TTL."""
        if not _ID_RE.fullmatch(broadcaster_id or ""):
            return False
        with self._lock:
            hit = self._languages.get(broadcaster_id)
        return hit is None or self.clock() - hit[0] >= CHANNEL_TTL

    async def refresh_channel_language(self, broadcaster_id: str) -> str | None:
        """GET channels for ``broadcaster_language``, title and tags, and store them. No special scope: any valid
        user or app token works (https://dev.twitch.tv/docs/api/reference/#get-channel-information). A refresh
        already running for the channel is not started twice. Returns the stored language value (possibly
        ``other`` or empty on failure); never raises to the turn path."""
        if not _ID_RE.fullmatch(broadcaster_id or ""):
            return None
        with self._lock:
            if broadcaster_id in self._refreshing:
                hit = self._languages.get(broadcaster_id)
                return hit[1] or None if hit is not None else None
            self._refreshing.add(broadcaster_id)
        try:
            return await self._fetch_channel_language(broadcaster_id)
        finally:
            with self._lock:
                self._refreshing.discard(broadcaster_id)

    async def _fetch_channel_language(self, broadcaster_id: str) -> str | None:
        """One channels fetch and cache write. Only called with the id held in ``_refreshing``."""
        now = self.clock()
        code, title, tags = "", "", ()
        try:
            rows = self._rows(await self._call("GET", "channels", {"broadcaster_id": broadcaster_id}))
            row = rows[0] if rows else {}
            raw = str(row.get("broadcaster_language") or "").strip().lower()
            code = raw if raw else ""
            title = str(row.get("title") or "")[:500]
            tags = tuple(t[:64] for t in row.get("tags", []) if isinstance(t, str))[:16]
        except HelixError as exc:
            logger.warning("Twitch: channel language lookup failed (%s)", exc.code)
            with self._lock:
                # Keep a usable value on a failed refresh, and bump its time so every turn does not spawn another
                # call while Twitch is down. Only record an empty miss when nothing usable is cached yet.
                if (hit := self._languages.get(broadcaster_id)) is not None and hit[1]:
                    self._languages[broadcaster_id] = (now, *hit[1:])
                    return hit[1]
        with self._lock:
            self._languages.pop(broadcaster_id, None)
            self._languages[broadcaster_id] = (now, code, title, tags)
            while len(self._languages) > MAX_CHANNEL_LANGS:
                self._languages.popitem(last=False)
        return code or None

    async def create_clip(self, broadcaster_id: str) -> Clip:
        """POST clips. Twitch answers before the clip is processed; its page works a few seconds later."""
        rows = self._rows(await self._call("POST", "clips", {"broadcaster_id": broadcaster_id}, scope=SCOPE_CLIPS))
        if not rows or not rows[0].get("id"):
            raise HelixError("unavailable", "Twitch did not return a clip id", retryable=True)
        return Clip(id=urllib.parse.quote(str(rows[0]["id"]), safe="-_"), edit_url=str(rows[0].get("edit_url") or ""))

    async def ban(self, broadcaster_id: str, user_id: str, *, duration: int | None = None, reason: str = "") -> dict:
        """POST moderation/bans; with ``duration`` (1 to 1209600 seconds) it is a timeout."""
        data: dict[str, Any] = {"user_id": user_id}
        if duration is not None:
            if not 1 <= int(duration) <= MAX_TIMEOUT:
                raise HelixError("bad_request", f"a timeout lasts 1 to {MAX_TIMEOUT} seconds")
            data["duration"] = int(duration)
        if reason := plain_text(reason):
            data["reason"] = reason
        params = {"broadcaster_id": broadcaster_id, "moderator_id": await self.bot_id()}
        rows = self._rows(await self._call("POST", "moderation/bans", params, scope=SCOPE_BANS,
                                           payload={"data": data}))
        return rows[0] if rows else {}

    async def unban(self, broadcaster_id: str, user_id: str) -> None:
        """DELETE moderation/bans: lifts a ban or a timeout."""
        params = {"broadcaster_id": broadcaster_id, "moderator_id": await self.bot_id(), "user_id": user_id}
        await self._call("DELETE", "moderation/bans", params, scope=SCOPE_BANS)

    async def delete_message(self, broadcaster_id: str, message_id: str) -> None:
        """DELETE moderation/chat for one message id."""
        params = {"broadcaster_id": broadcaster_id, "moderator_id": await self.bot_id(), "message_id": message_id}
        await self._call("DELETE", "moderation/chat", params, scope=SCOPE_CHAT_MESSAGES)

    async def announce(self, broadcaster_id: str, message: str, color: str = "primary") -> None:
        """POST chat/announcements."""
        if not (message := plain_text(message)):
            raise HelixError("bad_request", "an announcement needs some text")
        params = {"broadcaster_id": broadcaster_id, "moderator_id": await self.bot_id()}
        color = color if color in ANNOUNCEMENT_COLORS else "primary"
        await self._call("POST", "chat/announcements", params, scope=SCOPE_ANNOUNCEMENTS,
                         payload={"message": message, "color": color})

    async def shoutout(self, from_broadcaster_id: str, to_broadcaster_id: str) -> None:
        """POST chat/shoutouts. Twitch allows one every 2 minutes, and one per target per hour."""
        params = {"from_broadcaster_id": from_broadcaster_id, "to_broadcaster_id": to_broadcaster_id,
                  "moderator_id": await self.bot_id()}
        await self._call("POST", "chat/shoutouts", params, scope=SCOPE_SHOUTOUTS)
