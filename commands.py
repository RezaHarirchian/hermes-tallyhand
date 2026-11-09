"""Deterministic chat commands (``!uptime``, ``!so <login>``, ...): answered by the adapter itself, with no model
call and no turn slot. Other parts of the plugin add their own with ``add_command``."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .helix import Helix, HelixError, format_duration, login_of
from .privacy import Report
from .roles import LEVELS, MODERATOR, OWNER, VIEWER, Role, rank

logger = logging.getLogger(__name__)

# The !privacy answer, built from these parts by privacy_line(); every part is true for the settings it is used with.
PRIVACY_NOTES = "I am an automated assistant and take notes only with !remember."
PRIVACY_OLD_NOTES = "I am an automated assistant and take no new notes."
PRIVACY_NO_NOTES = "I am an automated assistant and keep no notes about viewers."
PRIVACY_HISTORY = "Your questions go to the streamer's AI model and stay in their Hermes history."
PRIVACY_PROVIDER = "Your questions go to the streamer's AI model and memory service and stay in their Hermes history."
PRIVACY_TAIL = "Recent chat lines go along too."
PRIVACY_FORGET = {  # (notes file present, per-viewer sessions)
    (True, True): "!forgetme deletes your notes and our conversation in this channel.",
    (True, False): "!forgetme deletes your notes in this channel.",
    (False, True): "!forgetme deletes our conversation in this channel.",
    (False, False): "",
}
PRIVACY_FORGET_PROVIDER = {  # the same with a memory service, whose copy !forgetme cannot reach
    (True, True): "!forgetme deletes your notes and our conversation here, not the memory service's copy.",
    (False, True): "!forgetme deletes our conversation here, not the memory service's copy.",
}
BOT_LINE = ("I am an automated assistant run by {channel}, using Tallyhand. I am not a person. Ask me something with "
            "!hermes followed by your question.")
COMMAND_COOLDOWN = 15.0  # seconds between two commands from one viewer; moderators and the streamer are exempt
# Seconds before viewers get the same command answered again in a channel: a crowd typing !uptime gets one answer,
# not one per account, so it cannot use up the bot's chat and Helix budgets. Moderators and the streamer are exempt.
CHANNEL_COOLDOWN = 30.0
# Which cooldowns hold a viewer's command: the per-viewer one shared by all commands plus the channel-wide one (the
# default); only a per-viewer one of the command's own, for a command about the caller's own data; or none.
COOLDOWN_SHARED, COOLDOWN_OWN, COOLDOWN_NONE = "shared", "own", "none"
_NAME_RE = re.compile(r"[a-z0-9_]{1,25}")


@dataclass(slots=True)
class Command:
    """One chat command as a handler sees it."""

    name: str
    args: str  # the text after the command
    channel: str  # channel login, without "#"
    user_id: str
    login: str
    display_name: str
    role: Role
    is_owner: bool
    message_id: str = ""
    room_id: str = ""  # the channel's broadcaster id
    helix: Helix | None = field(default=None, repr=False)
    notes: Any = field(default=None, repr=False)  # the adapter's ViewerNotes, None when viewer_notes is off
    chat_tail: int = 0  # the adapter's chat_tail setting
    privacy: Report = field(default_factory=Report)  # the adapter's report from connect
    may_ask: bool = False  # Hermes would take a question from this caller (its allowlist)
    commands_on: bool = True  # the chat_commands setting; off, only the commands registered always=True exist

    @property
    def level(self) -> str:
        return OWNER if self.is_owner else MODERATOR if self.role.privileged else VIEWER


Handler = Callable[[Command], Awaitable[str | None]]
Available = Callable[[Command], bool]


@dataclass(frozen=True, slots=True)
class Spec:
    name: str
    handler: Handler
    min_role: str
    help: str
    available: Available | None = None  # False: the command does not exist for this caller here
    cooldown: str = COOLDOWN_SHARED
    always: bool = False  # answered even with chat_commands off


_registry: dict[str, Spec] = {}


def _clean_name(name: str) -> str:
    clean = str(name or "").strip().lstrip("!").lower()
    if not _NAME_RE.fullmatch(clean):
        raise ValueError(f"command name must be 1 to 25 letters, digits or underscores: {name!r}")
    return clean


def add_command(name: str, handler: Handler, min_role: str = VIEWER, help: str = "",
                     available: Available | None = None, *, cooldown: str = COOLDOWN_SHARED,
                     always: bool = False) -> None:
    """Add (or replace) the chat command ``!name``. ``handler(cmd)`` is a coroutine function returning the text to
    post in chat, or None to post nothing. Callers below ``min_role`` ("viewer", "moderator" or "owner") are
    ignored silently, and so is everyone where ``available(cmd)`` is False; ``!commands`` lists neither.
    ``cooldown`` is COOLDOWN_SHARED, COOLDOWN_OWN or COOLDOWN_NONE; ``always`` keeps the command with
    chat_commands off."""
    if min_role not in LEVELS:
        raise ValueError(f"min_role must be one of {', '.join(LEVELS)}: {min_role!r}")
    if not callable(handler):
        raise TypeError("handler must be callable")
    if available is not None and not callable(available):
        raise TypeError("available must be callable")
    if cooldown not in (COOLDOWN_SHARED, COOLDOWN_OWN, COOLDOWN_NONE):
        raise ValueError(f"cooldown must be one of shared, own, none: {cooldown!r}")
    clean = _clean_name(name)
    _registry[clean] = Spec(clean, handler, min_role, str(help or ""), available, cooldown, bool(always))


def registered() -> dict[str, Spec]:
    """The registered commands by name (a copy)."""
    return dict(_registry)


def parse(text: str) -> tuple[Spec, str] | None:
    """``(spec, args)`` when ``text`` starts with a registered ``!command``, else None."""
    if not text.startswith("!"):
        return None
    head, _, args = text[1:].partition(" ")
    spec = _registry.get(head.lower())
    return (spec, args.strip()) if spec is not None else None


def allowed(spec: Spec, cmd: Command) -> bool:
    """``cmd``'s caller has the command's level and the command exists for them here."""
    return ((cmd.commands_on or spec.always) and rank(cmd.level) >= rank(spec.min_role)
            and (spec.available is None or bool(spec.available(cmd))))


# built-in commands


async def _uptime(cmd: Command) -> str | None:
    if cmd.helix is None or not cmd.room_id:
        return None
    try:
        stream = await cmd.helix.stream(cmd.room_id)
    except HelixError as exc:
        logger.warning("Twitch: !uptime in %s failed: %s", cmd.channel, exc.message)
        return None
    if stream is None:
        return f"{cmd.channel} is offline right now."
    return f"{cmd.channel} has been live for {format_duration(stream.uptime())}."


async def _bot(cmd: Command) -> str:
    return BOT_LINE.format(channel=cmd.channel)


async def _commands(cmd: Command) -> str:
    names = sorted(f"!{spec.name}" for spec in _registry.values() if allowed(spec, cmd))
    return "Commands: " + ", ".join(names)


def privacy_line(notes: Any = None, chat_tail: int = 0, report: Report | None = None) -> str:
    """What the bot keeps about viewers, in one chat line: viewer notes (taken, not taken any more while old ones
    are still deletable, or none), Hermes' own history of questions (and an external memory provider when one is
    set), the chat tail when it is on, and what !forgetme deletes."""
    report = report or Report()
    shared, external = report.shared_sessions, report.external_memory
    if notes is None:
        first = PRIVACY_NO_NOTES
    else:  # !remember needs the notes on and a session of the viewer's own
        first = PRIVACY_NOTES if notes.enabled and not shared else PRIVACY_OLD_NOTES
    forget_key = (notes is not None, not shared)
    forget = PRIVACY_FORGET_PROVIDER.get(forget_key, PRIVACY_FORGET[forget_key]) if external else (
        PRIVACY_FORGET[forget_key])
    history = PRIVACY_PROVIDER if external else PRIVACY_HISTORY
    return " ".join(part for part in (first, history, PRIVACY_TAIL if chat_tail > 0 else "", forget) if part)


async def _privacy(cmd: Command) -> str:
    return privacy_line(cmd.notes, cmd.chat_tail, cmd.privacy)


async def _shoutout(cmd: Command) -> str | None:
    target = login_of(cmd.args.split()[0]) if cmd.args.split() else ""
    if not target:
        return "Usage: !so <login>"
    if cmd.helix is None or not cmd.room_id:
        return None
    try:
        user = await cmd.helix.user(target)
    except HelixError as exc:
        logger.warning("Twitch: !so in %s failed: %s", cmd.channel, exc.message)
        return None
    if user is None:
        return f"There is no Twitch account named {target}."
    if user.id == cmd.room_id:
        return None
    try:
        await cmd.helix.shoutout(cmd.room_id, user.id)
    except HelixError as exc:  # the chat line still goes out; Twitch's own shoutout is the extra
        logger.warning("Twitch: Twitch shoutout for %s in %s failed: %s", user.login, cmd.channel, exc.message)
    return f"Go check out {user.display_name} at https://twitch.tv/{user.login}"


async def _clip(cmd: Command) -> str | None:
    if cmd.helix is None or not cmd.room_id:
        return None
    try:
        clip = await cmd.helix.create_clip(cmd.room_id)
    except HelixError as exc:
        if exc.code == "not_found":
            return "Clips work only while the stream is live."
        logger.warning("Twitch: !clip in %s failed: %s", cmd.channel, exc.message)
        return "Twitch did not make a clip this time."
    return f"Clip: {clip.url}"


BUILTINS: dict[str, tuple[Handler, str, str]] = {
    "uptime": (_uptime, VIEWER, "how long the stream has been live"),
    "bot": (_bot, VIEWER, "what this bot is"),
    "commands": (_commands, VIEWER, "the commands you can use"),
    "privacy": (_privacy, VIEWER, "what the bot stores about viewers"),
    "so": (_shoutout, MODERATOR, "shout out another channel: !so <login>"),
    "clip": (_clip, MODERATOR, "clip the last moments of the stream"),
}
ALWAYS = frozenset({"privacy"})  # a viewer can always ask what is kept about them


def register_builtins() -> None:
    for name, (handler, min_role, text) in BUILTINS.items():
        add_command(name, handler, min_role, text, always=name in ALWAYS)


register_builtins()
