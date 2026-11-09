"""OBS tools: registration and schemas, every tool's success and failure paths against FakeObs, the caption cut
and its clear timer, and dispatch through Hermes' own tool registry."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any

import pytest

from fake_obs import FakeObs
from harness import MOD_ID, VIEWER_ID, RecordingContext, closed_port, session, twitch_turn, until
from tallyhand import obs, obs_tools, roles, twitch_tools
from tallyhand.obs_tools import OBS_TOOL_NAMES, OBS_TOOLSET, caption_text, register_obs_tools

PASSWORD = "c0rrect-h0rse"
NAMES = ("obs_status", "obs_list_scenes", "obs_switch_scene", "obs_list_audio_inputs", "obs_set_mute",
         "obs_show_caption", "obs_clear_caption", "obs_set_source_visible", "obs_save_replay")


def handlers() -> dict:
    ctx = RecordingContext()
    register_obs_tools(ctx)
    return {tool["name"]: tool["handler"] for tool in ctx.tools}


HANDLERS = handlers()


async def run(name: str, args: Any = None, **context: Any) -> dict:
    result = await HANDLERS[name]({} if args is None else args, **context)
    assert isinstance(result, str)
    return json.loads(result)


class ManualTimer:
    """threading.Timer stand-in: started and cancelled on request, fired by the test on a worker thread."""

    def __init__(self, interval, function, args=(), kwargs=None):
        self.interval, self.function, self.args = interval, function, tuple(args)
        self.daemon = self.started = self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    async def fire(self) -> None:
        await asyncio.to_thread(self.function, *self.args)


@pytest.fixture
async def fake(monkeypatch):
    server = await FakeObs().start()
    monkeypatch.setenv(obs.URL_ENV, server.url)
    yield server
    await server.stop()


@pytest.fixture(autouse=True)
def timers(monkeypatch) -> list[ManualTimer]:
    made: list[ManualTimer] = []

    def factory(*args, **kwargs) -> ManualTimer:
        made.append(ManualTimer(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(obs_tools, "CAPTION", obs_tools.CaptionTimer(factory))
    return made


@pytest.fixture
def obs_down(monkeypatch) -> int:
    port = closed_port()
    monkeypatch.setenv(obs.URL_ENV, f"ws://127.0.0.1:{port}")
    return port


def assert_down(result: dict, port: int) -> None:
    assert result == {"success": False, "code": "unreachable",
                      "error": f"OBS is not running or its WebSocket server is off (nothing "
                                                 f"answered at 127.0.0.1:{port})"}


# registration

def test_register_obs_tools():
    ctx = RecordingContext()
    assert register_obs_tools(ctx) == list(NAMES)
    assert OBS_TOOL_NAMES == NAMES and OBS_TOOLSET == "obs"
    assert [tool["name"] for tool in ctx.tools] == list(NAMES)
    for tool in ctx.tools:
        assert tool["toolset"] == "obs" and tool["is_async"] is True
        assert tool["check_fn"] is obs.configured
        assert "override" not in tool and "emoji" not in tool and "requires_env" not in tool
        schema = tool["schema"]
        assert schema["name"] == tool["name"] and schema["description"] == tool["description"]
        parameters = schema["parameters"]
        assert parameters["type"] == "object" and set(parameters["required"]) <= set(parameters["properties"])
        assert json.loads(json.dumps(schema)) == schema
        assert inspect.iscoroutinefunction(tool["handler"])


def test_tool_parameters():
    schemas = {schema["name"]: schema["parameters"] for schema, _ in obs_tools.TOOLS}
    assert {name: (sorted(p["properties"]), p["required"]) for name, p in schemas.items()} == {
        "obs_status": ([], []), "obs_list_scenes": ([], []), "obs_switch_scene": (["scene"], ["scene"]),
        "obs_list_audio_inputs": ([], []), "obs_set_mute": (["input", "muted"], ["input", "muted"]),
        "obs_show_caption": (["seconds", "text"], ["text"]), "obs_clear_caption": ([], []),
        "obs_set_source_visible": (["scene", "source", "visible"], ["scene", "source", "visible"]),
        "obs_save_replay": ([], []),
    }
    assert schemas["obs_set_mute"]["properties"]["muted"]["enum"] == ["true", "false", "toggle"]
    seconds = schemas["obs_show_caption"]["properties"]["seconds"]
    assert (seconds["minimum"], seconds["maximum"], seconds["default"]) == (3, 60, 12)


def test_no_tool_starts_or_stops_output():
    for name in OBS_TOOL_NAMES:
        assert not any(word in name for word in ("start", "stop", "record", "stream"))


async def test_dispatch_through_the_hermes_registry(fake):
    """Hermes runs async handlers on its own loop from the agent's thread; the answer must still be our JSON."""
    from tools.registry import registry

    class RegistryContext:
        def register_tool(self, **kwargs: Any) -> None:
            registry.register(**kwargs)

    names = register_obs_tools(RegistryContext())
    try:
        assert registry.get_entry("obs_status").check_fn() is True
        listed = json.loads(await asyncio.to_thread(registry.dispatch, "obs_list_scenes", {}, task_id="t1"))
        assert listed == {"success": True, "live_scene": "Main", "scenes": ["Main", "BRB", "Gaming"]}
        switched = json.loads(await asyncio.to_thread(registry.dispatch, "obs_switch_scene", {"scene": "brb"}))
        assert switched == {"success": True, "scene": "BRB", "previous": "Main", "changed": True}
        assert fake.program == "BRB"
    finally:
        for name in names:
            registry.deregister(name)


# handlers never raise

@pytest.mark.parametrize("args", [None, "scene", ["BRB"], 7])
async def test_odd_arguments_come_back_as_errors(fake, args):
    result = json.loads(await HANDLERS["obs_switch_scene"](args))
    assert result == {"success": False, "error": "scene is required", "code": "bad_arguments"}


async def test_unexpected_exceptions_come_back_as_json(monkeypatch, caplog):
    def broken(config=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(obs, "session", broken)
    for name in NAMES:
        args = {"scene": "a", "input": "a", "muted": "true", "text": "hi", "source": "a", "visible": True}
        result = await run(name, args)
        assert result == {"success": False, "error": f"{name} failed unexpectedly (RuntimeError)", "code": "failed"}
    assert "OBS tool obs_status failed" in caplog.text


async def test_handlers_take_hermes_context_keywords(fake):
    assert (await run("obs_list_scenes", {}, task_id="t1", session_id="s1", user_task="x"))["success"] is True


# callers

OFF_HERE = "OBS tools are off on {}; the streamer can allow them with the tools_from_other_platforms plugin setting"


async def run_as(name: str, args: dict | None = None, **who) -> dict:
    with session(**who):
        return await run(name, args)


def allow_other_platforms(monkeypatch) -> None:
    monkeypatch.setattr(twitch_tools, "_get_config", {"tools_from_other_platforms": True}.get)


@pytest.mark.parametrize("who", [{}, {"source": "cli"}, {"source": "tui"}, {"source": "desktop"},
                                 {"platform": "acp"}, {"platform": "local"}])
async def test_the_local_operator_may_use_obs(fake, who):
    assert (await run_as("obs_switch_scene", {"scene": "BRB"}, **who))["success"] is True
    assert fake.program == "BRB"


@pytest.mark.parametrize("channel", ["#chan", "#elsewhere"])
async def test_the_owner_may_use_obs_from_twitch_chat(fake, chat, channel):
    assert (await run_as("obs_switch_scene", {"scene": "BRB"}, **twitch_turn(chat.room, channel)))["success"] is True
    assert fake.program == "BRB"


@pytest.mark.parametrize("user_id", [MOD_ID, VIEWER_ID, "999", ""])
async def test_every_other_twitch_asker_is_refused_before_obs_is_contacted(fake, chat, monkeypatch, user_id):
    allow_other_platforms(monkeypatch)  # a setting for other platforms, never for Twitch askers
    for name in NAMES:
        result = await run_as(name, {"scene": "BRB", "text": "hi"}, **twitch_turn(user_id))
        assert result == {"success": False, "error": "only the streamer may control OBS", "code": "not_allowed"}
    assert fake.connections == 0 and fake.program == "Main"


async def test_a_moderator_elsewhere_is_refused_too(fake, chat):
    result = await run_as("obs_status", **twitch_turn(MOD_ID, "#elsewhere"))
    assert result["code"] == "not_allowed" and fake.connections == 0


async def test_a_twitch_turn_without_the_adapter_in_this_process_is_refused(fake):
    result = await run_as("obs_status", **twitch_turn("1234"))
    assert result["code"] == "not_allowed" and fake.connections == 0


@pytest.mark.parametrize("who, where", [
    ({"platform": "telegram", "chat_id": "1", "user_id": "9"}, "telegram"),
    ({"platform": "discord", "chat_id": "1", "user_id": "9"}, "discord"),
    ({"cron_session": "1"}, "cron"),
])
async def test_other_platforms_and_cron_need_the_setting(fake, monkeypatch, who, where):
    assert await run_as("obs_status", **who) == {"success": False, "error": OFF_HERE.format(where),
                                                 "code": "not_allowed"}
    assert fake.connections == 0
    allow_other_platforms(monkeypatch)
    assert (await run_as("obs_status", **who))["success"] is True


@pytest.mark.parametrize("surface", ["a2a", "bot_room"])
async def test_peers_on_local_routes_need_the_setting(fake, monkeypatch, surface):
    monkeypatch.setattr(roles, "_surface", lambda: surface)
    assert await run_as("obs_status", platform="local") == {"success": False, "error": OFF_HERE.format(surface),
                                                            "code": "not_allowed"}
    allow_other_platforms(monkeypatch)
    assert (await run_as("obs_status", platform="local"))["success"] is True


async def test_not_knowing_the_caller_refuses(fake, monkeypatch):
    def broken():
        raise RuntimeError("no session context")

    monkeypatch.setattr(roles, "caller", broken)
    assert await run("obs_status") == {"success": False, "error": "obs_status failed unexpectedly (RuntimeError)",
                                       "code": "failed"}
    assert fake.connections == 0


# read-only tools

async def test_obs_status(fake):
    fake.streaming = True
    result = await run("obs_status")
    assert result["success"] is True and result["live_scene"] == "Main" and result["streaming"] is True
    assert result["fps"] == 60.0 and result["cpu_percent"] == 3.5 and result["stream_time"] == "01:02:03.456"
    assert result["dropped_frames"]["network"] == {"dropped": 12, "total": 215000}
    assert fake.request_types() == ["GetCurrentProgramScene", "GetStreamStatus", "GetRecordStatus", "GetStats"]


async def test_obs_status_when_obs_is_down(obs_down):
    assert_down(await run("obs_status"), obs_down)


async def test_obs_status_with_a_wrong_password(fake, monkeypatch):
    fake.password = PASSWORD
    monkeypatch.setenv(obs.PASSWORD_ENV, "Wr0ng-pass")
    result = await run("obs_status")
    assert result["success"] is False and result["error"].startswith("OBS rejected the password")
    monkeypatch.setenv(obs.PASSWORD_ENV, PASSWORD)
    assert (await run("obs_status"))["success"] is True


async def test_obs_status_with_a_remote_url_refused(monkeypatch):
    monkeypatch.setenv(obs.URL_ENV, "ws://192.168.1.20:4455")
    result = await run("obs_status")
    assert result["success"] is False and obs.ALLOW_REMOTE_ENV in result["error"]
    assert result["error"].endswith("(it points at 192.168.1.20)")


async def test_obs_list_scenes(fake):
    fake.program = "Gaming"
    assert await run("obs_list_scenes") == {"success": True, "live_scene": "Gaming",
                                            "scenes": ["Main", "BRB", "Gaming"]}


async def test_obs_list_scenes_failure(fake):
    fake.fail["GetSceneList"] = (205, "Scene collection is loading.")
    assert await run("obs_list_scenes") == {
        "success": False, "error": "OBS refused GetSceneList (status 205: Scene collection is loading.)",
        "code": "request"}


async def test_obs_list_audio_inputs(fake):
    assert await run("obs_list_audio_inputs") == {"success": True, "inputs": [
        {"name": "Desktop Audio", "muted": False}, {"name": "Mic/Aux", "muted": True},
        {"name": "Camera", "muted": False}]}


async def test_obs_list_audio_inputs_failure(obs_down):
    assert_down(await run("obs_list_audio_inputs"), obs_down)


# scene switching

async def test_obs_switch_scene(fake):
    assert await run("obs_switch_scene", {"scene": "  Gaming "}) == {
        "success": True, "scene": "Gaming", "previous": "Main", "changed": True}
    assert await run("obs_switch_scene", {"scene": "gaming"}) == {
        "success": True, "scene": "Gaming", "previous": "Gaming", "changed": False}
    assert fake.program == "Gaming"


@pytest.mark.parametrize("args, error, code", [
    ({"scene": "Nope"}, 'scene "Nope" does not exist', "not_found"),
    ({"scene": " "}, "scene is required", "bad_arguments"),
    ({}, "scene is required", "bad_arguments"),
    ({"scene": 5}, "scene is required", "bad_arguments"),
])
async def test_obs_switch_scene_failures(fake, args, error, code):
    assert await run("obs_switch_scene", args) == {"success": False, "error": error, "code": code}
    assert fake.program == "Main"


# mute

@pytest.mark.parametrize("muted, before, after", [
    ("true", False, True), ("false", True, False), (" TOGGLE ", True, False), ("toggle", False, True),
    (True, False, True), (False, True, False),
])
async def test_obs_set_mute(fake, muted, before, after):
    fake.inputs["Mic/Aux"].muted = before
    assert await run("obs_set_mute", {"input": "mic/aux", "muted": muted}) == {
        "success": True, "input": "Mic/Aux", "muted": after}
    assert fake.inputs["Mic/Aux"].muted is after


@pytest.mark.parametrize("args, error, code", [
    ({"input": "Mic/Aux", "muted": "maybe"}, 'muted must be true or false or "toggle"', "bad_arguments"),
    ({"input": "Mic/Aux", "muted": 1}, 'muted must be true or false or "toggle"', "bad_arguments"),
    ({"input": "Mic/Aux"}, 'muted must be true or false or "toggle"', "bad_arguments"),
    ({"muted": "true"}, "input is required", "bad_arguments"),
    ({"input": "Kazoo", "muted": "true"}, 'input "Kazoo" does not exist', "not_found"),
    ({"input": "Webcam Frame", "muted": "true"}, 'input "Webcam Frame" has no audio', "no_audio"),
])
async def test_obs_set_mute_failures(fake, args, error, code):
    assert await run("obs_set_mute", args) == {"success": False, "error": error, "code": code}


# captions

def test_caption_text_short_and_clean():
    assert caption_text("  hello\n\tchat\x00 \u202eworld  ") == "hello chat world"
    assert caption_text("x" * 240) == "x" * 240
    assert caption_text("") == ""


@pytest.mark.parametrize("text, cut", [
    ("word " * 100, ("word " * 47).rstrip() + "..."),
    ("alpha, beta " * 30, "alpha, beta " * 19 + "alpha..."),
    ("a" * 237 + " tail", "a" * 237 + "..."),
    ("ab " * 79 + "c" * 30, ("ab " * 79).rstrip() + "..."),
    ("x" * 300, "x" * 237 + "..."),
    ("one " + "y" * 300, "one..."),
])
def test_caption_text_cut_on_a_word_boundary(text, cut):
    assert caption_text(text) == cut and len(cut) <= 240


@pytest.mark.parametrize("value, seconds", [
    (None, 12), (12, 12), (1, 3), (0, 3), (-5, 3), (61, 60), (1000, 60), (7.4, 7), (7.6, 8), ("15", 15), (" 20 ", 20),
])
def test_caption_seconds(value, seconds):
    assert obs_tools._seconds(value) == seconds


@pytest.mark.parametrize("value", [True, False, "soon", "nan", "inf", float("nan"), [12], {}])
def test_caption_seconds_refused(value):
    with pytest.raises(obs.ObsError) as caught:
        obs_tools._seconds(value)
    assert caught.value.message == "seconds must be a number from 3 to 60"


async def test_show_caption_and_its_timer(fake, timers):
    result = await run("obs_show_caption", {"text": "Thanks for the raid!"})
    assert result == {"success": True, "caption": "Thanks for the raid!", "seconds": 12, "source": "tallyhand_caption"}
    assert fake.caption() == "Thanks for the raid!"
    [timer] = timers
    assert timer.interval == 12 and timer.daemon and timer.started and not timer.cancelled
    await timer.fire()
    assert fake.caption() == ""


async def test_later_caption_replaces_the_earlier_one(fake, timers):
    await run("obs_show_caption", {"text": "first", "seconds": 30})
    await run("obs_show_caption", {"text": "second", "seconds": 5})
    first, second = timers
    assert first.cancelled and not second.cancelled and (first.interval, second.interval) == (30, 5)
    await first.fire()
    assert fake.caption() == "second"
    await second.fire()
    assert fake.caption() == ""


async def test_timer_leaves_a_caption_someone_else_changed(fake, timers):
    await run("obs_show_caption", {"text": "mine"})
    fake.inputs["tallyhand_caption"].settings["text"] = "set from another process"
    await timers[0].fire()
    assert fake.caption() == "set from another process"


async def test_timer_survives_obs_going_away(fake, timers, caplog):
    caplog.set_level(logging.INFO)
    await run("obs_show_caption", {"text": "mine"})
    await fake.stop()
    await timers[0].fire()
    assert "could not clear the caption" in caplog.text


async def test_clear_caption_cancels_the_timer(fake, timers):
    await run("obs_show_caption", {"text": "mine"})
    assert await run("obs_clear_caption") == {"success": True, "source": "tallyhand_caption"}
    assert fake.caption() == "" and timers[0].cancelled
    fake.inputs["tallyhand_caption"].settings["text"] = "mine"
    await timers[0].fire()
    assert fake.caption() == "mine"


async def test_failed_caption_keeps_the_pending_clear(fake, timers, monkeypatch):
    await run("obs_show_caption", {"text": "mine"})
    monkeypatch.setenv(obs.URL_ENV, f"ws://127.0.0.1:{closed_port()}")
    assert (await run("obs_show_caption", {"text": "next"}))["success"] is False
    assert len(timers) == 1 and not timers[0].cancelled
    monkeypatch.setenv(obs.URL_ENV, fake.url)
    await timers[0].fire()
    assert fake.caption() == ""


async def test_caption_is_cleaned_cut_and_clamped(fake, timers):
    long = "Welcome\nto the stream, " + "everyone " * 40
    result = await run("obs_show_caption", {"text": long, "seconds": 99})
    assert result["seconds"] == 60 and timers[0].interval == 60
    assert result["caption"] == caption_text(long) == fake.caption()
    assert len(result["caption"]) <= 240 and "\n" not in result["caption"] and result["caption"].endswith("...")


async def test_caption_source_from_settings(fake, monkeypatch):
    monkeypatch.setenv(obs.CAPTION_SOURCE_ENV, "BRB Text")
    result = await run("obs_show_caption", {"text": "Back in five"})
    assert result["source"] == "BRB Text" and fake.inputs["BRB Text"].settings["text"] == "Back in five"


@pytest.mark.parametrize("args, error", [
    ({}, "text is required"),
    ({"text": "   "}, "text is required"),
    ({"text": "\x00\x01\u200b"}, "text has nothing to show"),
    ({"text": "hi", "seconds": "soon"}, "seconds must be a number from 3 to 60"),
])
async def test_show_caption_argument_failures(fake, timers, args, error):
    assert await run("obs_show_caption", args) == {"success": False, "error": error, "code": "bad_arguments"}
    assert timers == [] and fake.requests == []


async def test_show_caption_source_failures(fake, timers, monkeypatch):
    del fake.inputs["tallyhand_caption"]
    result = await run("obs_show_caption", {"text": "hi"})
    assert result["success"] is False and result["error"].startswith('the caption source "tallyhand_caption" does '
                                                                     'not exist')
    monkeypatch.setenv(obs.CAPTION_SOURCE_ENV, "Webcam Frame")
    result = await run("obs_show_caption", {"text": "hi"})
    assert result == {"success": False, "code": "not_text",
                      "error": 'the caption source "Webcam Frame" is not a Text source (its kind is image_source)'}
    assert timers == []


async def test_clear_caption_failure(obs_down, timers):
    assert_down(await run("obs_clear_caption"), obs_down)


async def test_real_timer_thread_clears_the_caption(fake):
    """The production timer: a daemon thread that runs its own event loop to clear the text."""
    timer = obs_tools.CaptionTimer()
    config = obs.load_config()
    async with obs.session(config) as client:
        await obs.set_text(client, "tallyhand_caption", "short lived")
    timer.start(0.05, config, "tallyhand_caption", "short lived")
    await until(lambda: fake.caption() == "", timeout=5, what="the caption to clear")


# visibility and replay

async def test_obs_set_source_visible(fake):
    assert await run("obs_set_source_visible", {"scene": "main", "source": "WEBCAM FRAME", "visible": False}) == {
        "success": True, "scene": "Main", "source": "Webcam Frame", "visible": False}
    assert [item.enabled for item in fake.scenes["Main"]] == [True, False, True]
    assert (await run("obs_set_source_visible", {"scene": "Main", "source": "Webcam Frame", "visible": "true"}))[
        "visible"] is True
    assert all(item.enabled for item in fake.scenes["Main"])


@pytest.mark.parametrize("args, error, code", [
    ({"scene": "Nope", "source": "Camera", "visible": True}, 'scene "Nope" does not exist', "not_found"),
    ({"scene": "BRB", "source": "Camera", "visible": True}, 'source "Camera" is not in scene "BRB"', "not_found"),
    ({"scene": "BRB", "source": "BRB Text", "visible": "yes"}, "visible must be true or false", "bad_arguments"),
    ({"scene": "BRB", "visible": True}, "source is required", "bad_arguments"),
])
async def test_obs_set_source_visible_failures(fake, args, error, code):
    assert await run("obs_set_source_visible", args) == {"success": False, "error": error, "code": code}


async def test_obs_save_replay(fake):
    fake.replay = True
    assert await run("obs_save_replay") == {"success": True, "saved": True}
    assert fake.replays_saved == 1


@pytest.mark.parametrize("replay, error, code", [(None, obs.REPLAY_OFF, "replay_off"),
                                                 (False, obs.REPLAY_STOPPED, "replay_stopped")])
async def test_obs_save_replay_failures(fake, replay, error, code):
    fake.replay = replay
    assert await run("obs_save_replay") == {"success": False, "error": error, "code": code}
    assert fake.replays_saved == 0


# secrets

async def test_tool_output_never_holds_the_password_or_auth_string(fake, monkeypatch, caplog):
    fake.password, fake.echo_auth = PASSWORD, True
    monkeypatch.setenv(obs.PASSWORD_ENV, PASSWORD)
    outputs = [await HANDLERS["obs_switch_scene"]({"scene": "Nope"})]
    fake.fail["GetSceneList"] = (205, "Failed")
    outputs.append(await HANDLERS["obs_list_scenes"]({}))
    monkeypatch.setenv(obs.PASSWORD_ENV, "Wr0ng-pass")
    outputs.append(await HANDLERS["obs_status"]({}))
    sent = {identify["authentication"] for identify in fake.identifies}
    assert len(sent) == 3 and all("***" in output for output in outputs[1:])
    for secret in (PASSWORD, "Wr0ng-pass", *sent):
        assert not any(secret in output for output in outputs) and secret not in caplog.text
