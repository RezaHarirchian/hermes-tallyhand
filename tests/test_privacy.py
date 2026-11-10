"""The connect-time privacy check: which Hermes settings put the owner's data into viewers' turns, read the way Hermes
reads them, one warning each, and the check through a real config.yaml and a connected adapter."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tallyhand import privacy

SAFE = {"memory": {"memory_enabled": False, "user_profile_enabled": False, "provider": ""},
        "gateway": {"platforms": {"twitch": {"skip_context_files": True}}},
        "platform_toolsets": {"twitch": ["todo"]}, "known_plugin_toolsets": {"twitch": ["obs", "twitch"]}}


def kinds(warnings: list[str]) -> list[str]:
    names = {"MEMORY.md": "memory", "USER.md": "user", "memory provider": "provider", "context files": "context",
             "known_plugin_toolsets": "toolsets"}
    return [next(kind for marker, kind in names.items() if marker in text) for text in warnings]


def test_hermes_defaults_have_four_risks():
    assert kinds(privacy.risky_settings({}, external=False)) == ["memory", "user", "context", "toolsets"]


def test_the_recommended_settings_have_none():
    assert privacy.risky_settings(SAFE, external=False) == []


def test_each_warning_names_the_setting_and_the_command():
    memory, user, context, toolsets = privacy.risky_settings({}, external=False, profile="tallyhand")
    assert "memory.memory_enabled: false" in memory
    assert "hermes -p tallyhand config set memory.memory_enabled false" in memory
    assert "memory.user_profile_enabled: false" in user
    assert "gateway.platforms.twitch.skip_context_files: true" in context
    assert "hermes -p tallyhand config edit" in context
    assert "known_plugin_toolsets: {twitch: [obs, twitch]}" in toolsets
    assert "hermes -p tallyhand config edit" in toolsets
    assert "platform_toolsets.twitch list without hermes-twitch, such as [todo]" in toolsets
    (provider,) = privacy.risky_settings({**SAFE, "memory": {**SAFE["memory"], "provider": "honcho"}}, external=True)
    assert "'honcho'" in provider and "hermes memory off" in provider


def test_the_spelling_hermes_ignores_is_called_out():
    config = {**SAFE, "gateway": {}, "platforms": {"twitch": {"skip_context_files": True}}}
    (context,) = privacy.risky_settings(config, external=False)
    assert "Hermes reads only the gateway.platforms spelling" in context


@pytest.mark.parametrize(("value", "on"), [
    (None, True), (True, True), (False, False), ("false", False), ("no", False), ("0", False), ("off", False),
    ("true", True), ("yes", True), ("On", True), ("maybe", False), (1, True), (0, False)])
def test_memory_flags_read_like_hermes(value, on):
    from tools.memory_tool import get_builtin_memory_store_flags

    config = {**SAFE, "memory": {"memory_enabled": value, "user_profile_enabled": value}}
    if value is None:
        config["memory"] = {}
    assert get_builtin_memory_store_flags(config) == (on, on)  # Hermes' own reading
    assert (len(privacy.risky_settings(config, external=False)) == 2) is on


@pytest.mark.parametrize("platforms", [None, [], ["twitch"], {"twitch": None}, {"twitch": {"skip_context_files": ""}},
                                       {"twitch": {"skip_context_files": "false"}}, {"twitch": {"other": 1}}])
def test_skip_context_files_reads_like_hermes(platforms):
    from types import SimpleNamespace

    from gateway.session_execution import GatewaySessionAgentMixin

    config = {**SAFE, "gateway": {"platforms": platforms}}
    reader = SimpleNamespace(_runner=None, _ctx=SimpleNamespace(source=None, user_config=config))
    skipped = GatewaySessionAgentMixin._skip_context_files(reader, "twitch")  # Hermes' own reader
    assert (privacy.risky_settings(config, external=False) == []) is skipped


@pytest.mark.parametrize(("home", "profile"), [
    (Path("/u/.hermes"), "default"), (Path("/u/.hermes/profiles/tallyhand"), "tallyhand"),
    (Path("/srv/hermes"), "default")])
def test_profile_name(home, profile):
    assert privacy.profile_name(home) == profile


def write_config(home: Path, text: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text, encoding="utf-8")


def test_check_reads_the_running_profile(monkeypatch, tmp_path, caplog):
    home = tmp_path / "root" / "profiles" / "tallyhand"
    write_config(home, "memory:\n  provider: honcho\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    with caplog.at_level(logging.INFO, logger="tallyhand.privacy"):
        report = privacy.check()
    assert kinds(report.warnings) == ["memory", "user", "provider", "context", "toolsets"] and report.external_memory
    assert [record.levelno for record in caplog.records] == [logging.WARNING] * 5 + [logging.INFO]
    assert "hermes -p tallyhand sessions refresh-prompts" in caplog.records[-1].getMessage()


def test_check_with_the_recommended_config_is_quiet(monkeypatch, tmp_path, caplog):
    home = tmp_path / "home"
    write_config(home, "memory:\n  memory_enabled: false\n  user_profile_enabled: false\n  provider: ''\n"
                       "gateway:\n  platforms:\n    twitch:\n      skip_context_files: true\n"
                       "platform_toolsets:\n  twitch: [todo]\nknown_plugin_toolsets:\n  twitch: [obs, twitch]\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    with caplog.at_level(logging.INFO, logger="tallyhand.privacy"):
        assert privacy.check() == privacy.Report()
    assert caplog.records == []


async def test_connect_logs_one_warning_per_risk(harness, twitch, monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "fresh"))
    with caplog.at_level(logging.WARNING, logger="tallyhand.privacy"):
        await harness.connected()
    assert len(caplog.records) == 4


@pytest.mark.parametrize(("toolsets", "known", "warning"), [
    (["todo"], ["obs", "twitch"], None),
    (["web", "todo"], ["a2a", "obs", "spotify", "twitch"], None),  # what `hermes tools disable` writes
    (["todo"], ["twitch"], "the obs tools"),
    (["todo"], None, "the obs and twitch tools"),
    (None, None, "or your own Twitch turns lose those tools as well"),
    (None, ["obs", "twitch"], "is not a list or names Hermes' default hermes-twitch set"),
    (["hermes-twitch"], ["obs", "twitch"], "is not a list or names Hermes' default hermes-twitch set"),
    ('["todo"]', ["obs", "twitch"], None),  # a list literal older `hermes config set` builds saved
])
def test_plugin_toolsets_in_viewers_turns(toolsets, known, warning):
    config = {**SAFE, "platform_toolsets": {"twitch": toolsets}, "known_plugin_toolsets": {"twitch": known}}
    warnings = privacy.risky_settings(config, external=False)
    if warning is None:
        assert warnings == []
    else:
        (text,) = warnings
        assert warning in text


ADD_KNOWN = ("Hermes adds every plugin's toolsets to viewers' and moderators' Twitch turns, so the model sees the "
             "obs and twitch tools there and may name them in chat, though Tallyhand refuses every call; add "
             "known_plugin_toolsets: {twitch: [obs, twitch]} with hermes -p tallyhand config edit")
WITH_A_LIST = (", together with a platform_toolsets.twitch list without hermes-twitch, such as [todo], or your own "
               "Twitch turns lose those tools as well")
OTHER_PLUGINS = " (other plugins' toolsets named in known_plugin_toolsets.twitch stay out the same way)"
SET_A_LIST = ("known_plugin_toolsets.twitch keeps the twitch and obs tools out of every Twitch turn that does not "
              "name them, and platform_toolsets.twitch is not a list or names Hermes' default hermes-twitch set, so "
              "your own Twitch turns lose them too; set platform_toolsets.twitch to a list without hermes-twitch, such "
              "as [todo], with hermes -p tallyhand config edit")


@pytest.mark.parametrize(("toolsets", "known", "expected"), [
    (None, None, ADD_KNOWN + WITH_A_LIST + OTHER_PLUGINS),  # platform_toolsets.twitch unset
    (["hermes-twitch"], None, ADD_KNOWN + WITH_A_LIST + OTHER_PLUGINS),  # a list naming Hermes' default set
    (["hermes-twitch", "web"], None, ADD_KNOWN + WITH_A_LIST + OTHER_PLUGINS),
    (["todo"], None, ADD_KNOWN + OTHER_PLUGINS),  # a list of its own needs only the key
    (None, ["obs", "twitch"], SET_A_LIST),
    (["hermes-twitch"], ["obs", "twitch"], SET_A_LIST),
    (["hermes-twitch", "web"], ["obs", "twitch"], SET_A_LIST),
])
def test_toolset_warnings_ask_for_a_list_without_hermes_twitch_and_config_edit(toolsets, known, expected):
    """What docs/twitch-tools.md and docs/configuration.md say: a platform_toolsets.twitch list without
    hermes-twitch, then known_plugin_toolsets through config edit. The hermes tools menu has no Twitch row."""
    config = {**SAFE, "platform_toolsets": {"twitch": toolsets}, "known_plugin_toolsets": {"twitch": known}}
    (text,) = privacy.risky_settings(config, external=False, profile="tallyhand")
    assert text == expected
    assert "\n" not in text and ". " not in text  # one log line, one sentence


def test_a_shared_channel_session_is_the_gateways_setting():
    (shared,) = privacy.risky_settings(SAFE, external=False, profile="tallyhand", shared=True)
    assert "group_sessions_per_user is false in the gateway's config" in shared
    assert "hermes config set group_sessions_per_user true" in shared  # the gateway runs in the default profile
    assert privacy.risky_settings({**SAFE, "group_sessions_per_user": False}, external=False) == []


def test_check_carries_the_layout_it_was_given(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "fresh"))
    assert privacy.check(shared=True).shared_sessions and not privacy.check().shared_sessions


def test_a_replaced_twitch_hint_is_called_out():
    (hint,) = privacy.risky_settings({**SAFE, "platform_hints": {"twitch": {"replace": "You are Bob, a person."}}},
                                     external=False)
    assert "platform_hints.twitch.replace" in hint and "platform_hints.twitch.append" in hint
    assert privacy.risky_settings({**SAFE, "platform_hints": {"twitch": {"append": "Be brief."}}}, external=False) == []


def test_the_memory_warnings_say_what_happens_to_viewers():
    refused = "; until then Tallyhand refuses questions from anyone but you"
    allowed = f"; {privacy.OPT_IN} is on, so viewers' answers use it"
    memory, user, *_ = privacy.risky_settings({}, external=False)
    assert memory.endswith(refused) and user.endswith(refused)
    memory, user, *_ = privacy.risky_settings({}, external=False, opted_in=True)
    assert memory.endswith(allowed) and user.endswith(allowed)
    config = {**SAFE, "memory": {**SAFE["memory"], "provider": "honcho"}}
    assert privacy.risky_settings(config, external=True)[0].endswith(refused)
    assert privacy.risky_settings(config, external=True, opted_in=True)[0].endswith(allowed)
    assert privacy.risky_settings(SAFE, external=False, opted_in=True) == []  # nothing on, nothing to say
