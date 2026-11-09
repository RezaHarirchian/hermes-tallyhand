"""The ``twitch`` toolset: registration, what each caller class may run, and what reaches Helix."""

from __future__ import annotations

import json
import logging

import pytest

from harness import MOD_ID, TOKEN, VIEWER_ID, RecordingContext, session, until
from tallyhand import roles, twitch_tools
from tallyhand.helix import Helix
from tallyhand.twitch_tools import HANDLERS, TOOL_NAMES

MSG = "b34ccfc7-4977-403a-8a94-33c6bac34fb8"
TARGET_ID = "77"


async def call(tool: str, args: dict | None = None, **who) -> dict:
    with session(**who):
        return json.loads(await HANDLERS[tool](dict(args or {})))


@pytest.fixture
async def chat(harness, twitch, helix_api):
    """A connected adapter (the gateway process) with Helix on the fake. #chan's broadcaster is the owner; mo is a
    moderator there, as seen in chat; spammer exists on Twitch."""
    twitch.badges["#chan"] = "moderator/1"
    adapter = await harness.connected(_test_helix_url=helix_api.url)
    room = twitch.room_id("#chan")
    helix_api.add_user("chan", room, "Chan")
    helix_api.add_user("spammer", TARGET_ID, "Spammer")
    helix_api.add_user("friend", "3000", "Friend")
    helix_api.messages.add(MSG)
    await twitch.privmsg("#chan", "hi all", login="mo", user_id=MOD_ID, mod=True, badges="moderator/1")
    await twitch.privmsg("#chan", "hello", login="alice", user_id=VIEWER_ID)
    await until(lambda: adapter.roles.role_of(room, MOD_ID) and adapter.roles.role_of(room, VIEWER_ID), what="roles")
    adapter.room = room
    return adapter


def owner(chat) -> dict:
    return {"platform": "twitch", "chat_id": "#chan", "user_id": chat.room}


MODERATOR = {"platform": "twitch", "chat_id": "#chan", "user_id": MOD_ID}
VIEWER = {"platform": "twitch", "chat_id": "#chan", "user_id": VIEWER_ID}
LOCAL = {"source": "cli"}
TELEGRAM = {"platform": "telegram", "chat_id": "123", "user_id": "9"}

ARGS = {
    "twitch_stream_info": {},
    "twitch_create_clip": {},
    "twitch_announce": {"message": "Giveaway in five minutes", "color": "green"},
    "twitch_shoutout": {"login": "friend"},
    "twitch_timeout": {"login": "spammer", "seconds": 300, "reason": "links"},
    "twitch_ban": {"login": "spammer", "reason": "bot"},
    "twitch_unban": {"login": "spammer"},
    "twitch_delete_message": {"message_id": MSG},
}
EFFECTS = {
    "twitch_stream_info": ("streams", "GET"),
    "twitch_create_clip": ("clips", "POST"),
    "twitch_announce": ("chat/announcements", "POST"),
    "twitch_shoutout": ("chat/shoutouts", "POST"),
    "twitch_timeout": ("moderation/bans", "POST"),
    "twitch_ban": ("moderation/bans", "POST"),
    "twitch_unban": ("moderation/bans", "DELETE"),
    "twitch_delete_message": ("moderation/chat", "DELETE"),
}


def prepare(api, chat, tool: str) -> None:
    api.go_live(chat.room)
    if tool == "twitch_unban":
        api.bans[(chat.room, TARGET_ID)] = None


# registration


def test_register_tools_declares_eight_tools_in_one_toolset():
    ctx = RecordingContext()
    assert twitch_tools.register_tools(ctx) == list(TOOL_NAMES)
    assert [tool["name"] for tool in ctx.tools] == list(TOOL_NAMES)
    for tool in ctx.tools:
        assert (tool["toolset"], tool["is_async"], tool["check_fn"]) == ("twitch", True, twitch_tools.token_configured)
        schema = tool["schema"]
        assert schema["name"] == tool["name"] and schema["description"] == tool["description"]
        assert schema["parameters"]["type"] == "object" and "channel" in schema["parameters"]["properties"]
        assert tool["handler"] is HANDLERS[tool["name"]]
    required = {tool["name"]: tool["schema"]["parameters"].get("required", []) for tool in ctx.tools}
    assert required == {"twitch_stream_info": [], "twitch_create_clip": [], "twitch_announce": ["message"],
                        "twitch_shoutout": ["login"], "twitch_timeout": ["login"], "twitch_ban": ["login"],
                        "twitch_unban": ["login"], "twitch_delete_message": ["message_id"]}


async def test_check_fn_needs_a_token_or_a_running_adapter(monkeypatch, chat):
    assert twitch_tools.token_configured() is True  # the adapter runs in this process
    await chat.disconnect()
    assert roles.live() is None
    assert twitch_tools.token_configured() is False
    monkeypatch.setenv("TWITCH_BOT_TOKEN", "oauth:")
    assert twitch_tools.token_configured() is False
    monkeypatch.setenv("TWITCH_BOT_TOKEN", f"oauth:{TOKEN}")
    assert twitch_tools.token_configured() is True


# who may run what


@pytest.mark.parametrize("tool", TOOL_NAMES)
async def test_owner_runs_every_tool(chat, helix_api, tool):
    prepare(helix_api, chat, tool)
    result = await call(tool, ARGS[tool], **owner(chat))
    assert result.get("ok") is True, result
    assert result["channel"] == "chan"
    assert helix_api.calls(*EFFECTS[tool])


@pytest.mark.parametrize("tool", TOOL_NAMES)
async def test_moderator_runs_the_moderator_tools(chat, helix_api, tool):
    prepare(helix_api, chat, tool)
    result = await call(tool, ARGS[tool], **MODERATOR)
    if tool in twitch_tools.MODERATOR_TOOLS:
        assert result.get("ok") is True, result
        assert helix_api.calls(*EFFECTS[tool])
    else:
        assert result["code"] == "not_allowed" and "only the streamer may use" in result["error"]
        assert helix_api.calls(*EFFECTS[tool]) == []


async def test_moderator_timeouts_stop_at_600_seconds(chat, helix_api):
    assert (await call("twitch_timeout", {"login": "spammer", "seconds": 600}, **MODERATOR))["ok"]
    refused = await call("twitch_timeout", {"login": "spammer", "seconds": 601}, **MODERATOR)
    assert refused["code"] == "not_allowed" and "600 seconds" in refused["error"]
    assert (await call("twitch_timeout", {"login": "spammer"}, **MODERATOR))["seconds"] == 600
    assert [seen.body["data"]["duration"] for seen in helix_api.calls("moderation/bans", "POST")] == [600, 600]


async def test_moderator_only_in_channels_they_moderate(chat, helix_api, twitch):
    await twitch.privmsg("#chan", "hi", login="vip", user_id="48", vip=True, badges="vip/1")
    await until(lambda: chat.roles.role_of(chat.room, "48"), what="vip role")
    result = await call("twitch_timeout", {"login": "spammer", "channel": "friend"}, **MODERATOR)
    assert result["code"] == "not_allowed" and "moderator of friend" in result["error"]
    vip = await call("twitch_timeout", {"login": "spammer"}, platform="twitch", chat_id="#chan", user_id="48")
    assert vip["code"] == "not_allowed"
    assert helix_api.calls("moderation/bans") == []


@pytest.mark.parametrize("tool", TOOL_NAMES)
async def test_viewer_gets_no_twitch_tool(chat, helix_api, tool):  # the sandbox hook says no first, the tool too
    prepare(helix_api, chat, tool)
    result = await call(tool, ARGS[tool], **VIEWER)
    assert result["code"] == "not_allowed"
    assert helix_api.calls(*EFFECTS[tool]) == []


@pytest.mark.parametrize("who", [None, "telegram"])
async def test_acting_stays_in_the_channels_the_bot_serves(chat, helix_api, monkeypatch, who):
    """A steered owner turn cannot ban or announce in a channel the bot never joined; reading stream info is fine."""
    monkeypatch.setattr(twitch_tools, "_get_config", {"tools_from_other_platforms": True}.get)
    caller = TELEGRAM if who else owner(chat)
    helix_api.go_live("3000")
    for tool in ("twitch_ban", "twitch_announce", "twitch_create_clip", "twitch_delete_message"):
        result = await call(tool, {**ARGS[tool], "channel": "friend"}, **caller)
        assert result["code"] == "not_allowed" and "not a channel this bot serves" in result["error"]
    assert helix_api.calls("moderation/bans") == [] and helix_api.calls("chat/announcements") == []
    assert (await call("twitch_stream_info", {"channel": "friend"}, **caller))["ok"]
    assert (await call("twitch_ban", {"login": "spammer", "channel": "friend"}, **LOCAL))["ok"]  # the operator may
    monkeypatch.setenv("TWITCH_CHANNEL", "chan,friend")  # listed in TWITCH_CHANNEL: served
    assert (await call("twitch_announce", {**ARGS["twitch_announce"], "channel": "friend"}, **caller))["ok"]


async def test_an_unrecognised_other_platforms_value_is_logged_once(monkeypatch, caplog):
    monkeypatch.setattr(twitch_tools, "_warned", set())
    monkeypatch.setattr(twitch_tools, "_get_config", {"tools_from_other_platforms": "sometimes"}.get)
    assert not twitch_tools.other_platforms_allowed() and not twitch_tools.other_platforms_allowed()
    assert caplog.text.count("ignoring tools_from_other_platforms='sometimes'") == 1


@pytest.mark.parametrize("tool", TOOL_NAMES)
async def test_local_operator_runs_every_tool(chat, helix_api, tool):
    prepare(helix_api, chat, tool)
    assert (await call(tool, ARGS[tool], **LOCAL)).get("ok") is True


@pytest.mark.parametrize("surface", ["cli", "tui", "desktop", "local"])
async def test_local_surfaces(chat, helix_api, surface):
    assert (await call("twitch_ban", {"login": "spammer"}, source=surface))["ok"]
    assert (await call("twitch_unban", {"login": "spammer"}, platform=surface))["ok"]


@pytest.mark.parametrize("tool", TOOL_NAMES)
async def test_other_platforms_are_refused_by_default(chat, helix_api, tool):
    prepare(helix_api, chat, tool)
    result = await call(tool, ARGS[tool], **TELEGRAM)
    assert result["code"] == "not_allowed" and "tools_from_other_platforms" in result["error"]
    assert helix_api.calls(*EFFECTS[tool]) == []


async def test_other_platforms_with_the_setting(chat, helix_api, monkeypatch):
    monkeypatch.setattr(twitch_tools, "_get_config", {"tools_from_other_platforms": True}.get)
    assert (await call("twitch_ban", {"login": "spammer"}, **TELEGRAM))["ok"]
    monkeypatch.setattr(twitch_tools, "_get_config", {"tools_from_other_platforms": "false"}.get)
    assert (await call("twitch_unban", {"login": "spammer"}, **TELEGRAM))["code"] == "not_allowed"


async def test_a_turn_without_platform_or_source_is_the_cli_operator(chat, helix_api):
    assert (await call("twitch_stream_info"))["ok"]


async def test_cron_counts_as_another_platform(chat, helix_api, monkeypatch):
    refused = await call("twitch_announce", ARGS["twitch_announce"], cron_session="1")
    assert refused["code"] == "not_allowed" and "cron" in refused["error"]
    monkeypatch.setattr(twitch_tools, "_get_config", {"tools_from_other_platforms": True}.get)
    assert (await call("twitch_announce", ARGS["twitch_announce"], cron_session="1"))["ok"]


async def test_roles_come_from_the_adapter_not_the_session(chat, helix_api):
    # A Twitch turn's session carries only ids; nothing in it can claim a role.
    forged = {"platform": "twitch", "chat_id": "#chan", "user_id": VIEWER_ID, "user_name": "chan"}
    assert (await call("twitch_ban", {"login": "spammer"}, **forged))["code"] == "not_allowed"
    await chat.disconnect()  # no adapter, no roles: a Twitch asker is nobody
    assert (await call("twitch_timeout", {"login": "spammer", "channel": "chan"}, **MODERATOR))["code"] == "not_allowed"


# the local operator without a gateway


async def test_cli_without_the_adapter_uses_the_env_token(helix_api, validate, monkeypatch):
    helix_api.add_user("chan", "2000", "Chan")
    helix_api.add_user("spammer", TARGET_ID, "Spammer")
    monkeypatch.setenv("TWITCH_BOT_TOKEN", f"oauth:{TOKEN}")
    monkeypatch.setenv("TWITCH_CHANNEL", "Chan,other")
    monkeypatch.setattr(twitch_tools, "_local_client", None)
    built = twitch_tools.client()
    assert built.base_url == "https://api.twitch.tv/helix"
    built.base_url, built.validate_url = helix_api.url, validate.url
    assert roles.live() is None and twitch_tools.default_channel() == "chan"
    result = await call("twitch_timeout", {"login": "@Spammer", "seconds": 1800}, source="cli")
    assert (result["ok"], result["channel"], result["seconds"]) == (True, "chan", 1800)
    assert helix_api.bans == {("2000", TARGET_ID): 1800}
    assert helix_api.calls("users")[0].authorization == f"Bearer {TOKEN}"


async def test_cli_without_a_channel(helix_api, monkeypatch):
    monkeypatch.setattr(twitch_tools, "_local_client", Helix(TOKEN, base_url=helix_api.url))
    result = await call("twitch_stream_info", source="cli")
    assert result == {"error": "no channel given and TWITCH_CHANNEL is not set", "code": "bad_arguments"}


# arguments and errors


@pytest.mark.parametrize(("tool", "args", "code"), [
    ("twitch_timeout", {"login": "spammer", "seconds": 0}, "bad_arguments"),
    ("twitch_timeout", {"login": "spammer", "seconds": 1_209_601}, "bad_arguments"),
    ("twitch_timeout", {"login": "spammer", "seconds": "ten"}, "bad_arguments"),
    ("twitch_timeout", {"login": "spammer", "seconds": True}, "bad_arguments"),
    ("twitch_timeout", {"login": "not a login"}, "bad_arguments"),
    ("twitch_ban", {}, "bad_arguments"),
    ("twitch_ban", {"login": "nobody_here"}, "bad_arguments"),
    ("twitch_announce", {"message": "  "}, "bad_arguments"),
    ("twitch_announce", {"message": "hi", "color": "neon"}, "bad_arguments"),
    ("twitch_delete_message", {"message_id": "12345"}, "bad_arguments"),
    ("twitch_stream_info", {"channel": "#not a channel"}, "bad_arguments"),
    ("twitch_stream_info", {"channel": "ghost_channel"}, "bad_arguments"),
    ("twitch_create_clip", {}, "not_found"),
    ("twitch_delete_message", {"message_id": "c34ccfc7-4977-403a-8a94-33c6bac34fb8"}, "not_found"),
])
async def test_bad_arguments_and_twitch_refusals(chat, helix_api, tool, args, code):
    result = await call(tool, args, **owner(chat))
    assert result["code"] == code and result["error"], result


async def test_stream_info_offline_and_live(chat, helix_api):
    assert await call("twitch_stream_info", **MODERATOR) == {"ok": True, "channel": "chan", "live": False}
    helix_api.go_live("3000", title="Ranked", game="Chess", viewers=7)
    info = await call("twitch_stream_info", {"channel": "friend"}, **owner(chat))
    assert (info["live"], info["title"], info["game"], info["viewers"], info["channel"]) == (
        True, "Ranked", "Chess", 7, "friend")
    assert info["uptime"] and info["uptime_seconds"] > 0


async def test_helix_errors_become_tool_errors(chat, helix_api, validate):
    helix_api.moderated = set()
    result = await call("twitch_timeout", {"login": "spammer"}, **owner(chat))
    assert result["code"] == "forbidden" and "must be a moderator of the channel" in result["error"]
    validate.respond(status=401)
    helix_api.force(401)
    result = await call("twitch_ban", {"login": "spammer"}, **owner(chat))
    assert result["code"] == "token_invalid"


async def test_unexpected_failures_never_raise(chat, monkeypatch, caplog):
    async def explode(*args):
        raise RuntimeError(f"boom {TOKEN}")

    handler = twitch_tools._handler("twitch_ban", explode)
    with session(**owner(chat)):
        result = json.loads(await handler({"login": "spammer"}))
    assert result == {"error": "twitch_ban failed (RuntimeError)", "code": "failed"}
    assert TOKEN not in caplog.text
    assert json.loads(await HANDLERS["twitch_stream_info"]("not a dict"))["ok"] is True  # read as no arguments


async def test_nothing_secret_in_results_or_logs(chat, helix_api, caplog):
    caplog.set_level(logging.DEBUG)
    results = []
    for tool in TOOL_NAMES:
        prepare(helix_api, chat, tool)
        for who in (owner(chat), MODERATOR, VIEWER, LOCAL, TELEGRAM):
            results.append(await call(tool, {**ARGS[tool], "reason": f"has {TOKEN}"}, **who))
    text = json.dumps(results) + caplog.text
    assert TOKEN not in text and "fakeclientid" not in text
    for seen in helix_api.requests:
        assert TOKEN not in seen.url and TOKEN not in json.dumps(seen.body)
        assert "fakeclientid" not in seen.url and "fakeclientid" not in json.dumps(seen.body)
