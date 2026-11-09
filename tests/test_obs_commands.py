"""OBS chat commands: the registry contract, levels and the owner's-channel rule on the real ``commands.Command``,
!brb, !back and !scene against FakeObs, and the commands through a connected adapter."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import replace

import pytest

from fake_obs import FakeObs, Item
from harness import closed_port
from tallyhand import commands, obs, roles
from tallyhand.commands import Command
from tallyhand.obs_commands import MAX_REPLY, chat_commands, register_commands
from tallyhand.roles import Role, role_from_tags

PASSWORD = "c0rrect-h0rse"
OWNER_CHANNEL = "chan"


def cmd(*, role: Role | None = None, is_owner: bool = False, user_id: str = "42", login: str = "someone",
        channel: str = OWNER_CHANNEL) -> Command:
    """A command as the adapter builds it for a chat line in ``channel``."""
    return Command(name="x", args="", channel=channel, user_id=user_id, login=login, display_name=login.title(),
                   role=role or Role(), is_owner=is_owner)


def badged(*names: str) -> Role:
    """The role chat gives a viewer who wears these badges (and the matching tags)."""
    return role_from_tags("1000", "42", dict.fromkeys(names, "1"), dict.fromkeys(names, "1"))


VIEWERS = {
    "viewer": cmd(), "subscriber": cmd(role=badged("subscriber")), "vip": cmd(role=badged("vip")),
    "vip_subscriber": cmd(role=badged("vip", "subscriber")),
}
MODERATORS = {
    "moderator": cmd(role=Role(moderator=True), user_id="47", login="mo"),
    "broadcaster": cmd(role=Role(broadcaster=True), user_id="3000", login="friend"),  # of another channel
}
OWNER = cmd(role=Role(broadcaster=True), is_owner=True, user_id="1000", login="streamer")


def as_cmd(base: Command, args: str = "", channel: str | None = None) -> Command:
    return replace(base, args=args, channel=base.channel if channel is None else channel)


@pytest.fixture(autouse=True)
def brb_scene(monkeypatch):
    monkeypatch.setenv(obs.BRB_SCENE_ENV, "BRB")


@pytest.fixture
async def fake(monkeypatch):
    server = await FakeObs().start()
    monkeypatch.setenv(obs.URL_ENV, server.url)
    yield server
    await server.stop()


@pytest.fixture
def owner_channel(monkeypatch):
    """The running adapter's owner channel, as ``roles.owner_channel()`` reports it."""
    monkeypatch.setattr(roles, "owner_channel", lambda: OWNER_CHANNEL)


@pytest.fixture
def handlers(owner_channel):
    return {name: handler for name, (handler, *_) in chat_commands().items()}


# contract, levels and channels

def test_chat_commands_contract():
    table = chat_commands()
    assert {name: min_role for name, (_, min_role, _, _) in table.items()} == {
        "brb": roles.MODERATOR, "back": roles.MODERATOR, "scene": roles.OWNER}
    for handler, _min_role, help_text, available in table.values():
        assert inspect.iscoroutinefunction(handler) and list(inspect.signature(handler).parameters) == ["cmd"]
        assert help_text and "\n" not in help_text
        assert callable(available)
    assert "!scene <name>" in table["scene"][2]


def test_chat_commands_go_through_the_command_registry():
    register_commands()
    specs = commands.registered()
    assert {name: specs[name].min_role for name in ("brb", "back", "scene")} == {
        "brb": "moderator", "back": "moderator", "scene": "owner"}
    assert commands.parse("!scene Just Chatting") == (specs["scene"], "Just Chatting")


@pytest.mark.parametrize("name, args", [("brb", ""), ("back", ""), ("scene", "Gaming"), ("scene", "")])
async def test_viewers_get_no_reply_and_obs_is_not_touched(fake, handlers, name, args):
    assert await handlers[name](as_cmd(VIEWERS["vip_subscriber"], args)) is None
    assert fake.connections == 0 and fake.program == "Main"


@pytest.mark.parametrize("who", list(MODERATORS))
@pytest.mark.parametrize("args", ["Gaming", ""])
async def test_scene_is_for_the_owner_only(fake, handlers, who, args):
    assert await handlers["scene"](as_cmd(MODERATORS[who], args)) is None
    assert fake.connections == 0 and fake.program == "Main"


@pytest.mark.parametrize("name", ["brb", "back"])
@pytest.mark.parametrize("who", [MODERATORS["moderator"], MODERATORS["broadcaster"], OWNER])
async def test_brb_and_back_answer_only_in_the_owners_channel(fake, handlers, monkeypatch, name, who):
    monkeypatch.setenv(obs.LIVE_SCENE_ENV, "Gaming")
    for channel in ("friend", "#friend", "chan2", ""):
        assert await handlers[name](as_cmd(who, channel=channel)) is None
    assert fake.connections == 0 and fake.program == "Main"
    assert await handlers[name](as_cmd(who, channel="#Chan")) in ("Switched to BRB.", "Back to Gaming.")
    assert fake.connections == 1


async def test_brb_and_back_stay_quiet_without_a_running_adapter(fake):
    table = chat_commands()  # roles.owner_channel() is empty: no adapter runs in this process
    for name in ("brb", "back"):
        assert await table[name][0](MODERATORS["moderator"]) is None
    assert fake.connections == 0


async def test_the_owner_switches_scenes_from_any_channel(fake, handlers):
    assert await handlers["scene"](as_cmd(OWNER, "Gaming", channel="friend")) == "Switched to Gaming."
    assert fake.program == "Gaming"


async def test_commands_through_a_connected_adapter(harness, twitch, fake):
    register_commands()
    await harness.connected(channels="#chan,#friend")
    owner = twitch.room_id("#chan")
    mod = {"login": "mo", "user_id": "47", "mod": True, "badges": "moderator/1"}
    await twitch.privmsg("#friend", "!brb", **mod)  # mo moderates #friend too, but OBS is #chan's
    await twitch.privmsg("#friend", "!back", **mod)
    await asyncio.sleep(0.3)
    assert fake.connections == 0 and twitch.privmsgs() == []
    await twitch.privmsg("#chan", "!brb", **mod)
    await twitch.wait_for(lambda: len(twitch.privmsgs()) == 1)
    assert fake.program == "BRB" and twitch.privmsgs()[-1].endswith("PRIVMSG #chan :Switched to BRB.")
    await twitch.privmsg("#chan", "!scene Gaming", **mod)
    await twitch.privmsg("#chan", "!back", login="alice", user_id="42")
    await twitch.privmsg("#friend", "!scene Gaming", login="chan", user_id=owner)
    await twitch.wait_for(lambda: len(twitch.privmsgs()) == 2)
    await asyncio.sleep(0.2)
    assert len(twitch.privmsgs()) == 2 and twitch.privmsgs()[-1].endswith("PRIVMSG #friend :Switched to Gaming.")
    assert fake.program == "Gaming"


# !brb and !back

@pytest.mark.parametrize("cmd", [*MODERATORS.values(), OWNER])
async def test_brb_and_back(fake, handlers, cmd):
    assert await handlers["brb"](cmd) == "Switched to BRB."
    assert fake.program == "BRB"
    assert await handlers["back"](cmd) == "Back to Main."
    assert fake.program == "Main"


async def test_brb_twice_keeps_the_scene_it_first_left(fake, handlers):
    mod = MODERATORS["moderator"]
    fake.program = "Gaming"
    assert await handlers["brb"](mod) == "Switched to BRB."
    assert await handlers["brb"](mod) == "Already on BRB."
    assert await handlers["back"](mod) == "Back to Gaming."
    assert fake.program == "Gaming"


async def test_brb_matches_the_scene_name_case(fake, handlers, monkeypatch):
    monkeypatch.setenv(obs.BRB_SCENE_ENV, "  brb ")
    assert await handlers["brb"](MODERATORS["moderator"]) == "Switched to BRB."


async def test_brb_without_a_configured_scene(fake, handlers, monkeypatch):
    monkeypatch.setenv(obs.BRB_SCENE_ENV, "  ")
    assert await handlers["brb"](MODERATORS["moderator"]) == (
        "No BRB scene is set; the streamer can set TALLYHAND_BRB_SCENE.")
    assert fake.connections == 0


async def test_brb_scene_missing_in_obs(fake, handlers, monkeypatch):
    monkeypatch.setenv(obs.BRB_SCENE_ENV, "Pause")
    assert await handlers["brb"](MODERATORS["moderator"]) == 'Scene "Pause" does not exist.'
    assert fake.program == "Main"


async def test_back_prefers_the_live_scene_setting(fake, handlers, monkeypatch):
    monkeypatch.setenv(obs.LIVE_SCENE_ENV, "Gaming")
    mod = MODERATORS["moderator"]
    assert await handlers["brb"](mod) == "Switched to BRB."
    assert await handlers["back"](mod) == "Back to Gaming."
    assert await handlers["back"](mod) == "Already on Gaming."


async def test_back_with_nowhere_to_go(fake, handlers):
    assert await handlers["back"](MODERATORS["moderator"]) == (
        "No live scene is set and there is no scene to go back to.")
    assert fake.connections == 0


async def test_back_uses_the_remembered_scene_once(fake, handlers):
    mod = MODERATORS["moderator"]
    await handlers["brb"](mod)
    assert await handlers["back"](mod) == "Back to Main."
    assert await handlers["back"](mod) == "No live scene is set and there is no scene to go back to."


async def test_each_command_table_remembers_its_own_scene(fake, owner_channel):
    first, second = chat_commands(), chat_commands()
    mod = MODERATORS["moderator"]
    assert await first["brb"][0](mod) == "Switched to BRB."
    assert await second["back"][0](mod) == "No live scene is set and there is no scene to go back to."
    assert await first["back"][0](mod) == "Back to Main."


# !scene

@pytest.mark.parametrize("args, reply, program", [
    ("Gaming", "Switched to Gaming.", "Gaming"),
    ("  gaming  ", "Switched to Gaming.", "Gaming"),
    ("Main", "Already on Main.", "Main"),
    ("Nope", 'Scene "Nope" does not exist.', "Main"),
])
async def test_scene(fake, handlers, args, reply, program):
    assert await handlers["scene"](as_cmd(OWNER, args)) == reply
    assert fake.program == program


async def test_scene_without_a_name_lists_the_scenes(fake, handlers):
    assert await handlers["scene"](OWNER) == "Live: Main. Scenes: Main, BRB, Gaming"
    assert fake.request_types() == ["GetSceneList"]


async def test_scene_list_with_no_scenes(fake, handlers):
    fake.scenes.clear()
    assert await handlers["scene"](OWNER) == "OBS has no scenes."


async def test_replies_stay_one_short_line(fake, handlers):
    for index in range(40):
        name = f"Scene number {index} with a long name\nand a second line"
        fake.scenes[name] = [Item("Camera", 1)]
        fake.scene_uuids[name] = str(index)
    reply = await handlers["scene"](OWNER)
    assert len(reply) <= MAX_REPLY and "\n" not in reply and reply.endswith("...")
    reply = await handlers["scene"](as_cmd(OWNER, "x" * 400 + "\n!ban someone"))
    assert len(reply) <= MAX_REPLY and "\n" not in reply and reply.startswith('Scene "xxx')


# failures

async def test_obs_down(monkeypatch, handlers, caplog):
    caplog.set_level(logging.INFO)
    port = closed_port()
    monkeypatch.setenv(obs.URL_ENV, f"ws://127.0.0.1:{port}")
    for name, cmd in (("brb", MODERATORS["moderator"]), ("scene", as_cmd(OWNER, "Gaming"))):
        reply = await handlers[name](cmd)
        assert reply == "OBS is not running or its WebSocket server is off."
        assert str(port) not in reply and "127.0.0.1" not in reply
    assert f"127.0.0.1:{port}" in caplog.text


async def test_wrong_password(fake, handlers, monkeypatch):
    fake.password = PASSWORD
    monkeypatch.setenv(obs.PASSWORD_ENV, "Wr0ng-pass")
    assert await handlers["brb"](MODERATORS["moderator"]) == "OBS rejected the password."
    monkeypatch.setenv(obs.PASSWORD_ENV, PASSWORD)
    assert await handlers["brb"](MODERATORS["moderator"]) == "Switched to BRB."


async def test_password_not_set(fake, handlers):
    fake.password = PASSWORD
    assert await handlers["brb"](MODERATORS["moderator"]) == "OBS asks for a password; set OBS_WEBSOCKET_PASSWORD."


async def test_remote_url_refused(handlers, monkeypatch):
    monkeypatch.setenv(obs.URL_ENV, "ws://192.168.1.20:4455")
    reply = await handlers["brb"](MODERATORS["moderator"])
    assert reply == ("OBS_WEBSOCKET_URL must point at this computer (127.0.0.1, ::1 or localhost) unless "
                     "TALLYHAND_OBS_ALLOW_REMOTE=true.")


async def test_obs_comments_and_secrets_stay_out_of_chat(fake, handlers, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    fake.password, fake.echo_auth = PASSWORD, True
    monkeypatch.setenv(obs.PASSWORD_ENV, PASSWORD)
    fake.fail["GetSceneList"] = (205, "Internal path C:\\Users\\streamer\\obs")
    reply = await handlers["scene"](OWNER)
    assert reply == "OBS refused GetSceneList."
    sent = fake.identifies[-1]["authentication"]
    for secret in (PASSWORD, sent, "streamer\\obs"):
        assert secret not in reply
    assert "OBS refused GetSceneList" in caplog.text and "***" in caplog.text
    assert PASSWORD not in caplog.text and sent not in caplog.text


# !commands


@pytest.fixture
async def listing(harness, twitch):
    """A connected adapter in #chan (the owner's) and #friend with the OBS commands registered as register() does;
    the bot moderates both, so replies are not paced."""
    register_commands()
    twitch.badges.update({"#chan": "moderator/1", "#friend": "moderator/1"})
    return await harness.connected(channels="#chan,#friend")


async def commands_seen(twitch, channel: str, **who) -> set[str]:
    before = len(twitch.privmsgs())
    await twitch.privmsg(channel, "!commands", **who)
    await twitch.wait_for(lambda: len(twitch.privmsgs()) > before, timeout=5.0)
    return set(twitch.privmsgs()[-1].split("Commands: ", 1)[1].split(", "))


MOD = {"login": "mo", "user_id": "47", "mod": True, "badges": "moderator/1"}
OBS_COMMANDS = {"!brb", "!back", "!scene"}


async def test_commands_list_brb_and_back_only_in_the_owners_channel(listing, twitch, fake):
    assert {"!brb", "!back"} <= await commands_seen(twitch, "#chan", **MOD)
    assert not OBS_COMMANDS & await commands_seen(twitch, "#friend", **MOD)
    owner = {"login": "chan", "user_id": twitch.room_id("#chan"), "badges": "broadcaster/1"}
    assert OBS_COMMANDS <= await commands_seen(twitch, "#chan", **owner)
    assert OBS_COMMANDS & await commands_seen(twitch, "#friend", **owner) == {"!scene"}


async def test_without_obs_the_obs_commands_are_hidden_and_quiet(listing, twitch):
    assert not obs.configured()
    owner = {"login": "chan", "user_id": twitch.room_id("#chan"), "badges": "broadcaster/1"}
    assert not OBS_COMMANDS & await commands_seen(twitch, "#chan", **owner)
    assert not OBS_COMMANDS & await commands_seen(twitch, "#chan", **MOD)
    before = len(twitch.privmsgs())
    await twitch.privmsg("#chan", "!brb", **MOD)
    await twitch.privmsg("#chan", "!scene", **owner)
    await asyncio.sleep(0.3)
    assert len(twitch.privmsgs()) == before


@pytest.mark.parametrize(("env", "configured"), [
    ({}, False), ({obs.PASSWORD_ENV: "x"}, True), ({obs.URL_ENV: "ws://127.0.0.1:4455"}, True),
    ({obs.URL_ENV: "ws://192.0.2.1:4455"}, False), ({obs.URL_ENV: "  "}, False),
    ({obs.URL_ENV: "ws://192.0.2.1:4455", obs.ALLOW_REMOTE_ENV: "true"}, True),
    ({obs.URL_ENV: "http://127.0.0.1:4455"}, False), ({obs.URL_ENV: "ws://127.0.0.1:notaport"}, False)])
def test_obs_configured(monkeypatch, env, configured):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert obs.configured() is configured
