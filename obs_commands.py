"""OBS chat commands: !brb and !back for moderators, !scene for the owner. !brb and !back answer only in the owner's
channel, so a moderator of another joined channel never reaches the owner's OBS, and none of them exists until OBS is
set up. Each reply is one short chat line, and anyone below a command's level gets no reply at all."""

from __future__ import annotations

import logging

from . import commands, obs, roles
from .commands import Available, Command, Handler
from .obs import ObsError

logger = logging.getLogger(__name__)

MAX_REPLY = 300


def _reply(text: str) -> str:
    return obs.clean(text, MAX_REPLY)


def _failed(command: str, exc: ObsError) -> str:
    """The chat line for a failure: the short reason only, never the endpoint or OBS's own comment."""
    logger.info("OBS: !%s failed: %s", command, exc)
    return _reply(exc.message[:1].upper() + exc.message[1:] + ".")


def _in_owner_channel(cmd: Command) -> bool:
    owner = roles.owner_channel().lstrip("#").lower()
    return bool(owner) and cmd.channel.lstrip("#").lower() == owner


def _allowed(cmd: Command, level: str, *, owner_channel_only: bool = False) -> bool:
    """``cmd``'s caller has ``level`` or above and, when asked, typed it in the owner's channel."""
    if roles.rank(cmd.level) < roles.rank(level):
        return False
    return not owner_channel_only or _in_owner_channel(cmd)


def obs_available(cmd: Command) -> bool:
    """The OBS commands exist only once the owner has set OBS up (see ``obs.configured``)."""
    return obs.configured()


def scene_keys_available(cmd: Command) -> bool:
    """!brb and !back exist only in the owner's channel, and only with OBS set up."""
    return _in_owner_channel(cmd) and obs.configured()


class SceneCommands:
    """!brb, !back and !scene. Remembers the scene !brb left so !back can return to it."""

    def __init__(self) -> None:
        self.left = ""

    async def brb(self, cmd: Command) -> str | None:
        if not _allowed(cmd, roles.MODERATOR, owner_channel_only=True):
            return None
        if not (target := obs.setting(obs.BRB_SCENE_ENV)):
            return f"No BRB scene is set; the streamer can set {obs.BRB_SCENE_ENV}."
        try:
            async with obs.session() as client:
                previous, scene = await obs.switch_scene(client, target)
        except ObsError as exc:
            return _failed("brb", exc)
        if previous == scene:
            return _reply(f"Already on {scene}.")
        self.left = previous
        return _reply(f"Switched to {scene}.")

    async def back(self, cmd: Command) -> str | None:
        if not _allowed(cmd, roles.MODERATOR, owner_channel_only=True):
            return None
        if not (target := obs.setting(obs.LIVE_SCENE_ENV) or self.left):
            return "No live scene is set and there is no scene to go back to."
        try:
            async with obs.session() as client:
                previous, scene = await obs.switch_scene(client, target)
        except ObsError as exc:
            return _failed("back", exc)
        self.left = ""
        return _reply(f"Already on {scene}." if previous == scene else f"Back to {scene}.")

    async def scene(self, cmd: Command) -> str | None:
        if not _allowed(cmd, roles.OWNER):
            return None
        name = str(cmd.args or "").strip()
        try:
            async with obs.session() as client:
                if not name:
                    current, names = await obs.scenes(client)
                    return _reply(f"Live: {current}. Scenes: {', '.join(names)}" if names else "OBS has no scenes.")
                previous, scene = await obs.switch_scene(client, name)
        except ObsError as exc:
            return _failed("scene", exc)
        return _reply(f"Already on {scene}." if previous == scene else f"Switched to {scene}.")


def chat_commands() -> dict[str, tuple[Handler, str, str, Available]]:
    """The OBS chat commands for ``commands.add_command``, ``name -> (handler, min_role, help, available)``.
    Each call returns handlers with their own remembered scene."""
    scenes = SceneCommands()
    return {
        "brb": (scenes.brb, roles.MODERATOR, "switch OBS to the BRB scene", scene_keys_available),
        "back": (scenes.back, roles.MODERATOR, "switch OBS back to the live scene", scene_keys_available),
        "scene": (scenes.scene, roles.OWNER, "switch OBS to a scene: !scene <name>; alone it lists them",
                  obs_available),
    }


def register_commands() -> None:
    """Add !brb, !back and !scene to the chat commands; register() calls this."""
    for name, (handler, min_role, text, available) in chat_commands().items():
        commands.add_command(name, handler, min_role, text, available)
