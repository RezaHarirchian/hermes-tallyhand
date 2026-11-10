"""Tallyhand: Twitch chat as a Hermes gateway platform."""

from __future__ import annotations

from typing import Any

__all__ = ["register"]

SETTINGS = ("user_cooldown", "max_reply_lines", "chat_tail", "chat_commands", "viewer_notes", "viewer_notes_days",
            "event_messages", "min_raid_size", "auto_clip", "auto_clip_announce", "caption_answers", "reply_language",
            "allow_profile_memory_for_viewers")
DOCS_URL = "https://github.com/RezaHarirchian/hermes-tallyhand#readme"


def _settings(ctx: Any) -> dict:
    """The plugin's config_schema values, read when an adapter is built so edits apply on gateway restart."""
    get = getattr(ctx, "get_config", None)
    if not callable(get):
        return {}
    return {key: value for key in SETTINGS if (value := get(key)) is not None}


def register(ctx: Any) -> None:
    """Plugin entry point: registers the ``twitch`` platform (with its setup wizard and the sender cron jobs use when
    no adapter runs in the process), the ``twitch`` and ``obs`` toolsets, the OBS chat commands, the sandbox hook and,
    where Hermes has the API, redaction patterns for the bot token."""
    import dataclasses

    from gateway.platform_registry import PlatformEntry

    from . import auth, roles, sender, wizard
    from .adapter import (
        TwitchAdapter,
        _int_option,
        check_requirements,
        env_enablement,
        is_connected,
        validate_config,
    )
    from .hint import PLATFORM_HINT
    from .obs_commands import register_commands
    from .obs_tools import register_obs_tools
    from .sandbox import pre_tool_call
    from .twitch_tools import register_tools

    async def standalone_sender(pconfig: Any, chat_id: str, message: str, **kwargs: Any) -> dict:
        lines = _int_option("max_reply_lines", 3, _settings(ctx), {}, minimum=1, maximum=sender.MAX_LINES)
        return await sender.send_standalone(pconfig, chat_id, message, max_lines=lines, **kwargs)

    optional = {
        "is_connected": is_connected,
        "setup_fn": wizard.setup,
        "standalone_sender_fn": standalone_sender,
        "parse_target_ref_fn": sender.parse_target_ref,
        "validate_target_ref_fn": sender.validate_target_ref,
        "env_enablement_fn": env_enablement,
        "cron_deliver_env_var": "TWITCH_HOME_CHANNEL",
        "allowed_users_env": "TWITCH_ALLOWED_USERS",
        "allow_all_env": "TWITCH_ALLOW_ALL_USERS",
        "allowlist_suppresses_pairing": True,
        "pii_safe": False,
        "allow_update_command": False,
        "platform_hint": PLATFORM_HINT,
        "display_tier": "low",
        "dashboard_description": "Twitch chat: viewers ask the agent with !hermes or an @mention of the bot.",
        "docs_url": DOCS_URL,
    }
    # Older Hermes releases reject unknown PlatformEntry fields, so pass only the ones this release has.
    known = {field.name for field in dataclasses.fields(PlatformEntry)}
    ctx.register_platform(
        name="twitch",
        label="Twitch",
        adapter_factory=lambda config: TwitchAdapter(config, settings=_settings(ctx)),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["TWITCH_BOT_TOKEN", "TWITCH_CHANNEL"],
        install_hint="No extra packages needed",
        **{key: value for key, value in optional.items() if key in known},
    )
    roles.bind(ctx)
    register_tools(ctx)
    register_obs_tools(ctx)
    register_commands()
    ctx.register_hook("pre_tool_call", pre_tool_call)
    if callable(redact := getattr(ctx, "register_redaction_patterns", None)):
        redact(list(auth.REDACTION_PATTERNS))
