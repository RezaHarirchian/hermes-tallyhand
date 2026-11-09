"""Chat commands: the registry API, the built-ins and their role gates, and how the adapter answers them."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from types import SimpleNamespace

import pytest

from harness import PRIVACY_LIMIT, TOKEN, until
from tallyhand import auth, commands, helix, roles
from tallyhand.commands import Command, add_command

MOD = {"mod": True, "badges": "moderator/1"}
# The adapters here keep 12 lines of chat tail and viewer notes on.
PRIVACY = commands.privacy_line(SimpleNamespace(enabled=True), 12)


@pytest.fixture
async def chat(harness, twitch, helix_api):
    """A connected adapter with Helix pointed at the fake; the bot moderates #chan, so replies are not paced to one
    a second, and the channel's broadcaster exists on Helix."""
    twitch.badges["#chan"] = "moderator/1"
    adapter = await harness.connected(_test_helix_url=helix_api.url)
    helix_api.add_user("chan", twitch.room_id("#chan"), "Chan")
    helix_api.add_user("friend", "3000", "FriendlyStreamer")
    return adapter


async def replies(twitch, count: int = 1) -> list[str]:
    await twitch.wait_for(lambda: len(twitch.privmsgs()) >= count, timeout=5.0)
    await asyncio.sleep(0.05)
    return twitch.texts()


async def ask(twitch, text: str, **kwargs) -> str:
    """Send one chat line and return the bot's answer to it."""
    before = len(twitch.privmsgs())
    await twitch.privmsg("#chan", text, **{"login": "alice", "user_id": "42", **kwargs})
    await twitch.wait_for(lambda: len(twitch.privmsgs()) > before, timeout=5.0)
    return twitch.texts()[-1]


async def quiet(twitch, text: str, **kwargs) -> bool:
    """Send one chat line; True when the bot stays quiet."""
    before = len(twitch.privmsgs())
    await twitch.privmsg("#chan", text, **{"login": "alice", "user_id": "42", **kwargs})
    await asyncio.sleep(0.3)
    return len(twitch.privmsgs()) == before


def iso(seconds_ago: float) -> str:
    moment = datetime.datetime.fromtimestamp(time.time() - seconds_ago, tz=datetime.UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# registry


def test_builtins_and_their_levels():
    levels = {name: spec.min_role for name, spec in commands.registered().items()}
    assert levels == {"uptime": "viewer", "bot": "viewer", "commands": "viewer", "privacy": "viewer",
                      "so": "moderator", "clip": "moderator", "remember": "viewer", "mynotes": "viewer",
                      "forgetme": "viewer"}
    assert all(spec.help for spec in commands.registered().values())


async def test_add_command_api():
    async def scene(cmd):
        return f"{cmd.args}|{cmd.channel}|{cmd.level}"

    add_command("!Scene", scene, min_role="owner", help="switch scene")
    spec, args = commands.parse("!scene  intro please ")
    assert (spec.name, spec.min_role, spec.help, args) == ("scene", "owner", "switch scene", "intro please")
    assert commands.parse("!SCENE") is not None and commands.parse("scene") is None
    assert commands.parse("!scenery") is None
    add_command("scene", scene)
    assert commands.registered()["scene"].min_role == "viewer"


@pytest.mark.parametrize(("name", "min_role", "handler", "error"), [
    ("", "viewer", None, ValueError), ("two words", "viewer", None, ValueError), ("x" * 26, "viewer", None, ValueError),
    ("ok", "admin", None, ValueError), ("ok", "viewer", "not callable", TypeError)])
def test_add_command_rejects(name, min_role, handler, error):
    async def noop(cmd):
        return None

    with pytest.raises(error):
        add_command(name, noop if handler is None else handler, min_role=min_role)


def test_levels_and_allowed():
    viewer = Command("x", "", "chan", "42", "alice", "Alice", roles.Role(), is_owner=False)
    mod = Command("x", "", "chan", "47", "mo", "Mo", roles.Role(moderator=True), is_owner=False)
    caster = Command("x", "", "friend", "3000", "friend", "Friend", roles.Role(broadcaster=True), is_owner=False)
    owner = Command("x", "", "friend", "2000", "chan", "Chan", roles.Role(), is_owner=True)
    assert [c.level for c in (viewer, mod, caster, owner)] == ["viewer", "moderator", "moderator", "owner"]
    spec = commands.registered()["so"]
    assert [commands.allowed(spec, c) for c in (viewer, mod, caster, owner)] == [False, True, True, True]


# built-ins in chat


async def test_bot_and_privacy(chat, twitch):
    asked = await twitch.privmsg("#chan", "!bot", login="alice", user_id="42")
    await twitch.privmsg("#chan", "!privacy", login="bob", user_id="43")
    lines = await replies(twitch, 2)
    assert lines == [commands.BOT_LINE.format(channel="chan"), PRIVACY]
    assert "automated assistant" in lines[0] and "Tallyhand" in lines[0] and "not a person" in lines[0]
    assert twitch.privmsgs()[0].startswith(f"@reply-parent-msg-id={asked} PRIVMSG #chan :")
    assert chat.recorder.events == []  # no model call
    assert chat._tail.lines("#chan") == []  # commands are not chat context


async def test_commands_lists_what_the_caller_may_use(chat, twitch):
    await twitch.privmsg("#chan", "!commands", login="alice", user_id="42")
    await twitch.privmsg("#chan", "!commands", login="mo", user_id="47", **MOD)
    assert await replies(twitch, 2) == [
        "Commands: !bot, !commands, !forgetme, !mynotes, !privacy, !remember, !uptime",
        "Commands: !bot, !clip, !commands, !forgetme, !mynotes, !privacy, !remember, !so, !uptime"]


async def test_uptime(chat, twitch, helix_api):
    room = twitch.room_id("#chan")
    assert await ask(twitch, "!uptime") == "chan is offline right now."
    helix_api.go_live(room, started_at=iso(4530))
    later = time.monotonic() + commands.CHANNEL_COOLDOWN
    chat._clock = lambda: later  # past the channel's cooldown for !uptime
    chat.helix.clock = lambda: time.time() + helix.STREAM_TTL  # and past the cached stream info
    assert await ask(twitch, "!UPTIME", login="bob", user_id="43") == "chan has been live for 1h 15m."
    assert [seen.query for seen in helix_api.calls("streams")] == [{"user_id": [room]}] * 2


async def test_shoutout_is_for_moderators(chat, twitch, helix_api):
    room = twitch.room_id("#chan")
    helix_api.go_live(room)
    await twitch.privmsg("#chan", "!so friend", login="alice", user_id="42")
    await asyncio.sleep(0.1)
    assert twitch.privmsgs() == [] and helix_api.calls("chat/shoutouts") == []  # ignored silently
    await twitch.privmsg("#chan", "!so @Friend thanks for the raid", login="mo", user_id="47", **MOD)
    assert await replies(twitch) == ["Go check out FriendlyStreamer at https://twitch.tv/friend"]
    assert helix_api.shoutouts == [(room, "3000")]


async def test_shoutout_edge_cases(chat, twitch, helix_api, caplog):
    mod = {"login": "mo", "user_id": "47", **MOD}
    assert await ask(twitch, "!so", **mod) == "Usage: !so <login>"
    assert await ask(twitch, "!so not/a/login", **mod) == "Usage: !so <login>"
    assert await ask(twitch, "!so nobody_here", **mod) == "There is no Twitch account named nobody_here."
    # Offline: Twitch refuses the shoutout itself, the chat line still goes out.
    assert await ask(twitch, "!so friend", **mod) == "Go check out FriendlyStreamer at https://twitch.tv/friend"
    assert "Twitch shoutout for friend in chan failed" in caplog.text
    assert await quiet(twitch, "!so chan", **mod)
    assert helix_api.shoutouts == []


async def test_clip(chat, twitch, helix_api):
    assert await quiet(twitch, "!clip")
    assert await ask(twitch, "!clip", login="mo", user_id="47", **MOD) == "Clips work only while the stream is live."
    helix_api.go_live(twitch.room_id("#chan"))
    answer = await ask(twitch, "!clip", login="chan", user_id=twitch.room_id("#chan"), badges="broadcaster/1")
    assert answer == f"Clip: https://clips.twitch.tv/{helix_api.clips[0]}"


async def test_clip_without_the_scope_says_little_in_chat(chat, twitch, helix_api, caplog):
    helix_api.go_live(twitch.room_id("#chan"))
    chat.helix.remember(auth.TokenInfo("tallybot", "1000", ("chat:read", "chat:edit"), 5_000_000, "fakeclientid"))
    assert await ask(twitch, "!clip", login="mo", user_id="47", **MOD) == "Twitch did not make a clip this time."
    assert "clips:edit" in caplog.text and helix_api.calls("clips") == []


# adapter behaviour


async def test_chat_commands_off(harness, twitch):
    adapter = await harness.connected(settings={"chat_tail": 12, "chat_commands": False})
    await twitch.privmsg("#chan", "!bot", login="alice", user_id="42")
    await until(lambda: adapter._tail.lines("#chan"), what="tail")
    await asyncio.sleep(0.05)
    assert twitch.privmsgs() == [] and adapter.recorder.events == []


@pytest.mark.parametrize(("value", "on"), [(True, True), (False, False), ("false", False), ("no", False),
                                          ("TRUE", True), ("maybe", True), (None, True)])
def test_chat_commands_setting_values(harness, value, on):
    adapter = harness.adapter(settings={"chat_commands": value} if value is not None else {})
    assert adapter._chat_commands is on


async def test_per_viewer_command_cooldown(chat, twitch):
    for _ in range(2):
        await twitch.privmsg("#chan", "!bot", login="alice", user_id="42")
        await twitch.privmsg("#chan", "!privacy", login="mo", user_id="47", **MOD)
    await twitch.privmsg("#chan", "!privacy", login="bob", user_id="43")
    lines = await replies(twitch, 4)
    assert lines.count(commands.BOT_LINE.format(channel="chan")) == 1  # alice waits out the cooldown
    assert lines.count(PRIVACY) == 3  # moderators are exempt; bob has his own timer


async def test_viewers_share_one_answer_per_command_and_channel(chat, twitch, helix_api):
    assert await ask(twitch, "!bot") == commands.BOT_LINE.format(channel="chan")
    assert await quiet(twitch, "!bot", login="bob", user_id="43")  # alice's answer is still on screen
    assert await ask(twitch, "!privacy", login="bob", user_id="43") == PRIVACY  # another command
    assert await ask(twitch, "!bot", login="mo", user_id="47", **MOD) == commands.BOT_LINE.format(channel="chan")
    later = time.monotonic() + commands.CHANNEL_COOLDOWN
    chat._clock = lambda: later
    assert await ask(twitch, "!bot", login="carol", user_id="44") == commands.BOT_LINE.format(channel="chan")


async def test_a_crowd_typing_uptime_costs_one_helix_call(chat, twitch, helix_api):
    helix_api.go_live(twitch.room_id("#chan"))
    for n in range(10):
        await twitch.privmsg("#chan", "!uptime", login=f"viewer{n}", user_id=str(500 + n))
    await replies(twitch, 1)
    await asyncio.sleep(0.2)
    assert len(twitch.privmsgs()) == 1 and len(helix_api.calls("streams")) == 1


async def test_command_cooldown_is_separate_from_the_question_cooldown(chat, twitch):
    await twitch.privmsg("#chan", "!hermes first question", login="alice", user_id="42")
    await until(lambda: chat.recorder.events, what="question")
    await twitch.privmsg("#chan", "!bot", login="alice", user_id="42")
    assert await replies(twitch) == [commands.BOT_LINE.format(channel="chan")]
    await twitch.privmsg("#chan", "!hermes second question", login="alice", user_id="42")
    await asyncio.sleep(0.1)
    assert len(chat.recorder.events) == 1  # the question cooldown still runs


async def test_commands_take_no_turn_slot(harness, twitch, helix_api):
    twitch.badges["#chan"] = "moderator/1"
    adapter = await harness.connected(hold=True, _test_helix_url=helix_api.url)
    await twitch.privmsg("#chan", "!hermes a long one", login="alice", user_id="42")
    await until(lambda: adapter.recorder.open, what="running turn")
    for login, user_id, text in (("bob", "43", "!bot"), ("carol", "44", "!privacy"), ("dave", "45", "!commands"),
                                 ("erin", "46", "!uptime")):
        await twitch.privmsg("#chan", text, login=login, user_id=user_id)
    await replies(twitch, 4)
    assert adapter._gate.running("#chan") == 1 and adapter._gate.waiting("#chan") == 0


async def test_triggers_keep_priority(chat, twitch):
    async def hijack(cmd):
        return "hijacked"

    add_command("hermes", hijack)
    add_command("h", hijack)
    await twitch.privmsg("#chan", "!hermes what game is this?", login="alice", user_id="42")
    await twitch.privmsg("#chan", "@tallybot !bot", login="bob", user_id="43")
    await until(lambda: len(chat.recorder.events) == 2, what="questions")
    assert [e.text for e in chat.recorder.events] == ["what game is this?", "!bot"]
    await asyncio.sleep(0.05)
    assert twitch.privmsgs() == []


async def test_reply_mention_is_dropped_before_a_command(chat, twitch):
    parent = {"msg-id": "b34ccfc7-4977-403a-8a94-33c6bac34fb8", "user-id": "43", "user-login": "bob",
              "msg-body": "what is this bot?"}
    assert await ask(twitch, "@bob !privacy", reply_to=parent) == PRIVACY
    assert chat.recorder.events == []


async def test_unknown_commands_are_plain_chat(chat, twitch):
    await twitch.privmsg("#chan", "!discord", login="alice", user_id="42")
    await until(lambda: chat._tail.lines("#chan"), what="tail")
    assert twitch.privmsgs() == [] and chat.recorder.events == []


async def test_a_plugged_in_command_with_an_owner_gate(chat, twitch):
    seen: list[Command] = []

    async def scene(cmd):
        seen.append(cmd)
        return None if cmd.args == "quiet" else f"scene {cmd.args}"

    add_command("scene", scene, min_role="owner", help="switch scene")
    room = twitch.room_id("#chan")
    await twitch.privmsg("#chan", "!scene intro", login="mo", user_id="47", **MOD)
    await twitch.privmsg("#chan", "!scene quiet", login="chan", user_id=room, badges="broadcaster/1",
                         display_name="ChanTV")
    await twitch.privmsg("#chan", "!scene intro", login="chan", user_id=room, badges="broadcaster/1")
    assert await replies(twitch) == ["scene intro"]
    cmd = seen[0]
    assert (cmd.args, cmd.channel, cmd.user_id, cmd.login, cmd.display_name, cmd.is_owner) == (
        "quiet", "chan", room, "chan", "ChanTV", True)
    assert cmd.role.broadcaster and cmd.room_id == room and cmd.helix is chat.helix
    assert len(seen) == 2  # the moderator was ignored before the handler ran


async def test_a_failing_handler_does_not_break_chat(chat, twitch, caplog):
    async def broken(cmd):
        raise RuntimeError("boom")

    add_command("broken", broken)
    await twitch.privmsg("#chan", "!broken", login="alice", user_id="42")
    await until(lambda: "background task failed" in caplog.text, what="logged failure")
    await twitch.privmsg("#chan", "!bot", login="bob", user_id="43")
    assert await replies(twitch) == [commands.BOT_LINE.format(channel="chan")]


async def test_no_token_or_client_id_in_command_logs(chat, twitch, helix_api, caplog):
    caplog.set_level(logging.DEBUG)

    async def no_wait(seconds):
        return None

    chat.helix.sleep, chat.helix.retry_delay = no_wait, 0.0
    helix_api.go_live(twitch.room_id("#chan"))
    for status in (401, 403, 429, 500):
        for text, failure in (("!uptime", "!uptime in chan failed"), ("!clip", "!clip in chan failed"),
                              ("!so friend", "!so in chan failed")):
            helix_api.forced.clear()
            helix_api.force(status, times=4, reset=time.time() - 1, message=f"echo {TOKEN} fakeclientid")
            chat.helix.clock = lambda: time.time() + 3600  # no cached stream info from an earlier round
            before = caplog.text.count(failure)
            await twitch.privmsg("#chan", text, login="mo", user_id="47", **MOD)
            await until(lambda failure=failure, before=before: caplog.text.count(failure) > before,
                        what=f"{text} failing on {status}")
    assert TOKEN not in caplog.text and "fakeclientid" not in caplog.text
    lines = " ".join(twitch.privmsgs())
    assert TOKEN not in lines and "fakeclientid" not in lines


@pytest.mark.parametrize("notes", [None, SimpleNamespace(enabled=True), SimpleNamespace(enabled=False)])
@pytest.mark.parametrize("chat_tail", [0, 12])
@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("shared", [False, True])
def test_every_privacy_line_is_short_and_says_what_is_kept(notes, chat_tail, external, shared):
    from tallyhand import privacy

    line = commands.privacy_line(notes, chat_tail, privacy.Report(external_memory=external, shared_sessions=shared))
    assert len(line) < PRIVACY_LIMIT and "\n" not in line
    assert ("!remember" in line) is (notes is not None and notes.enabled and not shared)  # never into a shared session
    assert ("!forgetme" in line) is (notes is not None or not shared)
    assert ("our conversation" in line) is (not shared)
    assert ("Recent chat lines" in line) is (chat_tail > 0)
    assert ("memory service" in line) is external
    assert ("not the memory service's copy" in line) is (external and not shared)
    assert "streamer's" in line and "Hermes history" in line


async def test_privacy_names_an_external_memory_service(chat, twitch):
    chat._privacy = SimpleNamespace(external_memory=True, shared_sessions=False)
    assert await ask(twitch, "!privacy") == commands.privacy_line(SimpleNamespace(enabled=True), 12, chat._privacy)
