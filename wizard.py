"""``hermes gateway setup`` for Twitch: the bot token (checked live, never shown), the channels, the owner, who may
ask the bot, and the Twitch-only Hermes settings from the README's Recommended setup."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from . import auth, helix, irc, privacy

TOKEN_ENV, CHANNEL_ENV = "TWITCH_BOT_TOKEN", "TWITCH_CHANNEL"
ALLOWED_ENV, ALLOW_ALL_ENV = "TWITCH_ALLOWED_USERS", "TWITCH_ALLOW_ALL_USERS"
TOKEN_TRIES = 3
OWNER_ONLY, LIST, EVERYONE = 0, 1, 2
# The Twitch-only settings the wizard can apply; the memory switches are profile-wide, so it only names them.
RECOMMENDED_TOOLSETS = ["todo"]


class Prompts(Protocol):
    """What the wizard needs from a terminal."""

    def text(self, question: str, default: str = "") -> str: ...

    def secret(self, question: str) -> str: ...

    def choice(self, question: str, options: list[str], default: int = 0) -> int: ...

    def yes_no(self, question: str, default: bool = True) -> bool: ...

    def info(self, text: str) -> None: ...

    def warn(self, text: str) -> None: ...


@dataclass
class Store:
    """Where the answers go: env values (the token among them) and the Twitch platform settings; apply_platform says
    whether it set platform_toolsets.twitch and whether it set known_plugin_toolsets.twitch."""

    read_env: Callable[[str], str]
    save_env: Callable[[str, str], Any]
    apply_platform: Callable[[], tuple[bool, bool]]


@dataclass
class Endpoints:
    validate_url: str = auth.VALIDATE_URL
    helix_url: str = helix.BASE_URL


def _validate(token: str, endpoints: Endpoints) -> auth.TokenInfo:
    return asyncio.run(auth.validate_token(token, url=endpoints.validate_url))


def _lookup(token: str, logins: list[str], endpoints: Endpoints) -> dict[str, helix.User]:
    client = helix.Helix(token, base_url=endpoints.helix_url, validate_url=endpoints.validate_url)
    return asyncio.run(client.users(logins))


def ask_token(ui: Prompts, store: Store, endpoints: Endpoints) -> tuple[str, auth.TokenInfo] | None:
    """The bot token, validated with Twitch; Enter keeps the one already set. None after TOKEN_TRIES failures."""
    current = auth.strip_oauth_prefix(store.read_env(TOKEN_ENV))
    for _ in range(TOKEN_TRIES):
        hint = " (Enter keeps the one already set)" if current else ""
        token = auth.strip_oauth_prefix(ui.secret(f"Bot access token{hint}")) or current
        if not token:
            ui.warn("A token is required: a user access token of the bot account with chat:read and chat:edit.")
            continue
        try:
            return token, _validate(token, endpoints)
        except auth.AuthError as exc:
            ui.warn(f"Twitch did not accept the token: {exc.message}")
    return None


def show_scopes(ui: Prompts, info: auth.TokenInfo) -> None:
    ui.info(f"The token belongs to {info.login} (user id {info.user_id}).")
    if info.expires_in > 0:
        ui.info(f"It expires in {auth.expires_in(info)}. Tallyhand never refreshes it: generate a new one before then.")
    else:
        ui.info("Twitch reported no expiry for it.")
    for feature, missing in helix.missing_by_feature(info.scopes):
        ui.info(f"  {feature}: {'ready' if not missing else 'needs ' + ', '.join(missing)}")


def ask_channels(ui: Prompts, store: Store, token: str, endpoints: Endpoints) -> tuple[list[str], helix.User]:
    """The channels to join, owner's first, each checked with Twitch; the owner's account."""
    current = ",".join(channel.lstrip("#") for channel in irc.parse_channels(store.read_env(CHANNEL_ENV)))
    while True:
        answer = ui.text("Channels to join, comma-separated, your own first", current)
        channels = irc.parse_channels(answer)
        if not channels:
            ui.warn("Give at least one channel login, for example: yourchannel")
            continue
        found = _lookup(token, [channel.lstrip("#") for channel in channels], endpoints)
        unknown = [channel for channel in channels if channel.lstrip("#") not in found]
        if unknown:
            ui.warn(f"Twitch has no channel named {', '.join(name.lstrip('#') for name in unknown)}.")
            continue
        owner = found[channels[0].lstrip("#")]
        ui.info(f"The owner is {owner.display_name} ({owner.login}, user id {owner.id}), the broadcaster of "
                f"{channels[0]}: only the owner's questions run with your own toolsets.")
        return channels, owner


def ask_allowed(ui: Prompts, token: str, owner: helix.User, endpoints: Endpoints) -> tuple[str, bool]:
    """TWITCH_ALLOWED_USERS and TWITCH_ALLOW_ALL_USERS for the chosen answer. The list holds Twitch user ids,
    which Hermes matches as they are: the bot needs no lookup at startup, so a Twitch outage then locks nobody out."""
    pick = ui.choice("Who may ask the bot questions?", [
        f"Only you ({owner.login})", "You and the Twitch accounts you list", "Everyone in chat"], OWNER_ONLY)
    if pick == EVERYONE:
        ui.warn("Every viewer will be able to start a turn, and every question costs you a model call. Viewers "
                "get a to-do list only, never your tools, but anyone can try to talk the bot into saying something.")
        if ui.yes_no("Let everyone in chat ask?", default=False):
            return "", True
        pick = OWNER_ONLY
    users = [owner]
    if pick == LIST:
        while True:
            answer = ui.text("Twitch logins, comma-separated")
            wanted = [login for value in answer.split(",") if (login := helix.login_of(value))]
            found = _lookup(token, wanted, endpoints) if wanted else {}
            if unknown := [login for login in wanted if login not in found]:
                ui.warn(f"Twitch has no account named {', '.join(unknown)}.")
                continue
            users += [found[login] for login in dict.fromkeys(wanted) if found[login].id != owner.id]
            break
    ui.info("Saved as Twitch user ids: " + ", ".join(f"{user.login} {user.id}" for user in users) + ".")
    return ",".join(user.id for user in users), False


def run(ui: Prompts, store: Store, endpoints: Endpoints | None = None) -> bool:
    """The whole wizard; True when the settings were saved."""
    endpoints = endpoints or Endpoints()
    ui.info("Tallyhand connects Hermes to Twitch chat as your bot account. See Token scopes in docs/twitch-tools.md "
            "for what each feature needs.")
    if (validated := ask_token(ui, store, endpoints)) is None:
        ui.warn("Nothing was saved. Generate a new token for the bot account and run the setup again.")
        return False
    token, info = validated  # validation already refused a token without chat:read and chat:edit
    show_scopes(ui, info)
    try:
        channels, owner = ask_channels(ui, store, token, endpoints)
        allowed, everyone = ask_allowed(ui, token, owner, endpoints)
    except helix.HelixError as exc:
        ui.warn(f"Twitch did not answer an account lookup ({exc.message}). Nothing was saved; run the setup again.")
        return False
    store.save_env(TOKEN_ENV, token)
    store.save_env(CHANNEL_ENV, ",".join(channel.lstrip("#") for channel in channels))
    store.save_env(ALLOWED_ENV, allowed)
    store.save_env(ALLOW_ALL_ENV, "true" if everyone else "false")
    ui.info(f"Saved {TOKEN_ENV}, {CHANNEL_ENV}, {ALLOWED_ENV} and {ALLOW_ALL_ENV}.")
    if ui.yes_no("Apply the recommended Twitch settings: skip context files in Twitch turns, give Twitch only the "
                 "to-do list toolset, and keep the Twitch and OBS tools out of viewers' turns?", default=True):
        toolsets, known = store.apply_platform()
        ui.info("Applied gateway.platforms.twitch.skip_context_files: true"
                + (", platform_toolsets.twitch: [todo]" if toolsets else "")
                + (" and known_plugin_toolsets.twitch: [obs, twitch]." if known else "."))
        if not toolsets:
            ui.info("platform_toolsets.twitch was already set and stays as it is.")
        if not known:
            ui.warn("platform_toolsets.twitch is Hermes' default composite, so known_plugin_toolsets.twitch was left "
                    "alone: with it set, your own Twitch turns would lose the Twitch and OBS tools too. Viewers' turns "
                    "still list those tools, though Tallyhand refuses every call; give platform_toolsets.twitch a list "
                    "of your own and run the setup again to keep them out.")
    ui.info("Memory is set for the whole profile, so the wizard leaves it alone. While it is on, Tallyhand answers "
            f"only you, unless you set {privacy.OPT_IN}. For a profile that runs only the bot, turn it off as "
            "Recommended setup in the README shows.")
    ui.info("Restart the gateway to connect: hermes gateway restart")
    return True


# the terminal side: Hermes' own public setup helpers


class HermesPrompts:
    """Prompts through hermes_cli.setup and hermes_cli.cli_output, the helpers Hermes' bundled platform wizards use
    (QQBot's among them). Ctrl+C or a closed input ends the wizard the way Hermes' own prompts do."""

    def text(self, question: str, default: str = "") -> str:
        from hermes_cli.setup import prompt

        return prompt(question, default or None)

    def secret(self, question: str) -> str:
        from hermes_cli.setup import prompt

        return prompt(question, password=True)

    def choice(self, question: str, options: list[str], default: int = 0) -> int:
        from hermes_cli.setup import prompt_choice

        return prompt_choice(question, options, default)

    def yes_no(self, question: str, default: bool = True) -> bool:
        from hermes_cli.setup import prompt_yes_no

        return prompt_yes_no(question, default)

    def info(self, text: str) -> None:
        from hermes_cli.cli_output import print_info

        print_info(text)

    def warn(self, text: str) -> None:
        from hermes_cli.cli_output import print_warning

        print_warning(text)


def read_env(name: str) -> str:
    from gateway.platforms._shared import get_scoped_secret

    return str(get_scoped_secret(name, "") or "")


def save_env(name: str, value: str) -> None:
    """Hermes' own save path for platform settings (hermes_cli/config.py save_env_value, which the IRC wizard uses):
    the active profile's .env, written atomically, mode 0600 for a new file, the value never echoed."""
    from hermes_cli.config import save_env_value

    save_env_value(name, value)


def apply_platform() -> tuple[bool, bool]:
    """gateway.platforms.twitch.skip_context_files, platform_toolsets.twitch when unset, and Tallyhand's toolsets in
    known_plugin_toolsets.twitch, merged into the profile's config.yaml with
    hermes_cli.config.save_config(merge_existing=True), which keeps every other key and the comments.
    skip_context_files is spelled the way the gateway reads it (gateway/session_execution.py); hermes config set
    would move it to platforms.twitch. known_plugin_toolsets keeps the twitch and obs tools out of viewers' and
    moderators' turns, and is set only where the owner's turns name them through a list of their own (see
    privacy.explicit_toolsets). Returns whether it set platform_toolsets.twitch and known_plugin_toolsets.twitch."""
    from hermes_cli.config import load_config_readonly, save_config

    current = load_config_readonly() or {}
    update: dict[str, Any] = {"gateway": {"platforms": {"twitch": {"skip_context_files": True}}}}
    toolsets = not privacy.saved_toolsets(current)
    if toolsets:
        update["platform_toolsets"] = {"twitch": list(RECOMMENDED_TOOLSETS)}
    known = privacy.known_toolsets(current)
    if set_known := toolsets or privacy.explicit_toolsets(current):
        update["known_plugin_toolsets"] = {"twitch": sorted({*known, *privacy.TOOLSETS})}
    save_config(update, merge_existing=True)
    return toolsets, set_known


def setup() -> None:
    """PlatformEntry.setup_fn: ``hermes gateway setup``, then Twitch."""
    from hermes_cli.gateway_setup_wizard import confirm_reconfigure, print_setup_header

    print_setup_header("Twitch")
    if not confirm_reconfigure("Twitch", TOKEN_ENV, CHANNEL_ENV):
        return
    run(HermesPrompts(), Store(read_env=read_env, save_env=save_env, apply_platform=apply_platform),
        Endpoints(validate_url=auth.VALIDATE_URL, helix_url=helix.BASE_URL))
