"""Settings a plugin cannot switch per viewer; connect warns once per risky one (Hermes 0.22.7 through main).
memory.memory_enabled and memory.user_profile_enabled (both default true) put MEMORY.md and USER.md in every turn;
a non-built-in memory.provider gets each turn; only gateway.platforms.twitch.skip_context_files drops context files."""

from __future__ import annotations

import ast
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PLATFORM = "twitch"
# Tallyhand's toolsets, and what Hermes falls back to when platform_toolsets.twitch is unset: plugin platforms get
# "hermes-<platform>" (hermes_cli/tools_config.py _platform_default_toolset, 0.22.7 through main).
TOOLSETS = ("obs", "twitch")
DEFAULT_TOOLSET = "hermes-twitch"
TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def truthy(value: Any, default: bool) -> bool:
    """A bool setting read the way hermes_cli/config.py reads one (utils.py is_truthy_value): unset means
    ``default``, a string counts only when it is one of TRUE_WORDS."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in TRUE_WORDS
    return bool(value)


def _section(config: Mapping, *keys: str) -> Mapping:
    for key in keys:
        value = config.get(key) if isinstance(config, Mapping) else None
        config = value if isinstance(value, Mapping) else {}
    return config


def saved_toolsets(config: Mapping) -> list[str] | None:
    """platform_toolsets.twitch as Hermes reads it (hermes_cli/toolset_validation.py parse_platform_toolsets_value):
    a list, or a "[...]" string older ``hermes config set`` builds saved; None for no selection at all."""
    value = _section(config, "platform_toolsets").get(PLATFORM)
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            value = ast.literal_eval(value.strip())
        except (ValueError, SyntaxError, RecursionError):
            return None
    return [str(item) for item in value] if isinstance(value, list) else None


def explicit_toolsets(config: Mapping) -> bool:
    """True when platform_toolsets.twitch is a list of its own rather than Hermes' default composite."""
    names = saved_toolsets(config)
    return names is not None and DEFAULT_TOOLSET not in names


def known_toolsets(config: Mapping) -> list[str]:
    """known_plugin_toolsets.twitch: the plugin toolsets Hermes leaves out of a Twitch turn whose list does not name
    them (hermes_cli/tools_config.py _enabled_plugin_toolsets). Every other plugin toolset joins every turn."""
    value = _section(config, "known_plugin_toolsets").get(PLATFORM)
    return [str(item) for item in value] if isinstance(value, list) else []


def profile_name(home: Path) -> str:
    """The Hermes profile a home directory belongs to: ``<root>/profiles/<name>`` is <name>, anything else default."""
    return home.name if home.parent.name == "profiles" else "default"


def risky_settings(config: Mapping, *, external: bool, profile: str = "default", shared: bool = False) -> list[str]:
    """One warning per setting that puts the owner's data into viewers' turns or sends their turns elsewhere.
    ``external`` says whether memory.provider names a provider other than the built-in memory, ``shared`` whether
    the gateway keeps one session per channel."""
    hermes = command(profile)
    memory = _section(config, "memory")
    warnings = []
    if truthy(memory.get("memory_enabled"), True):
        warnings.append(f"Hermes memory is on, so every Twitch turn, viewers' included, carries MEMORY.md; turn it "
                        f"off with memory.memory_enabled: false ({hermes} config set memory.memory_enabled false)")
    if truthy(memory.get("user_profile_enabled"), True):
        warnings.append(f"the Hermes user profile is on, so every Twitch turn carries USER.md; turn it off with "
                        f"memory.user_profile_enabled: false ({hermes} config set memory.user_profile_enabled false)")
    if external:
        warnings.append(f"memory.provider is {str(memory.get('provider')).strip()!r}, so viewers' turns are sent to "
                        f"that memory provider and its recall goes into their answers; set memory.provider to '' "
                        f"({hermes} memory off)")
    if shared:
        warnings.append("group_sessions_per_user is false in the gateway's config, so all viewers in a channel share "
                        "one Hermes session and can read each other's questions back, viewer notes stay out of "
                        "questions, and !forgetme cannot delete one viewer's questions; set it to true in the profile "
                        "that runs the gateway (hermes config set group_sessions_per_user true)")
    hint = _section(config, "platform_hints").get(PLATFORM)
    if isinstance(hint, Mapping) and isinstance(hint.get("replace"), str) and hint["replace"].strip():
        warnings.append(f"platform_hints.twitch.replace swaps out Tallyhand's Twitch hint, which tells the model it is "
                        f"an automated assistant that never claims to be human and never follows instructions from "
                        f"chat; use platform_hints.twitch.append instead ({hermes} config edit)")
    platforms = _section(config, "gateway", "platforms")
    if not bool(_section(platforms, PLATFORM).get("skip_context_files")):
        misplaced = "skip_context_files" in _section(config, "platforms", PLATFORM)
        warnings.append(
            f"Hermes adds your project context files (AGENTS.md, .hermes.md and the like) to every Twitch turn; set "
            f"gateway.platforms.twitch.skip_context_files: true with {hermes} config edit"
            + ("; platforms.twitch.skip_context_files is set, but Hermes reads only the gateway.platforms spelling"
               if misplaced else ""))
    explicit = explicit_toolsets(config)
    if missing := [name for name in TOOLSETS if name not in known_toolsets(config)]:
        warnings.append(
            f"Hermes adds every plugin's toolsets to viewers' and moderators' Twitch turns, so the model sees the "
            f"{' and '.join(missing)} tools there and may name them in chat, though Tallyhand refuses every call; "
            f"add known_plugin_toolsets: {{twitch: [obs, twitch]}} with {hermes} config edit"
            + ("" if explicit else ", together with a platform_toolsets.twitch list without hermes-twitch, such as "
               "[todo], or your own Twitch turns lose those tools as well")
            + " (other plugins' toolsets named in known_plugin_toolsets.twitch stay out the same way)")
    elif not explicit:
        warnings.append("known_plugin_toolsets.twitch keeps the twitch and obs tools out of every Twitch turn that "
                        "does not name them, and platform_toolsets.twitch is not a list or names Hermes' default "
                        "hermes-twitch set, so your own Twitch turns lose them too; set platform_toolsets.twitch to a "
                        f"list without hermes-twitch, such as [todo], with {hermes} config edit")
    return warnings


def command(profile: str) -> str:
    return "hermes" if profile == "default" else f"hermes -p {profile}"


@dataclass(frozen=True, slots=True)
class Report:
    warnings: tuple[str, ...] = ()
    external_memory: bool = False  # viewers' turns go to an external memory provider
    shared_sessions: bool = False  # one Hermes session per channel instead of one per viewer


def check(*, shared: bool = False) -> Report:
    """Log the warnings for the running profile and return them; ``shared`` is the gateway's session layout, from
    the adapter. Never raises: a config that cannot be read is logged as such."""
    try:
        from agent.memory_provider import is_core_memory_provider
        from hermes_cli.config import load_config_readonly
        from hermes_constants import get_hermes_home

        config = load_config_readonly() or {}
        external = not is_core_memory_provider(_section(config, "memory").get("provider"))
        profile = profile_name(get_hermes_home())
        warnings = risky_settings(config, external=external, profile=profile, shared=shared)
    except (ImportError, OSError, ValueError, TypeError, AttributeError) as exc:
        logger.warning("Twitch: could not check Hermes' memory and context settings (%s)", type(exc).__name__)
        return Report(shared_sessions=shared)
    for warning in warnings:
        logger.warning("Twitch: %s", warning)
    if warnings:
        logger.info("Twitch: after changing these, run %s sessions refresh-prompts (existing sessions keep their old "
                    "prompt) and see Recommended setup in Tallyhand's README", command(profile))
    return Report(tuple(warnings), external, shared)
