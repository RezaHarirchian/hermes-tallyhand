"""Bot token checks against Twitch's validate endpoint: on connect, then hourly while connected."""

from __future__ import annotations

import asyncio
import functools
import http.client
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
REQUIRED_SCOPES = ("chat:read", "chat:edit")
REVALIDATE_SECONDS = 3600.0
EXPIRY_WARN_SECONDS = 7 * 86400
EXPIRY_WARN_EVERY = 86400.0
INVALID_TOKEN = "token is invalid or expired; generate a new one and set TWITCH_BOT_TOKEN"
# A Twitch user access token is 30 lowercase letters and digits. These shapes, its IRC "oauth:" form and the bare
# token after the validate endpoint's "OAuth " or Helix's "Bearer ", go to Hermes' redaction for logs and tool
# output, which masks each match down to its first six and last four characters.
REDACTION_PATTERNS = (r"oauth:[a-z0-9]{30}", r"OAuth [a-z0-9]{30}", r"Bearer [a-z0-9]{30}")
_MAX_BODY = 64 * 1024


def redact(text: str, token: str) -> str:
    """``text`` with ``token`` replaced by "[redacted]", also where punctuation, control or invisible characters
    split it (formatting can drop those and join the pieces again)."""
    return _token_pattern(token).sub("[redacted]", text) if token and text else text


@functools.lru_cache(maxsize=4)
def _token_pattern(token: str) -> re.Pattern:
    return re.compile(r"[^0-9A-Za-z\s]*".join(re.escape(ch) for ch in token))


class AuthError(Exception):
    """A failed validation. ``retryable`` is False for anything a retry cannot fix."""

    def __init__(self, code: str, message: str, *, retryable: bool):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


@dataclass(frozen=True, slots=True)
class TokenInfo:
    login: str
    user_id: str
    scopes: tuple[str, ...]
    expires_in: int
    client_id: str


def strip_oauth_prefix(token: str | None) -> str:
    """Bare token from a value that may carry the ``oauth:`` prefix."""
    token = (token or "").strip()
    return token[6:].strip() if token[:6].lower() == "oauth:" else token


def missing_scopes(scopes: tuple[str, ...] | list[str]) -> list[str]:
    return [scope for scope in REQUIRED_SCOPES if scope not in scopes]


def parse_validation(status: int, body: bytes) -> TokenInfo:
    """Turn a validate response into TokenInfo or raise AuthError."""
    if status == 401:
        raise AuthError("token_invalid", INVALID_TOKEN, retryable=False)
    if status >= 500 or status == 429:
        raise AuthError("validate_unavailable", f"Twitch token validation failed with HTTP {status}", retryable=True)
    if status != 200:
        raise AuthError("token_rejected", f"Twitch rejected the token (HTTP {status}); set TWITCH_BOT_TOKEN to a "
                        "user access token", retryable=False)
    try:
        data = json.loads(body.decode("utf-8"))
        info = TokenInfo(login=str(data.get("login") or "").lower(), user_id=str(data.get("user_id") or ""),
                         scopes=tuple(str(s) for s in data.get("scopes") or ()),
                         expires_in=int(data.get("expires_in") or 0), client_id=str(data.get("client_id") or ""))
    except (UnicodeDecodeError, ValueError, TypeError, AttributeError) as exc:
        raise AuthError("validate_unavailable", f"unreadable token validation response ({type(exc).__name__})",
                        retryable=True) from None
    if not info.login or not info.user_id:
        raise AuthError("token_not_user", "TWITCH_BOT_TOKEN is not a user access token (no login); generate a "
                        "user token for the bot account", retryable=False)
    if missing := missing_scopes(info.scopes):
        raise AuthError("token_scope", f"TWITCH_BOT_TOKEN is missing the {', '.join(missing)} scope; generate a "
                        "new token with chat:read and chat:edit", retryable=False)
    return info


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib copies the Authorization header onto a redirect to any host; a redirect is an error here instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def open_url(request: urllib.request.Request, timeout: float):
    """``urlopen`` that never follows a redirect, so a token only ever goes to the URL it was meant for."""
    return _OPENER.open(request, timeout=timeout)


def _fetch(url: str, token: str, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"Authorization": f"OAuth {token}"})
    try:
        with open_url(request, timeout) as response:
            return response.status, response.read(_MAX_BODY)
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read(_MAX_BODY) if exc.fp else b""


async def validate_token(token: str, *, url: str = VALIDATE_URL, timeout: float = 10.0) -> TokenInfo:
    """Validate ``token`` (``oauth:`` prefix optional). Raises AuthError; never puts the token in a message."""
    token = strip_oauth_prefix(token)
    if not token:
        raise AuthError("config_missing", "TWITCH_BOT_TOKEN is not set", retryable=False)
    if any(ch.isspace() or not ch.isprintable() for ch in token):
        raise AuthError("token_malformed", "TWITCH_BOT_TOKEN contains spaces or control characters; set it to the "
                        "token alone", retryable=False)
    loop = asyncio.get_running_loop()
    try:
        status, body = await loop.run_in_executor(None, _fetch, url, token, timeout)
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        reason = getattr(exc, "reason", None) or type(exc).__name__
        raise AuthError("validate_unavailable", f"could not reach the Twitch token validation endpoint ({reason})",
                        retryable=True) from None
    return parse_validation(status, body)


def expires_in(info: TokenInfo) -> str:
    """``5d 3h``: how long the token has left, as Twitch's validate endpoint reports it."""
    days, hours = divmod(info.expires_in // 3600, 24)
    return f"{days}d {hours}h"


class ExpiryNotice:
    """Rate-limits the "token expires soon" warning to once per day."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._last: float | None = None

    def check(self, info: TokenInfo) -> str | None:
        """Warning text when the token expires within 7 days and none was given in the last day."""
        if not 0 < info.expires_in < EXPIRY_WARN_SECONDS:
            return None
        now = self._clock()
        if self._last is not None and now - self._last < EXPIRY_WARN_EVERY:
            return None
        self._last = now
        return (f"Twitch bot token for {info.login} expires in {expires_in(info)}; generate a new one and update "
                "TWITCH_BOT_TOKEN before then")
