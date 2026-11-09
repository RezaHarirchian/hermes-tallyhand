"""Viewer roles per channel, learned from chat tags and held in a bounded LRU, and the process-level view of the
running adapter's roles that tools, hooks and chat commands read."""

from __future__ import annotations

import weakref
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

MAX_ENTRIES = 5000
# Access levels, lowest first. The owner is the broadcaster of the first configured channel; a moderator is a
# moderator or the broadcaster of the channel in question; everyone else is a viewer.
VIEWER, MODERATOR, OWNER = "viewer", "moderator", "owner"
LEVELS = (VIEWER, MODERATOR, OWNER)
# Surfaces where the person at the keyboard is the operator. CLI, TUI, desktop and ACP turns, A2A peers and group-chat
# members all run as gateway "local" routes; session_surface() tells them apart (cli, tui, desktop, acp, a2a, bot_room).
# Else HERMES_SESSION_PLATFORM, then HERMES_SESSION_SOURCE; neither is the classic CLI. Cron marks HERMES_CRON_SESSION.
LOCAL_SURFACES = frozenset({"cli", "oneshot", "tui", "desktop", "acp", "local"})
CRON = "cron"


@dataclass(slots=True)
class Role:
    broadcaster: bool = False
    moderator: bool = False

    @property
    def privileged(self) -> bool:
        return self.broadcaster or self.moderator


def role_from_tags(room_id: str, user_id: str, tags: Mapping[str, str], badges: Mapping[str, str]) -> Role:
    """Role flags for one message. Broadcaster means user-id == room-id or the broadcaster badge."""
    return Role(broadcaster=bool(user_id and user_id == room_id) or "broadcaster" in badges,
                moderator="moderator" in badges or tags.get("mod") == "1")


class RoleRegistry:
    """LRU of ``(room_id, user_id) -> Role``, refreshed from every chat line the viewer sends."""

    def __init__(self, max_entries: int = MAX_ENTRIES):
        self._roles: OrderedDict[tuple[str, str], Role] = OrderedDict()
        self._max = max_entries

    def __len__(self) -> int:
        return len(self._roles)

    def observe(self, room_id: str, user_id: str, tags: Mapping[str, str], badges: Mapping[str, str]) -> Role:
        role = role_from_tags(room_id, user_id, tags, badges)
        if room_id and user_id:
            key = (room_id, user_id)
            self._roles.pop(key, None)
            self._roles[key] = role
            while len(self._roles) > self._max:
                self._roles.popitem(last=False)
        return role

    def role_of(self, room_id: str, user_id: str) -> Role | None:
        return self._roles.get((room_id, user_id))

    def is_privileged(self, room_id: str, user_id: str) -> bool:
        """Broadcaster or moderator in that room, as last seen in chat."""
        role = self._roles.get((room_id, user_id))
        return bool(role and role.privileged)


def rank(level: str) -> int:
    """Position of ``level`` in LEVELS; -1 for an unknown level."""
    return LEVELS.index(level) if level in LEVELS else -1


# the running adapter, as seen by the rest of this process


class Live(Protocol):
    """What the connected TwitchAdapter exposes to tools, hooks and commands in its own process."""

    @property
    def roles(self) -> RoleRegistry: ...

    @property
    def helix(self) -> Any: ...

    def owner_id(self) -> str: ...

    def owner_channel(self) -> str: ...

    def room_id(self, channel: str) -> str: ...


_live: weakref.ref | None = None
_surface: Callable[[], str] | None = None


def bind(ctx: Any) -> None:
    """Keep the plugin context's ``session_surface`` for ``caller()``; register() calls this."""
    global _surface
    surface = getattr(ctx, "session_surface", None)
    _surface = surface if callable(surface) else None


def attach(adapter: Live) -> None:
    """Called by the adapter once it is connected."""
    global _live
    _live = weakref.ref(adapter)


def detach(adapter: Live) -> None:
    """Called by the adapter on disconnect; a newer adapter that attached since stays."""
    global _live
    if _live is not None and _live() is adapter:
        _live = None


def live() -> Live | None:
    """The connected adapter in this process, or None (a CLI or TUI process, or the gateway before connect)."""
    return _live() if _live is not None else None


def owner_channel() -> str:
    """Login of the owner's channel (the first in TWITCH_CHANNEL) without "#", from the running adapter."""
    adapter = live()
    return adapter.owner_channel() if adapter is not None else ""


def level_of(user_id: str | None, channel: str) -> str:
    """OWNER, MODERATOR or VIEWER for ``user_id`` in ``channel`` (``#`` optional), from the running adapter's
    registry: a moderator is one as last seen in that channel's chat. VIEWER without a running adapter."""
    adapter = live()
    if adapter is None or not user_id:
        return VIEWER
    if user_id == adapter.owner_id():
        return OWNER
    room = adapter.room_id(channel)
    return MODERATOR if room and adapter.roles.is_privileged(room, user_id) else VIEWER


@dataclass(frozen=True, slots=True)
class Caller:
    """Who the current tool call or hook runs for, from Hermes' session context."""

    platform: str  # HERMES_SESSION_PLATFORM, else HERMES_SESSION_SOURCE, lowercased; "cron" or "cli" when unbound
    user_id: str
    chat_id: str  # for a Twitch turn, the channel the question came from ("#name")
    single_query: bool = False  # hermes chat -q or hermes -z in its own process: nobody can answer a prompt

    @property
    def twitch(self) -> bool:
        return self.platform == "twitch"

    @property
    def local(self) -> bool:
        """The operator at a local surface (CLI, TUI, desktop app)."""
        return self.platform in LOCAL_SURFACES

    def level(self, channel: str | None = None) -> str:
        """The caller's level in ``channel`` (default: the turn's own channel). The local operator is OWNER; a
        Twitch asker is whatever the running adapter's roles say; anyone else is VIEWER."""
        if self.local:
            return OWNER
        if not self.twitch:
            return VIEWER
        return level_of(self.user_id, channel or self.chat_id)


def caller() -> Caller:
    """The Caller of the current turn, from Hermes' session context. A cron run is CRON; a turn that names neither
    a surface, a platform nor a source is the classic CLI, as Hermes reads it."""
    from gateway.session_context import get_session_env

    surface = str((_surface() if _surface is not None else "") or "")
    platform = (surface or get_session_env("HERMES_SESSION_PLATFORM", "")
                or get_session_env("HERMES_SESSION_SOURCE", "") or "").strip().lower()
    if get_session_env("HERMES_CRON_SESSION", "") == "1":
        platform = CRON
    return Caller(platform=platform or "cli", user_id=(get_session_env("HERMES_SESSION_USER_ID", "") or "").strip(),
                  chat_id=(get_session_env("HERMES_SESSION_CHAT_ID", "") or "").strip(),
                  single_query=get_session_env("HERMES_SINGLE_QUERY_SESSION", "") == "1")
