"""Questions through Hermes' own GatewayRunner and session authority, wired the way ``hermes gateway`` wires an
adapter: native admission accepts the adapter's events and the turn sees what the adapter set. Only the model call
is stubbed."""

from __future__ import annotations

import asyncio
import logging

import pytest
from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session_authority import initialize_session_authority
from gateway.session_results import execution_result

from harness import until

WAIT = 10.0


@pytest.fixture
async def wired(harness, registered_twitch, monkeypatch, tmp_path):
    """Start a connected adapter behind a real GatewayRunner and session authority (adapter keywords pass through);
    returns the adapter. Fresh Hermes home, no home channel, everyone may ask."""
    registered_twitch()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TWITCH_ALLOW_ALL_USERS", "true")
    started = []

    async def start(per_user: bool = True, **adapter_kwargs):
        runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions", group_sessions_per_user=per_user))
        # What GatewayRunner._instantiate_adapter copies into every adapter's config before building it.
        adapter_kwargs.setdefault("group_sessions_per_user", runner.config.group_sessions_per_user)
        adapter = await harness.connected(record=False, **adapter_kwargs)
        adapter.gateway_runner, runner.adapters = runner, {Platform("twitch"): adapter}
        started.append(await initialize_session_authority(runner, profile_id="default", instance_id="tallyhand"))
        runner._wire_adapter_handlers(adapter)
        return adapter

    yield start
    for authority in started:
        authority.db.close()


@pytest.fixture
async def gateway(wired, monkeypatch):
    """A wired adapter whose turns stop at Hermes' agent handler; returns the adapter and the turns run."""
    adapter = await wired()
    turns = []

    async def turn(event, *_args, **_kwargs):
        turns.append(event)
        answer = f"answer {len(turns)}"
        execution_result.get()["result"] = {"final_response": answer, "messages": []}
        return answer

    monkeypatch.setattr(adapter.gateway_runner, "_handle_message_with_agent", turn)
    yield adapter, turns
    await adapter.disconnect()


def stub_model(adapter, monkeypatch) -> list[str]:
    """Replace only the model call (GatewayRunner._run_agent): Hermes prepares each turn itself, first-contact
    notices included, and delivers the final reply. Returns the messages the model would have seen."""
    messages = []

    async def run_agent(message, context_prompt, history, source, session_id, **_kwargs):
        messages.append(message)
        return {"final_response": f"answer {len(messages)}", "messages": [], "api_calls": 1}

    monkeypatch.setattr(adapter.gateway_runner, "_run_agent", run_agent)
    return messages


@pytest.fixture
async def model_gateway(wired, monkeypatch):
    """A wired adapter with only the model stubbed; returns the adapter and the messages."""
    adapter = await wired()
    yield adapter, stub_model(adapter, monkeypatch)
    await adapter.disconnect()


async def test_viewer_questions_pass_native_admission(gateway, twitch):
    adapter, turns = gateway
    await twitch.privmsg("#chan", "this boss is hard", login="bob", user_id="43", display_name="Bob")
    first = await twitch.privmsg("#chan", "!hermes how do I beat the boss?", login="alice", user_id="42",
                                 display_name="Alice")
    await twitch.wait_for(lambda: twitch.privmsgs(), timeout=WAIT)
    own_id = twitch.sent_ids[-1]
    await until(lambda: adapter._triggers.is_own_message(own_id), what="USERSTATE id")
    second = await twitch.privmsg("#chan", "why that way?", login="carol", user_id="44", display_name="Carol",
                                  reply_to={"msg-id": own_id, "user-id": "1000", "user-login": "tallybot",
                                            "display-name": "TallyBot", "msg-body": "answer 1"})
    await twitch.wait_for(lambda: len(twitch.privmsgs()) == 2, timeout=WAIT)
    assert twitch.privmsgs() == [f"@reply-parent-msg-id={first} PRIVMSG #chan :answer 1",
                                 f"@reply-parent-msg-id={second} PRIVMSG #chan :answer 2"]
    alice, carol = turns
    assert (alice.text, alice.source.user_id, alice.message_id, alice.allow_gateway_control, alice.metadata) == (
        "how do I beat the boss?", "42", first, False, {})
    assert "Bob: this boss is hard" in alice.channel_context
    assert (carol.text, carol.reply_to_message_id, carol.reply_to_text, carol.reply_to_is_own_message) == (
        "why that way?", own_id, "answer 1", True)


async def test_owner_question_passes_native_admission_with_gateway_control(gateway, twitch):
    adapter, turns = gateway
    room = twitch.room_id("#chan")
    asked = await twitch.privmsg("#chan", "!hermes what changed today?", login="chan", user_id=room,
                                 badges="broadcaster/1")
    await twitch.wait_for(lambda: twitch.privmsgs(), timeout=WAIT)
    assert twitch.privmsgs() == [f"@reply-parent-msg-id={asked} PRIVMSG #chan :answer 1"]
    (owner,) = turns
    assert (owner.source.user_id, owner.allow_gateway_control, owner.metadata) == (room, True, {})
    assert adapter.toolsets_for_source(owner.source) is None  # Hermes' default, plugin toolsets included


async def test_commands_answer_beside_admitted_questions(gateway, twitch):
    adapter, turns = gateway
    twitch.badges["#chan"] = "moderator/1"
    await twitch.privmsg("#chan", "!bot", login="alice", user_id="42")
    await twitch.wait_for(lambda: twitch.privmsgs(), timeout=WAIT)
    asked = await twitch.privmsg("#chan", "!hermes and you are?", login="bob", user_id="43")
    await twitch.wait_for(lambda: len(twitch.privmsgs()) == 2, timeout=WAIT)
    bot_line, answer = twitch.privmsgs()
    assert "automated assistant" in bot_line
    assert answer == f"@reply-parent-msg-id={asked} PRIVMSG #chan :answer 1"
    assert [turn.text for turn in turns] == ["and you are?"]  # the command never became a turn


async def test_the_runner_gives_each_role_its_toolsets(gateway, twitch, monkeypatch):
    adapter, turns = gateway
    runner = adapter.gateway_runner
    # As in a gateway that loaded the plugin: "twitch" is a plugin toolset, which Hermes turns on everywhere.
    monkeypatch.setattr("hermes_cli.tools_config._get_plugin_toolset_keys", lambda: {"twitch"})
    room = twitch.room_id("#chan")
    await twitch.privmsg("#chan", "!hermes mod question", login="mo", user_id="47", mod=True, badges="moderator/1")
    await twitch.privmsg("#chan", "!hermes owner question", login="chan", user_id=room, badges="broadcaster/1")
    await twitch.privmsg("#chan", "!hermes viewer question", login="alice", user_id="42")
    await until(lambda: len(turns) == 3, timeout=WAIT, what="three turns")
    resolved = {turn.text: runner._resolve_enabled_toolsets_for_source({}, turn.source, "twitch") for turn in turns}
    assert resolved["viewer question"] == ["todo", "twitch"]  # the sandbox hook refuses these for viewers
    assert resolved["mod question"] == ["todo", "twitch"]
    assert "twitch" in resolved["owner question"] and "terminal" in resolved["owner question"]
    saved_off = {"known_plugin_toolsets": {"twitch": ["twitch"]}}
    viewer = next(turn for turn in turns if turn.text == "viewer question")
    assert runner._resolve_enabled_toolsets_for_source(saved_off, viewer.source, "twitch") == ["todo"]


# Hermes' one-time prompt for a chat without a home channel: locales/en.yaml gateway.notify.no_home_channel (line
# 653 in 0.22.7, 0.23.0 and main), sent by gateway/run_turn_prepare.py _hmwa_first_contact_notes through
# gateway/run_notifications.py _deliver_platform_notice.
NO_HOME_CHANNEL = ("\U0001f4ec No home channel is set for Twitch. A home channel is where Hermes delivers cron job "
                   "results and cross-platform messages.\n\nType /sethome to make this chat your home channel, or "
                   "ignore to skip.")


def test_the_notice_text_is_hermes_own():
    from agent.i18n import t

    assert t("gateway.notify.no_home_channel", platform="Twitch", sethome_cmd="/sethome") == NO_HOME_CHANNEL


async def test_the_first_question_in_a_fresh_channel_gets_only_the_answer(model_gateway, twitch, caplog):
    adapter, messages = model_gateway
    with caplog.at_level(logging.INFO, logger="tallyhand.adapter"):
        asked = await twitch.privmsg("#chan", "!hermes first question here", login="alice", user_id="42")
        await twitch.wait_for(lambda: twitch.privmsgs(), timeout=WAIT)
        await asyncio.sleep(0.5)
        assert twitch.privmsgs() == [f"@reply-parent-msg-id={asked} PRIVMSG #chan :answer 1"]
        assert len(messages) == 1 and messages[0].endswith("first question here")
        assert "Reply in the language of this question" in messages[0]
        await until(lambda: "No home channel is set for Twitch" in caplog.text, timeout=WAIT, what="the log line")
    assert adapter.config.extra["notice_delivery"] == "private"
    assert "/sethome" not in "".join(twitch.privmsgs())


async def test_hermes_notices_go_to_the_log_not_to_chat(model_gateway, twitch, caplog):
    adapter, _ = model_gateway
    source = adapter.build_source(chat_id="#chan", chat_name="#chan", chat_type="group", user_id="42",
                                  user_name="Alice")
    with caplog.at_level(logging.INFO, logger="tallyhand.adapter"):
        await adapter.gateway_runner._deliver_platform_notice(source, NO_HOME_CHANNEL)
    await asyncio.sleep(0.2)
    assert twitch.privmsgs() == []
    assert "Hermes notice kept out of #chan: \U0001f4ec No home channel is set for Twitch." in caplog.text


def test_notice_delivery_is_private_whatever_the_config_says(caplog):
    from gateway.config import PlatformConfig

    from tallyhand.adapter import TwitchAdapter

    with caplog.at_level(logging.WARNING):
        adapter = TwitchAdapter(PlatformConfig(enabled=True, extra={"channel": "#chan", "notice_delivery": "public"}))
    assert adapter.config.extra["notice_delivery"] == "private"
    assert "ignoring notice_delivery='public'" in caplog.text


async def test_the_final_answer_reaches_the_obs_caption(wired, twitch, monkeypatch):
    from tallyhand import obs, obs_tools

    mirrored = []

    async def mirror(text):
        mirrored.append(text)
        return True

    monkeypatch.setattr(obs_tools, "mirror", mirror)
    monkeypatch.setenv(obs.PASSWORD_ENV, "set")
    adapter = await wired(settings={"caption_answers": True})
    stub_model(adapter, monkeypatch)
    await twitch.privmsg("#chan", "!hermes what game is this?", login="alice", user_id="42")
    await twitch.wait_for(lambda: twitch.privmsgs(), timeout=WAIT)
    await until(lambda: mirrored, timeout=WAIT, what="the caption")
    assert mirrored == ["answer 1"]
    await adapter.disconnect()


# approvals: Tallyhand's hook asks, Hermes' gate posts the question, the owner answers in chat

BANNED = iter(range(1, 10_000))  # a new login per test: Hermes remembers "session" answers per session key
REASON = {"text": "spam"}


class BanAgent:
    """Stands in for the model at the moment it calls twitch_ban: Tallyhand's own pre_tool_call hook judges the call,
    and Hermes' plugin approval gate (hermes_cli/plugins.py _resolve_block_from_details) asks in chat and waits."""

    def __init__(self, login: str, outcomes: list):
        self.login, self.outcomes = login, outcomes

    def run_conversation(self, message, **_kwargs):
        from hermes_cli.plugins import _PreToolCallDirective, _resolve_block_from_details

        from tallyhand import sandbox

        directive = sandbox.pre_tool_call(tool_name="twitch_ban", args={"login": self.login, "reason": REASON["text"]})
        block = _resolve_block_from_details(_PreToolCallDirective(**directive), "twitch_ban")
        self.outcomes.append(block)
        return {"final_response": "banned" if block is None else "not banned", "messages": []}


@pytest.fixture
def ban_turn(monkeypatch):
    """Every turn runs BanAgent inside Hermes' real approval plumbing (TurnRunner._run_conversation_with_approval)."""
    from gateway.run_turn_runner import TurnRunner

    login, outcomes = f"spambot{next(BANNED)}", []

    def scoped(self):
        result = self._run_conversation_with_approval(BanAgent(login, outcomes), [], None, None, None)
        return {"final_response": result["final_response"], "messages": [], "api_calls": 1, "tools": []}

    monkeypatch.setattr(TurnRunner, "_run_sync_scoped", scoped)
    return login, outcomes


async def ask_for_a_ban(wired, twitch, **adapter_kwargs):
    adapter = await wired(**adapter_kwargs)
    room = twitch.room_id("#chan")
    await twitch.privmsg("#chan", "!hermes ban the spam bot", login="chan", user_id=room, badges="broadcaster/1")
    await twitch.wait_for(lambda: any("needs your OK" in line for line in twitch.texts()), timeout=WAIT)
    return adapter, room


async def settle(twitch, room, outcomes):
    """Deny a question still waiting, so no approval outlives its test (Hermes keys them by session)."""
    if not outcomes:
        await twitch.privmsg("#chan", "!hermes deny", login="chan", user_id=room, badges="broadcaster/1")
        await until(lambda: outcomes, timeout=WAIT, what="the denial")


async def test_the_approval_question_tells_the_owner_to_type_hermes_approve(wired, twitch, ban_turn):
    login, outcomes = ban_turn
    adapter, room = await ask_for_a_ban(wired, twitch)
    (question,) = [line for line in twitch.texts() if "needs your OK" in line]
    assert f"Ban {login} from #chan (reason: spam)?" in question
    assert "Reply !hermes approve to run it once" in question and "or !hermes deny to cancel" in question
    assert "/approve" not in question and "/deny" not in question
    assert outcomes == []  # still waiting for the owner
    await settle(twitch, room, outcomes)
    await adapter.disconnect()


@pytest.mark.parametrize(("answer", "ran"), [
    ("!hermes approve", True), ("!hermes approve session", True), ("!hermes yes", True), ("@tallybot approve", True),
    ("!hermes deny", False), ("!hermes no", False)])
async def test_the_owners_answer_passes_admission_and_busy_routing(wired, twitch, ban_turn, answer, ran):
    login, outcomes = ban_turn
    adapter, room = await ask_for_a_ban(wired, twitch)
    await twitch.privmsg("#chan", answer, login="chan", user_id=room, badges="broadcaster/1")
    await until(lambda: outcomes, timeout=WAIT, what="the approval outcome")
    final = "banned" if ran else "not banned"
    await twitch.wait_for(lambda: final in twitch.texts(), timeout=WAIT)
    assert (outcomes[0] is None) is ran
    if not ran:
        assert outcomes[0].startswith("BLOCKED: Action denied by user.")
    word = "Command approved" if ran else "Command denied"  # Hermes' confirmation, which may come after the answer
    await twitch.wait_for(lambda: any(word in line for line in twitch.texts()), timeout=WAIT)
    await adapter.disconnect()


async def test_a_viewer_cannot_answer_for_the_owner(wired, twitch, ban_turn):
    login, outcomes = ban_turn
    adapter, room = await ask_for_a_ban(wired, twitch)
    await twitch.privmsg("#chan", "!hermes approve", login="alice", user_id="42")
    await asyncio.sleep(1.5)
    assert outcomes == []  # the viewer's "approve" went to the viewer's own session
    await twitch.privmsg("#chan", "!hermes deny", login="chan", user_id=room, badges="broadcaster/1")
    await until(lambda: outcomes, timeout=WAIT, what="the owner's answer")
    assert outcomes[0].startswith("BLOCKED: Action denied by user.")
    await adapter.disconnect()


async def test_the_question_keeps_its_answer_words_in_slow_mode(wired, twitch, ban_turn, monkeypatch):
    monkeypatch.setitem(REASON, "text", "posted the same scam link in every channel it could reach " * 3)
    twitch.slow["#chan"] = 2  # the bot is no moderator here: an answer would be one line in slow mode
    login, outcomes = ban_turn
    adapter, room = await ask_for_a_ban(wired, twitch)
    await twitch.wait_for(lambda: any(line.endswith("NOT run.") for line in twitch.texts()), timeout=WAIT)
    first, second = twitch.texts()[:2]  # the second line waits out the slow-mode interval, then follows
    assert "needs your OK" in first and "!hermes deny to cancel" in first + second and second.endswith("NOT run.")
    await settle(twitch, room, outcomes)
    await adapter.disconnect()


async def test_neither_the_question_nor_the_confirmation_is_captioned(wired, twitch, ban_turn, monkeypatch):
    from tallyhand import obs, obs_tools

    mirrored = []

    async def mirror(text):
        mirrored.append(text)
        return True

    monkeypatch.setattr(obs_tools, "mirror", mirror)
    monkeypatch.setenv(obs.PASSWORD_ENV, "set")
    login, outcomes = ban_turn
    adapter, room = await ask_for_a_ban(wired, twitch, settings={"caption_answers": True})
    await twitch.privmsg("#chan", "!hermes approve", login="chan", user_id=room, badges="broadcaster/1")
    await twitch.wait_for(lambda: "banned" in twitch.texts(), timeout=WAIT)
    await twitch.wait_for(lambda: any("Command approved" in line for line in twitch.texts()), timeout=WAIT)
    await asyncio.sleep(0.5)
    assert mirrored == ["banned"]
    await adapter.disconnect()


# TWITCH_ALLOWED_USERS with logins: resolved at connect, matched by Hermes


@pytest.mark.parametrize(("raw", "logins"), [
    ("alice, 12345 ,@Bob,*, ,not a login!,ALICE", ["alice", "bob"]),
    ('["carol", "42", "@dave"]', ["carol", "dave"]),
    ("", []), ("12345", []), ("@12345", []), ("*", [])])
def test_allowlist_logins_read_like_hermes(raw, logins):
    from tallyhand.adapter import allowlist_logins

    assert allowlist_logins(raw) == logins


@pytest.fixture
def listed(helix_api, monkeypatch):
    """Twitch knows alice (42) and mo (47); TWITCH_ALLOWED_USERS names alice by login and 99 by id."""
    helix_api.add_user("alice", "42", "Alice")
    helix_api.add_user("mo", "47", "Mo")
    monkeypatch.setenv("TWITCH_ALLOWED_USERS", "@Alice, 99, ghostviewer")
    return helix_api


async def test_logins_resolve_at_connect(harness, twitch, listed, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="tallyhand.adapter"):
        adapter = await harness.connected(_test_helix_url=listed.url)
    assert adapter.resolved_allowlist_user_ids() == {"42"}
    assert caplog.text.count("names ghostviewer, but Twitch has no account") == 1
    assert [call.query["login"] for call in listed.calls("users")] == [["alice", "ghostviewer"]]
    monkeypatch.setenv("TWITCH_ALLOWED_USERS", "99")  # taken off the list: no restart needed
    assert adapter.resolved_allowlist_user_ids() == set()


async def test_an_unreachable_twitch_leaves_logins_out_until_it_answers(harness, twitch, listed, monkeypatch,
                                                                         caplog):
    from tallyhand.adapter import TwitchAdapter

    monkeypatch.setattr(TwitchAdapter, "revalidate_tick", 0.05)
    listed.force(503, times=1000)
    with caplog.at_level(logging.WARNING, logger="tallyhand.adapter"):
        adapter = await harness.connected(_test_helix_url=listed.url)
        assert adapter.is_connected and adapter.resolved_allowlist_user_ids() == set()
        await until(lambda: len(listed.calls("users")) >= 3, what="lookups tried again")
        listed.forced.clear()  # Twitch answers again
        await until(lambda: adapter.resolved_allowlist_user_ids() == {"42"}, what="the login resolved")
    assert caplog.text.count("could not look up the logins in TWITCH_ALLOWED_USERS") == 1  # not once a minute
    await adapter.disconnect()


async def test_hermes_admits_a_viewer_listed_by_login(wired, twitch, listed, monkeypatch):
    monkeypatch.delenv("TWITCH_ALLOW_ALL_USERS")
    adapter = await wired(_test_helix_url=listed.url)
    messages = stub_model(adapter, monkeypatch)
    await twitch.privmsg("#chan", "!hermes who is refused?", login="mo", user_id="47", display_name="Mo")
    asked = await twitch.privmsg("#chan", "!hermes am I allowed?", login="alice", user_id="42", display_name="Alice")
    await twitch.wait_for(lambda: twitch.privmsgs(), timeout=WAIT)
    await asyncio.sleep(0.5)
    assert twitch.privmsgs() == [f"@reply-parent-msg-id={asked} PRIVMSG #chan :answer 1"]
    assert len(messages) == 1 and messages[0].endswith("am I allowed?")  # mo's question never became a turn
    await adapter.disconnect()


async def test_a_refused_viewer_costs_no_slot_and_no_cooldown(harness, twitch, listed):
    adapter = await harness.connected(_test_helix_url=listed.url)
    adapter.set_authorization_check(lambda user_id, chat_type=None, chat_id=None, **kwargs: user_id == "42")
    await twitch.privmsg("#chan", "!hermes let me in", login="mo", user_id="47")
    await twitch.privmsg("#chan", "!hermes hello", login="alice", user_id="42")
    await until(lambda: adapter.recorder.events, what="alice's question")
    assert [event.text for event in adapter.recorder.events] == ["hello"]
    assert adapter._cooldowns.ready(("#chan", "47"))  # mo was never put on a cooldown


# !forgetme and Hermes' own history


def twitch_sessions() -> dict[str, str]:
    """session_key -> session id of every Twitch session in the profile's state.db, through the public SessionDB."""
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        return {row["session_key"]: row["id"] for row in db.list_gateway_sessions(platform="twitch", active_only=False)}
    finally:
        db.close()


async def test_forgetme_deletes_the_viewers_own_sessions_only(wired, twitch, monkeypatch):
    from tallyhand.viewer_notes import FORGET_TRACES

    adapter = await wired(settings={"user_cooldown": 0})
    stub_model(adapter, monkeypatch)
    twitch.badges["#chan"] = "moderator/1"
    for login, user_id in (("alice", "42"), ("bob", "43")):
        await twitch.privmsg("#chan", f"!hermes hi from {login}", login=login, user_id=user_id)
    await twitch.wait_for(lambda: len(twitch.privmsgs()) == 2, timeout=WAIT)
    await until(lambda: len(twitch_sessions()) == 2, timeout=WAIT, what="two sessions")
    adapter._notes.add(twitch.room_id("#chan"), "42", "I play on PC")
    await twitch.privmsg("#chan", "!forgetme", login="alice", user_id="42", display_name="Alice")
    await twitch.wait_for(lambda: len(twitch.privmsgs()) == 3, timeout=WAIT)
    assert twitch.texts()[-1] == (
        f"Done, Alice: I deleted your 1 note and our conversation in this channel. {FORGET_TRACES}")
    keys = twitch_sessions()
    assert [key.rsplit(":", 1)[1] for key in keys] == ["43"]  # bob's session stays
    await twitch.privmsg("#chan", "!hermes back again", login="alice", user_id="42")
    await until(lambda: len(twitch_sessions()) == 2, timeout=WAIT, what="a new session for alice")
    await adapter.disconnect()


async def test_forgetme_leaves_a_shared_channel_session_alone(wired, twitch, monkeypatch, caplog):
    """group_sessions_per_user off in the gateway's own config, which is what Hermes keys sessions by; the bot
    profile's config.yaml says nothing about it."""
    with caplog.at_level(logging.WARNING, logger="tallyhand.privacy"):
        adapter = await wired(per_user=False)
    messages = stub_model(adapter, monkeypatch)
    assert adapter._privacy.shared_sessions and "group_sessions_per_user is false" in caplog.text
    twitch.badges["#chan"] = "moderator/1"
    adapter._notes.add(twitch.room_id("#chan"), "42", "I play on PC")  # stored before the layout changed
    await twitch.privmsg("#chan", "!hermes hi", login="alice", user_id="42")
    await twitch.wait_for(lambda: twitch.privmsgs(), timeout=WAIT)
    assert "I play on PC" not in messages[0]  # a shared session would show the note to every viewer
    await until(lambda: twitch_sessions(), timeout=WAIT, what="the channel session")
    assert list(twitch_sessions()) == ["agent:main:twitch:group:#chan"]
    await twitch.privmsg("#chan", "!remember I use a controller", login="alice", user_id="42")
    await twitch.privmsg("#chan", "!forgetme", login="alice", user_id="42", display_name="Alice")
    await twitch.wait_for(lambda: len(twitch.privmsgs()) == 2, timeout=WAIT)
    from tallyhand.viewer_notes import FORGET_SHARED

    assert twitch.texts()[-1] == f"Done, Alice: I deleted your 1 note in this channel. {FORGET_SHARED}"
    assert len(twitch_sessions()) == 1  # everyone's words are in it: !forgetme cannot pick one viewer's out
    await adapter.disconnect()


async def test_the_bot_posts_only_where_it_serves(harness, twitch, monkeypatch):
    adapter = await harness.connected(channels="#chan")
    refused = await adapter.send("#elsewhere", "hello")
    assert not refused.success and "not a channel this bot serves" in refused.error
    monkeypatch.setenv("TWITCH_HOME_CHANNEL", "elsewhere")
    assert (await adapter.send("#elsewhere", "now it is the home channel")).success
    await twitch.wait_for(lambda: twitch.privmsgs())
    assert twitch.privmsgs() == ["PRIVMSG #elsewhere :now it is the home channel"]
