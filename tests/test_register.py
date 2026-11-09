"""Plugin registration and the manifest: what register() hands Hermes and what plugin.yaml declares."""

from __future__ import annotations

import dataclasses
import inspect
import logging
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry
from hermes_cli.plugins import PluginContext

import tallyhand
from harness import RecordingContext
from tallyhand import adapter, auth, commands, obs_tools, sandbox, sender, twitch_tools, wizard
from tallyhand.hint import PLATFORM_HINT

ROOT = Path(tallyhand.__file__).resolve().parent
SECRET = "zq9s3cr3tt0ken7x"
IDENTITY = {"name": "tallyhand", "kind": "platform", "label": "Twitch", "version": "0.1.0", "manifest_version": 2,
            "license": "MIT"}
OPTIONAL_ENV = ["TWITCH_ALLOWED_USERS", "TWITCH_ALLOW_ALL_USERS", "TWITCH_HOME_CHANNEL", "TWITCH_HOME_CHANNEL_NAME",
                "TALLYHAND_TRIGGERS",
                "TALLYHAND_USER_COOLDOWN", "TALLYHAND_REPLY_LANGUAGE", "OBS_WEBSOCKET_URL", "OBS_WEBSOCKET_PASSWORD",
                "TALLYHAND_BRB_SCENE", "TALLYHAND_LIVE_SCENE", "TALLYHAND_CAPTION_SOURCE", "TALLYHAND_OBS_ALLOW_REMOTE"]
ALL_TOOLS = [*twitch_tools.TOOL_NAMES, *obs_tools.OBS_TOOL_NAMES]
NEWER_FIELDS = ("allowlist_suppresses_pairing", "dashboard_description", "docs_url")


class BareContext(RecordingContext):
    """A context from a Hermes release without plugin settings."""

    get_config = None


@dataclasses.dataclass
class OldPlatformEntry:
    """PlatformEntry of a Hermes release from before platform hints and env enablement."""

    name: str
    label: str
    adapter_factory: Callable
    check_fn: Callable
    validate_config: Callable | None = None
    required_env: list = dataclasses.field(default_factory=list)
    install_hint: str = ""
    source: str = "plugin"
    plugin_name: str = ""
    allowed_users_env: str = ""
    allow_all_env: str = ""
    max_message_length: int = 0


@dataclasses.dataclass
class NewPlatformEntry(PlatformEntry):
    """PlatformEntry of a later Hermes release that has the fields this one lacks."""

    allowlist_suppresses_pairing: bool = False
    dashboard_description: str = ""
    docs_url: str = ""


def registered(ctx: RecordingContext | None = None) -> tuple[RecordingContext, dict]:
    ctx = ctx or RecordingContext()
    tallyhand.register(ctx)
    assert len(ctx.platforms) == 1
    return ctx, ctx.platforms[0]


def tuning(call: dict) -> dict:
    """Build an adapter through the registered factory; the settings it and the tools run with, keyed like
    config_schema."""
    built = call["adapter_factory"](PlatformConfig(enabled=True, extra={"channel": "#chan"}))
    assert isinstance(built, adapter.TwitchAdapter)
    return {"user_cooldown": built._cooldowns.seconds, "max_reply_lines": built._max_lines,
            "chat_tail": built._tail.size, "chat_commands": built._chat_commands,
            "tools_from_other_platforms": twitch_tools.other_platforms_allowed(), "viewer_notes": built._notes_on,
            "viewer_notes_days": built._notes_days, "event_messages": built._event_mode,
            "min_raid_size": built._min_raid, "auto_clip": built._auto_clip,
            "auto_clip_announce": built._auto_clip_announce, "caption_answers": built._caption_answers,
            "reply_language": built._reply_language}


DEFAULTS = {"viewer_notes": True, "viewer_notes_days": 90, "event_messages": "off", "min_raid_size": 1,
            "auto_clip": False, "auto_clip_announce": True, "caption_answers": False, "reply_language": "channel"}


@pytest.fixture(scope="module")
def manifest() -> dict:
    from ruamel.yaml import YAML

    return YAML(typ="safe").load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))


# register()

def test_register_platform():
    _, call = registered()
    expected = {
        "name": "twitch", "label": "Twitch", "required_env": ["TWITCH_BOT_TOKEN", "TWITCH_CHANNEL"],
        "install_hint": "No extra packages needed", "cron_deliver_env_var": "TWITCH_HOME_CHANNEL",
        "allowed_users_env": "TWITCH_ALLOWED_USERS", "allow_all_env": "TWITCH_ALLOW_ALL_USERS",
        "pii_safe": False, "allow_update_command": False, "platform_hint": PLATFORM_HINT,
        "check_fn": adapter.check_requirements, "validate_config": adapter.validate_config,
        "is_connected": adapter.is_connected, "env_enablement_fn": adapter.env_enablement,
    }
    assert {key: call.get(key) for key in expected} == expected
    assert adapter.check_requirements() is True
    assert call["setup_fn"] is wizard.setup
    assert call["parse_target_ref_fn"] is sender.parse_target_ref
    assert call["validate_target_ref_fn"] is sender.validate_target_ref
    assert inspect.iscoroutinefunction(call["standalone_sender_fn"])
    assert "standalone_send" not in call  # generic routing: the live adapter first, the sender without one
    assert "max_message_length" not in call  # send() alone chunks and caps replies


def test_register_passes_the_dashboard_and_pairing_fields():
    _, call = registered()
    assert call["allowlist_suppresses_pairing"] is True
    assert call["docs_url"] == "https://github.com/RezaHarirchian/hermes-tallyhand#readme"
    assert call["dashboard_description"] and "\n" not in call["dashboard_description"]


def test_register_builds_a_real_platform_entry():
    _, call = registered()
    assert set(call) <= {field.name for field in dataclasses.fields(PlatformEntry)}
    entry = PlatformEntry(source="plugin", **{"plugin_name": "tallyhand", **call})
    assert (entry.name, entry.max_message_length, entry.platform_hint) == ("twitch", 0, PLATFORM_HINT)


@pytest.mark.parametrize("name", NEWER_FIELDS)
def test_register_skips_fields_this_hermes_lacks(name):
    _, call = registered()
    assert (name in call) == (name in {field.name for field in dataclasses.fields(PlatformEntry)})


def test_register_drops_fields_an_older_hermes_lacks(monkeypatch):
    monkeypatch.setattr("gateway.platform_registry.PlatformEntry", OldPlatformEntry)
    _, call = registered()
    assert set(call) <= {field.name for field in dataclasses.fields(OldPlatformEntry)}
    assert (call["allowed_users_env"], call["allow_all_env"]) == ("TWITCH_ALLOWED_USERS", "TWITCH_ALLOW_ALL_USERS")
    OldPlatformEntry(**call)


def test_register_passes_fields_a_newer_hermes_adds(monkeypatch):
    monkeypatch.setattr("gateway.platform_registry.PlatformEntry", NewPlatformEntry)
    _, call = registered()
    assert call["allowlist_suppresses_pairing"] is True
    assert call["docs_url"] == tallyhand.DOCS_URL
    assert call["dashboard_description"]
    NewPlatformEntry(**call)


def test_adapter_factory_reads_settings_when_it_builds():
    ctx, call = registered(RecordingContext({"user_cooldown": 45}))
    changed = {"max_reply_lines": 2, "chat_tail": 5, "chat_commands": False, "tools_from_other_platforms": True,
               "viewer_notes": False, "viewer_notes_days": 30, "event_messages": "thanks", "min_raid_size": 10,
               "auto_clip": True, "auto_clip_announce": False, "caption_answers": True, "reply_language": "fa"}
    ctx.settings.update(changed)
    assert tuning(call) == {"user_cooldown": 45, **changed}


def test_env_cooldown_beats_the_setting(monkeypatch):
    monkeypatch.setenv("TALLYHAND_USER_COOLDOWN", "7")
    _, call = registered(RecordingContext({"user_cooldown": 45, "max_reply_lines": 2}))
    assert tuning(call) == {"user_cooldown": 7, "max_reply_lines": 2, "chat_tail": 0, "chat_commands": True,
                            "tools_from_other_platforms": False, **DEFAULTS}


@pytest.mark.parametrize("ctx_class", [RecordingContext, BareContext])
def test_unset_settings_fall_back_to_schema_defaults(manifest, ctx_class):
    ctx, call = registered(ctx_class())
    assert tuning(call) == {key: spec["default"] for key, spec in manifest["config_schema"].items()}


def test_settings_read_are_the_schema_keys(manifest):
    ctx, call = registered()
    tuning(call)
    assert sorted(set(ctx.reads)) == sorted(manifest["config_schema"])


def test_register_adds_the_tools_the_commands_and_the_sandbox_hook(manifest):
    ctx, _ = registered()
    assert manifest["provides_tools"] == [call["name"] for call in ctx.tools] == ALL_TOOLS
    assert {call["name"]: call["toolset"] for call in ctx.tools} == {
        **dict.fromkeys(twitch_tools.TOOL_NAMES, "twitch"), **dict.fromkeys(obs_tools.OBS_TOOL_NAMES, "obs")}
    assert manifest["provides_hooks"] == [call["hook_name"] for call in ctx.hooks] == ["pre_tool_call"]
    assert ctx.hooks[0]["callback"] is sandbox.pre_tool_call
    levels = {name: spec.min_role for name, spec in commands.registered().items()}
    assert {name: levels.get(name) for name in ("brb", "back", "scene")} == {
        "brb": "moderator", "back": "moderator", "scene": "owner"}


def test_register_adds_the_token_redaction_patterns():
    ctx, _ = registered()
    assert ctx.redactions == [{"patterns": list(auth.REDACTION_PATTERNS)}]


def test_the_patterns_mask_twitch_tokens_in_hermes_redaction():
    from agent import redact
    from hermes_cli.plugins import PluginManifest, get_plugin_manager

    sample = "q7" * 15  # the shape of a Twitch token: 30 lowercase letters and digits
    ctx = PluginContext(PluginManifest(name="tallyhand", source="user"), get_plugin_manager())
    try:
        assert ctx.register_redaction_patterns(list(auth.REDACTION_PATTERNS)) == 3
        for text, kept in ((f"PASS oauth:{sample}", "PASS oauth:"), (f"curl -H 'Bearer {sample}'", "curl -H 'Bearer"),
                           (f"-H 'OAuth {sample}' id.twitch.tv", "-H 'OAuth"),
                           (f"TWITCH_BOT_TOKEN=oauth:{sample}", "TWITCH_BOT_TOKEN=")):
            masked = redact.redact_sensitive_text(text, force=True)
            assert sample not in masked and masked.startswith(kept), masked
        assert redact.redact_sensitive_text(f"room {sample} id", force=True) == f"room {sample} id"
    finally:
        redact._reset_plugin_redaction_patterns()


def test_register_skips_redaction_on_a_hermes_without_it():
    class NoRedaction(RecordingContext):
        register_redaction_patterns = None

    ctx, _ = registered(NoRedaction())
    assert ctx.redactions == [] and len(ctx.tools) == len(ALL_TOOLS)


def test_register_binds_the_session_surface():
    from tallyhand import roles

    ctx, _ = registered()
    ctx.surface = "a2a"
    assert roles.caller().platform == "a2a"


def test_register_passes_the_low_display_tier():
    _, call = registered()
    if "display_tier" in {field.name for field in dataclasses.fields(PlatformEntry)}:
        assert call["display_tier"] == "low"
    else:
        assert "display_tier" not in call


def test_registered_env_names_are_declared(manifest):
    _, call = registered()
    assert sorted(call["required_env"]) == sorted(entry["name"] for entry in manifest["requires_env"])
    optional = {entry["name"] for entry in manifest["optional_env"]}
    assert {call["cron_deliver_env_var"], call["allowed_users_env"], call["allow_all_env"]} <= optional


# plugin.yaml

def test_manifest_identity(manifest):
    assert {key: manifest.get(key) for key in IDENTITY} == IDENTITY


@pytest.mark.filterwarnings("ignore:'maxsplit' is passed as positional argument:DeprecationWarning")
def test_manifest_requires_a_released_hermes(manifest):
    from hermes_cli.plugins_manifest import requires_hermes_error, running_hermes_version

    assert re.fullmatch(r">=\d+\.\d+\.\d+", manifest["requires_hermes"]), manifest["requires_hermes"]
    assert manifest["requires_hermes"] == ">=0.22.7"
    # Hermes' own load gate; a checkout without release tags reports "unknown", which the gate lets through.
    assert requires_hermes_error(manifest) is None, running_hermes_version()


def test_manifest_env(manifest):
    required = {entry["name"]: entry for entry in manifest["requires_env"]}
    assert sorted(required) == ["TWITCH_BOT_TOKEN", "TWITCH_CHANNEL"]
    assert len(manifest["requires_env"]) == 2
    # password marks the field in Hermes' config form, secret masks it at the hermes plugins install prompt.
    assert required["TWITCH_BOT_TOKEN"]["password"] is True and required["TWITCH_BOT_TOKEN"]["secret"] is True
    assert sorted(entry["name"] for entry in manifest["optional_env"]) == sorted(OPTIONAL_ENV)
    secret = {entry["name"] for entry in manifest["optional_env"] if entry["password"]}
    assert secret == {"OBS_WEBSOCKET_PASSWORD"}
    assert all(entry.get("secret") is True for entry in manifest["optional_env"] if entry["password"])


def test_manifest_config_schema(manifest):
    schema = manifest["config_schema"]
    assert {key: (spec["type"], spec["default"]) for key, spec in schema.items()} == {
        "user_cooldown": ("int", 20), "max_reply_lines": ("int", 3), "chat_tail": ("int", 0),
        "chat_commands": ("bool", True), "tools_from_other_platforms": ("bool", False),
        "viewer_notes": ("bool", True), "viewer_notes_days": ("int", 90), "event_messages": ("string", "off"),
        "min_raid_size": ("int", 1), "auto_clip": ("bool", False), "auto_clip_announce": ("bool", True),
        "caption_answers": ("bool", False), "reply_language": ("string", "channel")}
    assert schema["event_messages"]["choices"] == ["off", "thanks"]
    for spec in schema.values():
        assert isinstance(spec["label"], str) and spec["label"].strip()
        assert isinstance(spec["description"], str) and spec["description"].strip()


@pytest.mark.filterwarnings("ignore:'maxsplit' is passed as positional argument:DeprecationWarning")
def test_hermes_parses_the_manifest(caplog):
    plugins_manifest = pytest.importorskip("hermes_cli.plugins_manifest")
    with caplog.at_level(logging.WARNING):
        parsed = plugins_manifest.parse_manifest_file(ROOT / "plugin.yaml", ROOT, "user", "")
    assert parsed is not None
    assert [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING] == []
    assert (parsed.name, parsed.kind, parsed.version, parsed.manifest_version) == ("tallyhand", "platform", "0.1.0", 2)
    assert (parsed.provides_tools, parsed.provides_hooks) == (ALL_TOOLS, ["pre_tool_call"])
    assert plugins_manifest.requires_hermes_error(parsed) is None
    defaults = {key: spec["default"] for key, spec in parsed.config_schema.items()}
    assert plugins_manifest.validate_config_schema(parsed.name, parsed.config_schema, defaults) == []


# configuration helpers

@pytest.mark.parametrize("extra, token, ok", [
    ({"token": "oauth:abc", "channel": "#a"}, None, True),
    ({"channel": "a,b"}, "abc", True),
    ({"token": "abc", "channel": ["A", "#b"]}, None, True),
    ({"channel": "#a"}, None, False),
    ({"token": "abc"}, None, False),
    ({"token": "oauth:", "channel": "#a"}, None, False),
    ({"token": "abc", "channel": "#not a channel"}, None, False),
    ({}, None, False),
])
def test_validate_config_and_is_connected(extra, token, ok):
    config = PlatformConfig(enabled=True, token=token, extra=extra)
    assert adapter.validate_config(config) is ok
    assert adapter.is_connected(config) is ok


def test_validate_config_reads_env(monkeypatch):
    config = PlatformConfig(enabled=True)
    assert adapter.validate_config(config) is False
    monkeypatch.setenv("TWITCH_BOT_TOKEN", "oauth:abc")
    monkeypatch.setenv("TWITCH_CHANNEL", "Chan")
    assert adapter.validate_config(config) is True
    assert adapter.is_connected(config) is True


@pytest.mark.parametrize("env", [
    {},
    {"TWITCH_BOT_TOKEN": SECRET},
    {"TWITCH_CHANNEL": "a"},
    {"TWITCH_BOT_TOKEN": "oauth:", "TWITCH_CHANNEL": "a"},
    {"TWITCH_BOT_TOKEN": SECRET, "TWITCH_CHANNEL": " , "},
])
def test_env_enablement_needs_token_and_channel(monkeypatch, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert adapter.env_enablement() is None


@pytest.mark.parametrize("home, chat_id", [
    (None, None),
    ("  ", None),
    ("Other", "#other"),
    ("#B", "#b"),
    ("not a channel!", None),
])
def test_env_enablement_seeds_channels_and_home(monkeypatch, home, chat_id):
    monkeypatch.setenv("TWITCH_BOT_TOKEN", f"oauth:{SECRET}")
    monkeypatch.setenv("TWITCH_CHANNEL", "A, #b")
    if home is not None:
        monkeypatch.setenv("TWITCH_HOME_CHANNEL", home)
    seeded = adapter.env_enablement()
    assert seeded["channel"] == "#a,#b"
    assert seeded.get("home_channel", {}).get("chat_id") == chat_id
    assert "token" not in seeded
    assert SECRET not in repr(seeded)


def test_hermes_can_tell_two_profiles_use_the_same_bot():
    from gateway.run import GatewayRunner

    fingerprint = GatewayRunner._adapter_credential_fingerprint
    first = adapter.TwitchAdapter(PlatformConfig(enabled=True, extra={"token": f"oauth:{SECRET}", "channel": "#a"}))
    second = adapter.TwitchAdapter(PlatformConfig(enabled=True, extra={"token": SECRET, "channel": "#b"}))
    other = adapter.TwitchAdapter(PlatformConfig(enabled=True, extra={"token": "another", "channel": "#a"}))
    assert fingerprint(first) == fingerprint(second) != fingerprint(other)
    assert fingerprint(first) is not None and SECRET not in fingerprint(first)


@pytest.mark.parametrize(("settings", "expected"), [
    ({"event_messages": False}, "off"), ({"event_messages": True}, "thanks"),
    ({"event_messages": " Thanks "}, "thanks"), ({"event_messages": "loud"}, "off")])
def test_event_messages_as_hermes_may_store_it(settings, expected):
    _, call = registered(RecordingContext(settings))
    assert tuning(call)["event_messages"] == expected


@pytest.mark.parametrize(("value", "expected"), [
    (25, 25), ("25", 25), (25.0, 25), ("25.0", 25), (" 7 ", 7), (25.5, 20), ("lots", 20), (True, 20), ("1e400", 20)])
def test_numbers_as_hermes_may_hand_them_over(value, expected):
    _, call = registered(RecordingContext({"user_cooldown": value}))
    assert tuning(call)["user_cooldown"] == expected
