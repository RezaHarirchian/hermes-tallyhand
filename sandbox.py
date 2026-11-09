"""The ``pre_tool_call`` hook: what a Twitch turn may run, by the asker's role, and Hermes' human approval before a
ban, an unban or a timeout longer than a moderator may give. It backs up ``toolsets_for_source``, which cannot take
other plugins' or the context engine's tools away from viewers."""

from __future__ import annotations

import logging
from typing import Any

from . import roles, twitch_tools

logger = logging.getLogger(__name__)

TODO_TOOLSET = "todo"
_TODO_FALLBACK = frozenset({"todo_list"})  # the todo toolset in Hermes 0.22.7 through main
# Hermes hides plugin tools and todo_list behind its tool search bridge. Searching and describing only read the
# catalog; a tool_call through the bridge reaches this hook as the tool it calls (agent/tool_executor.py
# _unwrap_tool_search_call), so the bridge's own name is never allowed.
CATALOG_TOOLS = frozenset({"tool_search", "tool_describe"})
FAILED = "Blocked: Tallyhand could not check this tool call."


def todo_tools() -> frozenset[str]:
    """Tool names of Hermes' ``todo`` toolset, the only tools every Twitch asker keeps."""
    from tools.registry import registry

    return frozenset(registry.get_tool_names_for_toolset(TODO_TOOLSET)) or _TODO_FALLBACK


def block(message: str) -> dict:
    return {"action": "block", "message": message}


def decide(tool: str, args: dict, who: roles.Caller) -> dict | None:
    """The hook's verdict for one call: None to let it run, a block, or an approval request."""
    if who.twitch:
        level = who.level()
        if level != roles.OWNER:
            allowed = todo_tools() | CATALOG_TOOLS
            if level == roles.MODERATOR:
                allowed |= twitch_tools.MODERATOR_TOOLS
            if tool not in allowed:
                return block(f"{tool} is not available to you in Twitch chat.")
            if tool == twitch_tools.TIMEOUT:
                try:
                    seconds = twitch_tools.timeout_seconds(args)
                except twitch_tools.BadArguments as exc:
                    return block(f"{tool} refused: {exc}.")
                if seconds > twitch_tools.MODERATOR_MAX_TIMEOUT:
                    return block(twitch_tools.moderator_timeout_cap(sentence=True))
            return None
    if tool not in twitch_tools.TOOL_NAMES:
        return None
    if not (who.twitch or who.local or twitch_tools.other_platforms_allowed()):
        return block(f"Twitch tools are off here; the streamer can allow them with the "
                     f"{twitch_tools.OTHER_PLATFORMS_SETTING} plugin setting.")
    if not who.local:  # before any approval question: a steered turn cannot even ask about another channel
        try:
            reason = twitch_tools.unserved_channel(tool, twitch_tools.channel_arg(args))
        except twitch_tools.BadArguments:
            reason = None  # approval() or the tool says what is wrong with the arguments
        if reason:
            return block(f"{tool} refused: {reason}.")
    verdict = twitch_tools.approval(tool, args)
    if verdict is not None and verdict["action"] == "approve" and who.single_query:
        # A single-query run that hosts its own tools, where Hermes would approve without asking. From 0.22.7 on,
        # chat -q and -z run their turns in the gateway, which never carries the marker (cli_single_query.py,
        # PLG-7): Hermes refuses a -q call itself and approves a -z call unasked (see docs/twitch-tools.md).
        return block(f"{tool} needs a person to approve it; run it from an interactive session.")
    return verdict


def pre_tool_call(**kwargs: Any) -> dict | None:
    """Hermes ``pre_tool_call`` callback. Never raises; when the check itself fails it blocks the call for a
    Twitch turn (and for any Twitch tool, or any call in a process where the Twitch adapter runs, when it cannot
    even tell whose turn it is)."""
    tool = str(kwargs.get("tool_name") or "")
    args = kwargs.get("args")
    args = args if isinstance(args, dict) else {}
    try:
        who = roles.caller()
    except Exception:  # hook seam: never raise into Hermes' tool loop
        guarded = tool in twitch_tools.TOOL_NAMES or roles.live() is not None
        logger.warning("Twitch: cannot tell whose turn runs %s; %s", tool,
                       "blocking it" if guarded else "leaving it alone")
        return block(FAILED) if guarded else None
    try:
        return decide(tool, args, who)
    except Exception:  # hook seam: never raise into Hermes' tool loop; fail closed for Twitch
        logger.warning("Twitch: sandbox check for %s failed", tool, exc_info=True)
        return block(FAILED) if who.twitch or tool in twitch_tools.TOOL_NAMES else None
