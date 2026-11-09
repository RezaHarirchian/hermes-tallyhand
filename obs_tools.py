"""OBS tools for the owner's agent turns, in toolset ``obs``. Each handler checks its caller (the sandbox hook covers
Twitch turns first), opens one short-lived obs-websocket connection, returns JSON and never raises. Starting or
stopping the stream or a recording is deliberately not a tool."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from . import irc, obs, roles, twitch_tools
from .obs import ObsError
from .triggers import one_line

logger = logging.getLogger(__name__)

OBS_TOOLSET = "obs"
CAPTION_LIMIT = 240
CAPTION_SECONDS, CAPTION_MIN_SECONDS, CAPTION_MAX_SECONDS = 12, 3, 60


def refusal(who: roles.Caller) -> str | None:
    """Why ``who`` may not use the OBS tools, or None. The local operator and the owner's Twitch turns may; any
    other Twitch asker may not; other platforms, A2A, group chats and cron only with tools_from_other_platforms."""
    if who.local:
        return None
    if who.twitch:
        return None if who.level() == roles.OWNER else "only the streamer may control OBS"
    if twitch_tools.other_platforms_allowed():
        return None
    return (f"OBS tools are off on {who.platform or 'this surface'}; the streamer can allow them with the "
            f"{twitch_tools.OTHER_PLATFORMS_SETTING} plugin setting")


def caption_text(text: str, limit: int = CAPTION_LIMIT) -> str:
    """Caption text for the stream: one line without control characters, cut on a word boundary to at most
    ``limit`` characters, ending in "..." when cut."""
    text = one_line(text)
    return text if len(text) <= limit else irc.fit(text, limit - 3).rstrip(" .,;:-") + "..."


class CaptionTimer:
    """Clears this process's latest caption after its time unless a newer caption or a clear came first. The timer
    is a daemon thread, so nothing is cleared once the process has exited."""

    def __init__(self, timer: Callable[..., Any] = threading.Timer):
        self.timer = timer
        self._lock = threading.Lock()
        self._generation = 0
        self._pending: Any = None

    def start(self, seconds: float, config: obs.ObsConfig, source: str, text: str) -> None:
        """Replace any pending clear with one for ``text`` in ``seconds``."""
        with self._lock:
            self._drop()
            pending = self.timer(seconds, self._expire, args=(self._generation, config, source, text))
            pending.daemon = True
            pending.start()
            self._pending = pending

    def cancel(self) -> None:
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        self._generation += 1
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None

    def _expire(self, generation: int, config: obs.ObsConfig, source: str, text: str) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._pending = None
        try:
            asyncio.run(_clear_if(config, source, text))
        except ObsError as exc:
            logger.info("OBS: could not clear the caption: %s", exc)


async def _clear_if(config: obs.ObsConfig, source: str, text: str) -> None:
    async with obs.session(config) as client:
        await obs.clear_text_if(client, source, text)


CAPTION = CaptionTimer()


def _name(args: dict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ObsError("bad_arguments", f"{key} is required")
    return value.strip()


def _state(args: dict, key: str, *, toggle: bool = False) -> bool | None:
    """True or False from a boolean or "true"/"false"; None for "toggle" where that is allowed."""
    value = args.get(key)
    if isinstance(value, bool):
        return value
    word = value.strip().lower() if isinstance(value, str) else ""
    if word in ("true", "false"):
        return word == "true"
    if toggle and word == "toggle":
        return None
    raise ObsError("bad_arguments", f"{key} must be true or false" + (' or "toggle"' if toggle else ""))


def _seconds(value: Any) -> int:
    """Caption time: 12 when not given, otherwise clamped to 3..60."""
    if value is None:
        return CAPTION_SECONDS
    try:
        seconds = math.nan if isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        seconds = math.nan
    if not math.isfinite(seconds):
        raise ObsError("bad_arguments",
                       f"seconds must be a number from {CAPTION_MIN_SECONDS} to {CAPTION_MAX_SECONDS}")
    return int(min(CAPTION_MAX_SECONDS, max(CAPTION_MIN_SECONDS, round(seconds))))


def _caption_source() -> str:
    return obs.setting(obs.CAPTION_SOURCE_ENV, obs.DEFAULT_CAPTION_SOURCE)


async def _status(args: dict) -> dict:
    async with obs.session() as client:
        return await obs.status(client)


async def _list_scenes(args: dict) -> dict:
    async with obs.session() as client:
        current, names = await obs.scenes(client)
    return {"live_scene": current, "scenes": names}


async def _switch_scene(args: dict) -> dict:
    name = _name(args, "scene")
    async with obs.session() as client:
        previous, scene = await obs.switch_scene(client, name)
    return {"scene": scene, "previous": previous, "changed": previous != scene}


async def _list_audio_inputs(args: dict) -> dict:
    async with obs.session() as client:
        return {"inputs": await obs.audio_inputs(client)}


async def _set_mute(args: dict) -> dict:
    name, muted = _name(args, "input"), _state(args, "muted", toggle=True)
    async with obs.session() as client:
        name, muted = await obs.set_mute(client, name, muted)
    return {"input": name, "muted": muted}


async def _show_caption(args: dict) -> dict:
    text = caption_text(_name(args, "text"))
    if not text:
        raise ObsError("bad_arguments", "text has nothing to show")
    seconds, config, source = _seconds(args.get("seconds")), obs.load_config(), _caption_source()
    async with obs.session(config) as client:
        await obs.set_text(client, source, text)
    CAPTION.start(seconds, config, source, text)
    return {"caption": text, "seconds": seconds, "source": source}


async def mirror(text: str) -> bool:
    """Show ``text`` as the caption for CAPTION_SECONDS, for the caption_answers setting: fire-and-forget, so an OBS
    failure is only logged. False when nothing was shown."""
    caption = caption_text(text)
    if not caption:
        return False
    try:
        config, source = obs.load_config(), _caption_source()
        async with obs.session(config) as client:
            await obs.set_text(client, source, caption)
    except ObsError as exc:
        logger.info("OBS: answer caption not shown: %s", exc)
        return False
    CAPTION.start(CAPTION_SECONDS, config, source, caption)
    return True


async def _clear_caption(args: dict) -> dict:
    source = _caption_source()
    async with obs.session() as client:
        await obs.set_text(client, source, "")
    CAPTION.cancel()
    return {"source": source}


async def _set_source_visible(args: dict) -> dict:
    scene, source, visible = _name(args, "scene"), _name(args, "source"), _state(args, "visible")
    async with obs.session() as client:
        scene, source = await obs.set_source_visible(client, scene, source, visible)
    return {"scene": scene, "source": source, "visible": visible}


async def _save_replay(args: dict) -> dict:
    async with obs.session() as client:
        await obs.save_replay(client)
    return {"saved": True}


def _handler(name: str, run: Callable[[dict], Awaitable[dict]]) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_context: Any) -> str:
        try:
            if reason := refusal(roles.caller()):
                result = {"success": False, "error": reason, "code": "not_allowed"}
            else:
                result = {"success": True, **await run(args if isinstance(args, dict) else {})}
        except ObsError as exc:
            result = {"success": False, "error": str(exc), "code": exc.code}
        except Exception as exc:  # Hermes' registry catches everything at this seam too; answer in JSON regardless
            logger.exception("OBS tool %s failed", name)
            result = {"success": False, "error": f"{name} failed unexpectedly ({type(exc).__name__})", "code": "failed"}
        return json.dumps(result, ensure_ascii=False)

    handler.__name__ = handler.__qualname__ = name
    return handler


def _schema(name: str, description: str, properties: dict | None = None, required: tuple[str, ...] = ()) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": list(required)}}


def _text(description: str) -> dict:
    return {"type": "string", "description": description}


_SCENE = _text("Scene name as OBS shows it; case does not matter when only one scene fits")
TOOLS: tuple[tuple[dict, Callable[[dict], Awaitable[dict]]], ...] = (
    (_schema("obs_status", "Read OBS's state: the live scene, whether it is streaming or recording, fps, CPU use and "
             "dropped frames. Changes nothing."), _status),
    (_schema("obs_list_scenes", "List OBS's scenes top to bottom and say which one is live."), _list_scenes),
    (_schema("obs_switch_scene", "Switch OBS's live (program) scene.", {"scene": _SCENE}, ("scene",)), _switch_scene),
    (_schema("obs_list_audio_inputs", "List the OBS inputs that carry audio and whether each is muted."),
     _list_audio_inputs),
    (_schema("obs_set_mute", "Mute, unmute or toggle one OBS audio input.", {
        "input": _text("Audio input name as OBS shows it"),
        "muted": {"type": "string", "enum": ["true", "false", "toggle"],
                  "description": "true mutes, false unmutes, toggle flips the current state"},
    }, ("input", "muted")), _set_mute),
    (_schema("obs_show_caption", f"Show a one-line caption on stream in Tallyhand's caption text source. It clears "
             f"itself after the given seconds and a newer caption replaces it. Text over {CAPTION_LIMIT} characters "
             "is cut.", {
                 "text": _text("Caption text"),
                 "seconds": {"type": "integer", "minimum": CAPTION_MIN_SECONDS, "maximum": CAPTION_MAX_SECONDS,
                             "default": CAPTION_SECONDS, "description": "How long the caption stays up"},
             }, ("text",)), _show_caption),
    (_schema("obs_clear_caption", "Remove the on-stream caption now."), _clear_caption),
    (_schema("obs_set_source_visible", "Show or hide a source in an OBS scene.", {
        "scene": _SCENE,
        "source": _text("Source name in that scene"),
        "visible": {"type": "boolean", "description": "true shows the source, false hides it"},
    }, ("scene", "source", "visible")), _set_source_visible),
    (_schema("obs_save_replay", "Save OBS's replay buffer (the last moments of the stream) to disk. The replay buffer "
             "must already be running in OBS."), _save_replay),
)
OBS_TOOL_NAMES = tuple(schema["name"] for schema, _ in TOOLS)


def register_obs_tools(ctx: Any) -> list[str]:
    """Register the OBS tools in toolset ``obs``, offered while an OBS URL resolves; returns their names."""
    for schema, run in TOOLS:
        name = schema["name"]
        ctx.register_tool(name=name, toolset=OBS_TOOLSET, schema=schema, handler=_handler(name, run),
                          check_fn=obs.configured, is_async=True, description=schema["description"])
    return list(OBS_TOOL_NAMES)
