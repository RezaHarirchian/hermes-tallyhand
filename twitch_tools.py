"""The ``twitch`` toolset: stream info, clips, announcements, shoutouts and moderation through Helix. Every handler
checks its caller itself (the sandbox hook is the first check, this is the second) and returns a JSON string."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any

from . import auth, irc, roles
from .helix import ANNOUNCEMENT_COLORS, MAX_TIMEOUT, Helix, HelixError, User, format_duration, login_of, plain_text
from .privacy import FALSE_WORDS, TRUE_WORDS, truthy
from .sender import allowed_target

logger = logging.getLogger(__name__)

TOOLSET = "twitch"
STREAM_INFO, CREATE_CLIP, ANNOUNCE = "twitch_stream_info", "twitch_create_clip", "twitch_announce"
SHOUTOUT, TIMEOUT, BAN = "twitch_shoutout", "twitch_timeout", "twitch_ban"
UNBAN, DELETE_MESSAGE = "twitch_unban", "twitch_delete_message"
TOOL_NAMES = (STREAM_INFO, CREATE_CLIP, ANNOUNCE, SHOUTOUT, TIMEOUT, BAN, UNBAN, DELETE_MESSAGE)
MODERATOR_TOOLS = frozenset({STREAM_INFO, DELETE_MESSAGE, TIMEOUT, ANNOUNCE, SHOUTOUT, CREATE_CLIP})
DEFAULT_TIMEOUT = 600
MODERATOR_MAX_TIMEOUT = 600  # longer timeouts, bans and unbans are the owner's, after human approval
APPROVAL_REASON = 100  # characters of the reason quoted in an approval question
OTHER_PLATFORMS_SETTING = "tools_from_other_platforms"

_get_config: Callable[[str], Any] | None = None
_warned: set[str] = set()
_local_client: Helix | None = None


class BadArguments(ValueError):
    """The model passed arguments the tool cannot use."""


# configuration


def _scoped(name: str) -> str:
    from gateway.platforms._shared import get_scoped_secret

    return get_scoped_secret(name, "") or ""


def _env_token() -> str:
    return auth.strip_oauth_prefix(_scoped("TWITCH_BOT_TOKEN"))


def token_configured() -> bool:
    """check_fn: the tools are offered only where the bot token is set (or the adapter runs). Fails closed."""
    try:
        return roles.live() is not None or bool(_env_token())
    except Exception:  # same seam as Hermes' a2a check_fn: a broken lookup hides the tools
        return False


def other_platforms_allowed() -> bool:
    """The ``tools_from_other_platforms`` plugin setting (default false)."""
    value = _get_config(OTHER_PLATFORMS_SETTING) if _get_config is not None else None
    if value is not None and not isinstance(value, bool) and str(value).strip().lower() not in TRUE_WORDS | FALSE_WORDS:
        if repr(value) not in _warned:
            _warned.add(repr(value))
            logger.warning("Twitch: ignoring %s=%r, not true or false", OTHER_PLATFORMS_SETTING, value)
    return truthy(value, False)


def client() -> Helix:
    """The running adapter's Helix client in the gateway process; elsewhere one built from TWITCH_BOT_TOKEN."""
    global _local_client
    adapter = roles.live()
    if adapter is not None:
        return adapter.helix
    if _local_client is None:
        _local_client = Helix(_env_token)
    return _local_client


def served(channel: str) -> bool:
    """The bot joined ``channel`` (login without "#"), or it is the home channel or listed in TWITCH_CHANNEL."""
    adapter = roles.live()
    return bool(adapter is not None and adapter.room_id(channel)) or bool(allowed_target(channel))


def unserved_channel(tool: str, channel: str) -> str | None:
    """Why ``tool`` may not act in ``channel``: stream info only reads, everything else stays in served channels."""
    if tool == STREAM_INFO or served(channel):
        return None
    return f"{channel} is not a channel this bot serves; use TWITCH_HOME_CHANNEL or a channel in TWITCH_CHANNEL"


def default_channel() -> str:
    """The owner's channel: the first in TWITCH_CHANNEL."""
    if channel := roles.owner_channel():
        return channel
    channels = irc.parse_channels(_scoped("TWITCH_CHANNEL"))
    return channels[0].lstrip("#") if channels else ""


# arguments and authorisation


def channel_arg(args: dict) -> str:
    """The channel to act on: the ``channel`` argument, else the owner's channel."""
    raw = args.get("channel")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if channel := default_channel():
            return channel
        raise BadArguments("no channel given and TWITCH_CHANNEL is not set")
    if not (channel := login_of(str(raw))):
        raise BadArguments("channel must be a Twitch channel login")
    return channel


def moderator_timeout_cap(*, sentence: bool = False) -> str:
    """Shared wording for the moderator timeout ceiling. ``sentence=True`` for the hook's block line."""
    text = f"moderators can time someone out for at most {MODERATOR_MAX_TIMEOUT} seconds"
    return text[:1].upper() + text[1:] + "." if sentence else text


def timeout_seconds(args: dict) -> int:
    """The timeout's length. Hermes runs pre_tool_call on the raw arguments and turns a string into an integer
    only afterwards ("1e6" -> 1000000, tools/arg_coercion.py _coerce_number), so this reads a string the same
    way: the hook must judge the number the tool will get."""
    raw = args.get("seconds")
    if raw is None or raw == "":
        return DEFAULT_TIMEOUT
    if isinstance(raw, str):
        try:
            raw = float(raw)
        except (ValueError, OverflowError):
            raw = None
    if (isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(raw) or raw != int(raw)
            or not 1 <= raw <= MAX_TIMEOUT):
        raise BadArguments(f"seconds must be a whole number from 1 to {MAX_TIMEOUT}")
    return int(raw)


def refusal(tool: str, who: roles.Caller, channel: str, args: dict) -> str | None:
    """Why ``who`` may not run ``tool`` on ``channel``, or None. Twitch askers: the owner may run everything, a
    moderator of that channel the moderator tools (timeouts up to 600 s), anyone else nothing. Past stream info,
    which only reads, no one but the local operator acts in a channel the bot does not serve. The local operator may
    run everything; other platforms only with tools_from_other_platforms."""
    if who.local:
        return None
    unserved = unserved_channel(tool, channel)
    if who.twitch:
        level = who.level(channel)
        if level == roles.OWNER:
            return unserved
        if level == roles.MODERATOR and tool in MODERATOR_TOOLS:
            if tool == TIMEOUT and timeout_seconds(args) > MODERATOR_MAX_TIMEOUT:
                return moderator_timeout_cap()
            return None
        if tool in MODERATOR_TOOLS:
            return f"only the streamer or a moderator of {channel} may use {tool}"
        return f"only the streamer may use {tool}"
    if other_platforms_allowed():
        return unserved
    return (f"Twitch tools are off on {who.platform or 'this surface'}; the streamer can allow them with the "
            f"{OTHER_PLATFORMS_SETTING} plugin setting")


def approval(tool: str, args: dict) -> dict | None:
    """The pre_tool_call directive for what only a person may allow: a ban, an unban, or a timeout longer than
    MODERATOR_MAX_TIMEOUT get an approval request. Its rule key names the action, channel and person, so a
    "session" or "always" answer covers that one action, not every later ban. Arguments the hook cannot read are
    blocked instead of waved through: Hermes coerces them before the tool runs. None when nothing is needed."""
    if tool not in (BAN, UNBAN, TIMEOUT):
        return None
    try:
        channel, seconds = channel_arg(args), timeout_seconds(args) if tool == TIMEOUT else 0
        if not (login := login_of(str(args.get("login") or ""))):
            raise BadArguments("login must be a Twitch login")
    except BadArguments as exc:
        return {"action": "block", "message": f"{tool} refused: {exc}."}
    if tool == TIMEOUT and seconds <= MODERATOR_MAX_TIMEOUT:
        return None
    # Kept short: Hermes' prompt quotes this message in public chat, and it must stay within a few lines.
    reason = plain_text(str(args.get("reason") or ""), APPROVAL_REASON)
    action = {BAN: f"Ban {login} from #{channel}", UNBAN: f"Lift the ban or timeout on {login} in #{channel}",
              TIMEOUT: f"Time {login} out of #{channel} for {format_duration(seconds)}"}[tool]
    return {"action": "approve", "message": f"{action}{f' (reason: {reason})' if reason and tool != UNBAN else ''}?",
            "rule_key": ":".join([tool, channel, login, *([str(seconds)] if tool == TIMEOUT else [])])}


# actions


async def _broadcaster(api: Helix, channel: str) -> str:
    adapter = roles.live()
    if adapter is not None and (room := adapter.room_id(channel)):
        return room
    user = await api.user(channel)
    if user is None:
        raise BadArguments(f"there is no Twitch channel named {channel}")
    return user.id


async def _target(api: Helix, args: dict) -> User:
    if not (login := login_of(str(args.get("login") or ""))):
        raise BadArguments("login must be a Twitch login")
    user = await api.user(login)
    if user is None:
        raise BadArguments(f"there is no Twitch account named {login}")
    return user


async def _stream_info(api: Helix, channel: str, args: dict) -> dict:
    stream = await api.stream(await _broadcaster(api, channel))
    if stream is None:
        return {"live": False}
    uptime = stream.uptime()
    return {"live": True, "title": stream.title, "game": stream.game, "viewers": stream.viewers,
            "uptime": format_duration(uptime), "uptime_seconds": uptime}


async def _create_clip(api: Helix, channel: str, args: dict) -> dict:
    clip = await api.create_clip(await _broadcaster(api, channel))
    return {"clip_url": clip.url, "edit_url": clip.edit_url}


async def _announce(api: Helix, channel: str, args: dict) -> dict:
    if not (message := plain_text(str(args.get("message") or ""))):
        raise BadArguments("message is required")
    color = str(args.get("color") or "primary").strip().lower()
    if color not in ANNOUNCEMENT_COLORS:
        raise BadArguments(f"color must be one of {', '.join(ANNOUNCEMENT_COLORS)}")
    await api.announce(await _broadcaster(api, channel), message, color)
    return {"announced": message, "color": color}


async def _shoutout(api: Helix, channel: str, args: dict) -> dict:
    user = await _target(api, args)
    await api.shoutout(await _broadcaster(api, channel), user.id)
    return {"shoutout": user.login}


async def _timeout(api: Helix, channel: str, args: dict) -> dict:
    seconds, user = timeout_seconds(args), await _target(api, args)
    await api.ban(await _broadcaster(api, channel), user.id, duration=seconds, reason=str(args.get("reason") or ""))
    return {"timed_out": user.login, "seconds": seconds}


async def _ban(api: Helix, channel: str, args: dict) -> dict:
    user = await _target(api, args)
    await api.ban(await _broadcaster(api, channel), user.id, reason=str(args.get("reason") or ""))
    return {"banned": user.login}


async def _unban(api: Helix, channel: str, args: dict) -> dict:
    user = await _target(api, args)
    await api.unban(await _broadcaster(api, channel), user.id)
    return {"unbanned": user.login}


async def _delete_message(api: Helix, channel: str, args: dict) -> dict:
    message_id = str(args.get("message_id") or "").strip()
    if not irc.is_message_id(message_id):
        raise BadArguments("message_id must be a Twitch chat message id")
    await api.delete_message(await _broadcaster(api, channel), message_id)
    return {"deleted": message_id}


def _error(message: str, code: str) -> str:
    return json.dumps({"error": message, "code": code}, ensure_ascii=False)


def _handler(tool: str, action: Callable[[Helix, str, dict], Awaitable[dict]]) -> Callable[..., Awaitable[str]]:
    async def handle(args: dict | None = None, **_: Any) -> str:
        args = args if isinstance(args, dict) else {}
        try:
            channel = channel_arg(args)
            if reason := refusal(tool, roles.caller(), channel, args):
                return _error(reason, "not_allowed")
            result = await action(client(), channel, args)
        except BadArguments as exc:
            return _error(str(exc), "bad_arguments")
        except HelixError as exc:
            return _error(exc.message, exc.code)
        except Exception as exc:  # same seam as Hermes' a2a client tools: an outbound call never raises into the turn
            logger.warning("Twitch: %s failed (%s)", tool, type(exc).__name__)
            return _error(f"{tool} failed ({type(exc).__name__})", "failed")
        return json.dumps({"ok": True, "channel": channel, **result}, ensure_ascii=False)

    handle.__name__ = handle.__qualname__ = tool
    return handle


# registration

_CHANNEL = {"type": "string", "description": "Channel login. Leave it out for the streamer's own channel."}
_LOGIN = {"type": "string", "description": "Twitch login of the person, without @."}
_REASON = {"type": "string", "description": "Short reason, shown to the person and to moderators."}
TOOLS: dict[str, tuple[Callable[[Helix, str, dict], Awaitable[dict]], str, dict, list[str]]] = {
    STREAM_INFO: (_stream_info, "Whether a Twitch channel is live, with its title, game, viewer count and uptime.",
                  {"channel": _CHANNEL}, []),
    CREATE_CLIP: (_create_clip, "Clip the last moments of a live Twitch stream and return the clip link.",
                  {"channel": _CHANNEL}, []),
    ANNOUNCE: (_announce, "Post a highlighted announcement in Twitch chat.",
               {"message": {"type": "string", "description": "Announcement text, at most 500 characters."},
                "color": {"type": "string", "enum": list(ANNOUNCEMENT_COLORS),
                          "description": "Highlight color; primary is the channel's accent color."},
                "channel": _CHANNEL}, ["message"]),
    SHOUTOUT: (_shoutout, "Give another streamer a Twitch shoutout (the channel must be live).",
               {"login": _LOGIN, "channel": _CHANNEL}, ["login"]),
    TIMEOUT: (_timeout, "Time a viewer out of Twitch chat. Moderators can give up to 600 seconds; longer timeouts "
                        "need the operator's approval.",
              {"login": _LOGIN, "seconds": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT,
                                            "description": f"Length in seconds (default {DEFAULT_TIMEOUT})."},
               "reason": _REASON, "channel": _CHANNEL}, ["login"]),
    BAN: (_ban, "Ban a viewer from Twitch chat. Needs the operator's approval.",
          {"login": _LOGIN, "reason": _REASON, "channel": _CHANNEL}, ["login"]),
    UNBAN: (_unban, "Lift a ban or timeout in Twitch chat. Needs the operator's approval.",
            {"login": _LOGIN, "channel": _CHANNEL}, ["login"]),
    DELETE_MESSAGE: (_delete_message, "Delete one Twitch chat message by its message id.",
                     {"message_id": {"type": "string", "description": "The chat message id (a UUID)."},
                      "channel": _CHANNEL}, ["message_id"]),
}
HANDLERS = {name: _handler(name, action) for name, (action, *_rest) in TOOLS.items()}


def register_tools(ctx: Any) -> list[str]:
    """Register the ``twitch`` toolset with Hermes; returns the tool names."""
    global _get_config
    get = getattr(ctx, "get_config", None)
    _get_config = get if callable(get) else None
    for name, (_action, description, properties, required) in TOOLS.items():
        parameters: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            parameters["required"] = list(required)
        ctx.register_tool(name=name, toolset=TOOLSET, handler=HANDLERS[name], check_fn=token_configured,
                          is_async=True, description=description,
                          schema={"name": name, "description": description, "parameters": parameters})
    return list(TOOLS)
