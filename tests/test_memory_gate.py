"""Who may ask while the profile's memory is on. memory.memory_enabled, memory.user_profile_enabled or an external
memory.provider puts the owner's MEMORY.md and USER.md into every Twitch turn, or sends the turn to a memory provider,
so questions from anyone but the owner are refused until the owner turns those off or sets
allow_profile_memory_for_viewers. The settings are read from the config Hermes loads, again for every question."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

import bot_profile
from harness import MOD_ID, VIEWER_ID, Clock, until
from tallyhand import adapter as adapter_module
from tallyhand import privacy

OPT_IN = "allow_profile_memory_for_viewers"
REFUSAL = "refusing questions from anyone but the owner"
BASE = {"chat_tail": 12}  # settle() watches the chat tail, which the harness keeps on unless given settings of its own


def memory(**values) -> str:
    """A config.yaml with the three memory settings, off unless ``values`` says otherwise."""
    section = {"memory_enabled": False, "user_profile_enabled": False, "provider": "", **values}
    return "memory:\n" + "".join(f"  {key}: {json.dumps(value)}\n" for key, value in section.items())


@pytest.fixture
def profile(monkeypatch, tmp_path):
    """``use(config)`` makes the Hermes home of the running profile hold that config.yaml; None deletes it, which
    leaves Hermes' own defaults (memory and user profile on). Returns the home."""
    home = tmp_path / "profile"

    def use(config: str | None, at: Path | None = None) -> Path:
        target = at or home
        target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(target))
        if config is None:
            (target / "config.yaml").unlink(missing_ok=True)
        else:
            (target / "config.yaml").write_text(config, encoding="utf-8")
        return target

    return use


def people(twitch) -> dict[str, dict]:
    """The three roles in #chan: its broadcaster (the owner), a moderator and a viewer."""
    return {"owner": {"login": "chan", "user_id": twitch.room_id("#chan"), "badges": "broadcaster/1"},
            "mod": {"login": "mo", "user_id": MOD_ID, "mod": True, "badges": "moderator/1"},
            "viewer": {"login": "alice", "user_id": VIEWER_ID}}


async def settle(adapter, twitch) -> None:
    """Wait until every line sent so far is handled and the channel has no turn running or queued."""
    marker = await twitch.privmsg("#chan", "marker", login="observer", user_id="999")
    await until(lambda: any(line.message_id == marker for line in adapter._tail.lines("#chan")), what="marker")
    await until(lambda: adapter._gate.running("#chan") == 0 and adapter._gate.waiting("#chan") == 0, what="idle")


async def ask(adapter, twitch, who: str, text: str | None = None) -> None:
    """``who`` asks ``!hermes <text>`` (their role's name by default) and the adapter finishes with it."""
    await twitch.privmsg("#chan", f"!hermes {text or who}", **people(twitch)[who])
    await settle(adapter, twitch)


def asked(adapter) -> list[str]:
    return [event.text for event in adapter.recorder.events]


def refusals(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if REFUSAL in record.getMessage()]


# the gate

async def test_memory_on_bars_everyone_but_the_owner(harness, twitch, profile, caplog):
    profile(None)  # Hermes' defaults: memory and user profile on
    adapter = await harness.connected()
    caplog.clear()
    for who in ("viewer", "mod", "owner"):
        await ask(adapter, twitch, who)
    assert asked(adapter) == ["owner"]
    assert adapter._cooldowns.ready(("#chan", VIEWER_ID))  # a refused question starts no cooldown
    (line,) = refusals(caplog)  # one line for both refusals, and it names nobody
    assert "memory.memory_enabled, memory.user_profile_enabled" in line
    assert "alice" not in caplog.text.lower() and "mo " not in caplog.text and VIEWER_ID not in caplog.text


async def test_the_bot_profile_answers_everyone(harness, twitch, profile, caplog):
    profile(bot_profile.CONFIG)
    adapter = await harness.connected()
    for who in ("viewer", "mod", "owner"):
        await ask(adapter, twitch, who)
    assert asked(adapter) == ["viewer", "mod", "owner"]
    assert refusals(caplog) == []


@pytest.mark.parametrize(("values", "name"), [
    ({"memory_enabled": True}, "memory.memory_enabled"),
    ({"user_profile_enabled": True}, "memory.user_profile_enabled"),
    ({"provider": "honcho"}, "memory.provider")])
async def test_each_memory_setting_alone_bars(harness, twitch, profile, caplog, values, name):
    profile(memory(**values))
    adapter = await harness.connected()
    caplog.clear()
    await ask(adapter, twitch, "viewer")
    await ask(adapter, twitch, "owner")
    assert asked(adapter) == ["owner"]
    (line,) = refusals(caplog)
    assert f"({name})" in line


@pytest.mark.parametrize("provider", ["", "default", "builtin", "built-in", "none", " None "])
async def test_the_built_in_store_is_not_a_memory_provider(harness, twitch, profile, provider):
    profile(memory(provider=provider))
    adapter = await harness.connected()
    await ask(adapter, twitch, "viewer")
    assert asked(adapter) == ["viewer"]


async def test_the_refusal_is_logged_again_after_ten_minutes(harness, twitch, profile, caplog):
    profile(None)
    adapter = await harness.connected()
    adapter._clock = clock = Clock()
    caplog.clear()
    await ask(adapter, twitch, "viewer", "one")
    await ask(adapter, twitch, "mod", "two")
    assert len(refusals(caplog)) == 1
    clock.now += adapter_module.BARRED_LOG_PERIOD - 1
    await ask(adapter, twitch, "viewer", "three")
    assert len(refusals(caplog)) == 1
    clock.now += 2
    await ask(adapter, twitch, "viewer", "four")
    assert len(refusals(caplog)) == 2
    assert asked(adapter) == []


async def test_a_refused_question_takes_no_turn_slot(harness, twitch, profile):
    profile(None)
    adapter = await harness.connected(hold=True)
    for n in range(5):
        await twitch.privmsg("#chan", f"!hermes q{n}", login=f"viewer{n}", user_id=str(100 + n))
    await settle(adapter, twitch)
    assert asked(adapter) == [] and adapter._gate.running("#chan") == 0 and adapter._gate.waiting("#chan") == 0
    await twitch.privmsg("#chan", "!hermes mine", **people(twitch)["owner"])  # the owner still gets the slot at once
    await until(lambda: asked(adapter) == ["mine"], what="the owner's turn")


# the opt-in

@pytest.mark.parametrize(("settings", "extra"), [
    ({OPT_IN: True}, {}), ({OPT_IN: "true"}, {}), ({OPT_IN: "Yes"}, {}), ({}, {OPT_IN: True}), ({}, {OPT_IN: "on"})])
async def test_the_opt_in_answers_everyone_while_memory_is_on(harness, twitch, profile, settings, extra):
    profile(None)
    adapter = await harness.connected(settings={**BASE, **settings}, **extra)
    for who in ("viewer", "mod", "owner"):
        await ask(adapter, twitch, who)
    assert asked(adapter) == ["viewer", "mod", "owner"]


@pytest.mark.parametrize("value", [False, "false", "no", "off", "0", "maybe", "", "  ", None])
async def test_anything_but_true_leaves_the_gate_closed(harness, twitch, profile, value):
    profile(None)
    adapter = await harness.connected(settings={**BASE, OPT_IN: value})
    await ask(adapter, twitch, "viewer")
    assert asked(adapter) == []


async def test_the_plugin_setting_beats_the_platform_extra(harness, twitch, profile):
    profile(None)
    adapter = await harness.connected(settings={**BASE, OPT_IN: False}, **{OPT_IN: True})
    await ask(adapter, twitch, "viewer")
    assert asked(adapter) == []


async def test_the_opt_in_is_said_in_the_connect_warnings(harness, profile, caplog):
    profile(None)
    with caplog.at_level(logging.WARNING, logger="tallyhand.privacy"):
        await harness.connected(settings={OPT_IN: True})
    memory_lines = [r.getMessage() for r in caplog.records if "MEMORY.md" in r.getMessage()
                    or "USER.md" in r.getMessage()]
    assert len(memory_lines) == 2
    assert all(f"{OPT_IN} is on, so viewers' answers use it" in line for line in memory_lines)
    assert not any("refuses questions" in line for line in memory_lines)


async def test_the_connect_warnings_say_questions_are_refused(harness, profile, caplog):
    profile(None)
    with caplog.at_level(logging.WARNING, logger="tallyhand.privacy"):
        await harness.connected()
    memory_lines = [r.getMessage() for r in caplog.records if "MEMORY.md" in r.getMessage()
                    or "USER.md" in r.getMessage()]
    assert len(memory_lines) == 2
    assert all(line.endswith("; until then Tallyhand refuses questions from anyone but you") for line in memory_lines)


# the settings are read for every question, and fail closed

async def test_a_config_change_counts_at_the_next_question(harness, twitch, profile):
    profile(bot_profile.CONFIG)
    adapter = await harness.connected(settings={**BASE, "user_cooldown": 0})
    await ask(adapter, twitch, "viewer", "one")
    profile(None)  # memory switched on while the gateway runs
    await ask(adapter, twitch, "viewer", "two")
    profile(bot_profile.CONFIG + "# switched off again\n")
    await ask(adapter, twitch, "viewer", "three")
    profile(memory(provider="honcho"))
    await ask(adapter, twitch, "viewer", "four")
    assert asked(adapter) == ["one", "three"]


async def test_an_unreadable_config_fails_closed(harness, twitch, profile, monkeypatch, caplog):
    profile(bot_profile.CONFIG)
    adapter = await harness.connected()
    caplog.clear()

    def broken():
        raise RuntimeError("config.yaml is damaged")

    monkeypatch.setattr(privacy, "read_config", broken)
    await ask(adapter, twitch, "viewer")
    await ask(adapter, twitch, "owner")
    assert asked(adapter) == ["owner"]
    (line,) = refusals(caplog)
    assert "could not be read (RuntimeError)" in line and OPT_IN in line


async def test_an_unreadable_config_does_not_stop_the_opt_in(harness, twitch, profile, monkeypatch):
    profile(bot_profile.CONFIG)
    adapter = await harness.connected(settings={**BASE, OPT_IN: True})
    monkeypatch.setattr(privacy, "read_config", lambda: (_ for _ in ()).throw(OSError("gone")))
    await ask(adapter, twitch, "viewer")
    assert asked(adapter) == ["viewer"]


async def test_a_queued_question_is_checked_again_when_it_runs(harness, twitch, profile, caplog):
    profile(bot_profile.CONFIG)
    adapter = await harness.connected(hold=True, settings={"user_cooldown": 0})
    await twitch.privmsg("#chan", "!hermes first", login="alice", user_id=VIEWER_ID)
    await until(lambda: adapter.recorder.open, what="the first turn")
    await twitch.privmsg("#chan", "!hermes second", login="bob", user_id="43")
    await until(lambda: adapter._gate.waiting("#chan") == 1, what="the second question waiting")
    profile(None)  # memory is switched on while it waits
    await adapter.recorder.finish()
    await until(lambda: adapter._gate.running("#chan") == 0 and adapter._gate.waiting("#chan") == 0, what="idle")
    assert asked(adapter) == ["first"]
    assert len(refusals(caplog)) == 1


async def test_the_owner_queued_behind_a_viewer_still_runs(harness, twitch, profile):
    profile(bot_profile.CONFIG)
    adapter = await harness.connected(hold=True, settings={"user_cooldown": 0})
    await twitch.privmsg("#chan", "!hermes first", login="alice", user_id=VIEWER_ID)
    await until(lambda: adapter.recorder.open, what="the first turn")
    await twitch.privmsg("#chan", "!hermes mine", **people(twitch)["owner"])
    await until(lambda: adapter._gate.waiting("#chan") == 1, what="the owner's question waiting")
    profile(None)
    await adapter.recorder.finish()
    await until(lambda: len(adapter.recorder.events) == 2, what="the owner's turn")
    assert asked(adapter) == ["first", "mine"]


# what else follows the gate

async def test_a_barred_viewer_gets_no_hint_and_no_notes_but_the_commands(harness, twitch, profile):
    profile(None)
    adapter = await harness.connected()
    await twitch.privmsg("#chan", "!hermes", login="alice", user_id=VIEWER_ID)
    await twitch.privmsg("#chan", "!remember I play on PC", login="alice", user_id=VIEWER_ID)
    await settle(adapter, twitch)
    await asyncio.sleep(0.3)
    assert twitch.privmsgs() == []
    assert adapter._notes is not None and adapter._notes.notes(twitch.room_id("#chan"), VIEWER_ID) == []
    await twitch.privmsg("#chan", "!privacy", login="alice", user_id=VIEWER_ID)
    await twitch.wait_for(lambda: twitch.privmsgs(), timeout=5.0)  # commands that need no model call stay


async def test_an_asking_viewer_gets_the_hint_and_notes(harness, twitch, profile):
    profile(bot_profile.CONFIG)
    adapter = await harness.connected()
    await twitch.privmsg("#chan", "!hermes", login="alice", user_id=VIEWER_ID)
    await twitch.wait_for(lambda: len(twitch.privmsgs()) == 1, timeout=5.0)
    await twitch.privmsg("#chan", "!remember I play on PC", login="alice", user_id=VIEWER_ID)
    await twitch.wait_for(lambda: len(twitch.privmsgs()) == 2, timeout=5.0)
    assert adapter._notes.notes(twitch.room_id("#chan"), VIEWER_ID) == ["I play on PC"]


# the settings, read from the config Hermes loads

@pytest.mark.parametrize(("config", "settings"), [
    (None, ("memory.memory_enabled", "memory.user_profile_enabled")),
    (bot_profile.CONFIG, ()),
    (memory(memory_enabled=True), ("memory.memory_enabled",)),
    (memory(user_profile_enabled=True, provider="honcho"), ("memory.user_profile_enabled", "memory.provider")),
    (memory(memory_enabled=True, user_profile_enabled=True, provider="honcho"), tuple(privacy.MEMORY_FIXES))])
def test_memory_state_reads_the_running_profile(profile, config, settings):
    profile(config)
    state = privacy.memory_state()
    assert state.settings == settings and state.on is bool(settings) and state.unreadable == ""


def test_memory_state_names_the_profile_and_its_fixes(profile, tmp_path):
    profile(None, at=tmp_path / "root" / "profiles" / "tallyhand")
    state = privacy.memory_state()
    assert state.profile == "tallyhand"
    line = state.refusal()
    assert "hermes -p tallyhand config set memory.memory_enabled false" in line
    assert "hermes -p tallyhand config set memory.user_profile_enabled false" in line
    assert f"or set {OPT_IN}: true to answer them anyway" in line and "\n" not in line


def test_the_provider_fix_is_hermes_memory_off(profile):
    profile(memory(provider="honcho"))
    assert "hermes memory off" in privacy.memory_state().refusal()


@pytest.mark.parametrize("error", [ImportError, OSError, ValueError, TypeError, AttributeError, KeyError,
                                   RuntimeError])
def test_memory_state_never_raises(monkeypatch, error):
    def broken():
        raise error("boom")

    monkeypatch.setattr(privacy, "read_config", broken)
    state = privacy.memory_state()
    assert state.on and state.unreadable == error.__name__ and state.settings == ()
    assert f"could not be read ({error.__name__})" in state.refusal()


def test_the_memory_settings_read_like_risky_settings(profile):
    for config in (None, bot_profile.CONFIG, memory(provider="honcho"), memory(memory_enabled=True)):
        profile(config)
        loaded, external, _ = privacy.read_config()
        on = privacy.memory_settings_on(loaded, external=external)
        warned = [w for w in privacy.risky_settings(loaded, external=external) if "MEMORY.md" in w or "USER.md" in w
                  or "memory provider" in w]
        assert len(warned) == len(on)
