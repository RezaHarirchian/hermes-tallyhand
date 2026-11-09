"""The pre_tool_call sandbox: what each Twitch asker may run, approvals for bans, the failure path, and the hook
inside Hermes' own dispatch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from harness import MOD_ID, VIEWER_ID, session, twitch_turn
from tallyhand import obs_tools, roles, sandbox, twitch_tools
from tallyhand.sandbox import pre_tool_call

ROOT = Path(__file__).resolve().parent.parent
MODERATOR_SUBSET = ["twitch_stream_info", "twitch_delete_message", "twitch_timeout", "twitch_announce",
                    "twitch_shoutout", "twitch_create_clip"]
OWNER_ONLY = ["twitch_ban", "twitch_unban"]
CORE = ["terminal", "write_file", "read_file", "browser_navigate", "memory", "execute_code", "delegate_task",
        "session_search", "skill_view", "tool_call", "mcp_filesystem_read", "obs_switch_scene", "context_engine_search"]


def hook(tool: str, args: dict | None = None, **who):
    with session(**who):
        return pre_tool_call(tool_name=tool, args=dict(args or {}), task_id="t", session_id="s", tool_call_id="c",
                             turn_id="u", api_request_id="", middleware_trace=[],
                             telemetry_schema_version="hermes.observer.v1")


# Twitch turns


@pytest.mark.parametrize("tool", ["todo_list", "tool_search", "tool_describe"])
async def test_everyone_keeps_the_todo_list_and_the_tool_catalog(chat, tool):
    for user_id in (VIEWER_ID, MOD_ID, chat.room, "999"):
        assert hook(tool, **twitch_turn(user_id)) is None


@pytest.mark.parametrize("tool", CORE + MODERATOR_SUBSET + OWNER_ONLY)
async def test_viewers_may_run_nothing_else(chat, tool):
    verdict = hook(tool, **twitch_turn(VIEWER_ID))
    assert verdict == {"action": "block", "message": f"{tool} is not available to you in Twitch chat."}


@pytest.mark.parametrize("tool", CORE + OWNER_ONLY)
async def test_moderators_are_limited_to_their_subset(chat, tool):
    assert hook(tool, **twitch_turn(MOD_ID))["action"] == "block"


@pytest.mark.parametrize("tool", MODERATOR_SUBSET)
async def test_moderator_subset(chat, tool):
    assert hook(tool, {"login": "spammer"}, **twitch_turn(MOD_ID)) is None


@pytest.mark.parametrize(("seconds", "verdict"), [
    (1, None), (600, None), ("600", None), (600.0, None), ("600.0", None), ("6e2", None), (None, None), ("", None),
    (601, "block"), (1_209_600, "block"), ("1e6", "block"), ("1209600.0", "block"),
    ("junk", "block"), ("600.5", "block"), (600.5, "block"), ("inf", "block"), ("nan", "block"), (True, "block")])
async def test_moderator_timeout_cap(chat, seconds, verdict):
    args = {"login": "spammer"} if seconds is None else {"login": "spammer", "seconds": seconds}
    result = hook("twitch_timeout", args, **twitch_turn(MOD_ID))
    assert (result or {}).get("action") == verdict
    if verdict and isinstance(seconds, int | float) and not isinstance(seconds, bool) and seconds == int(seconds):
        assert result["message"] == "Moderators can time someone out for at most 600 seconds."


async def test_a_moderator_in_another_channel_is_a_viewer_here(chat, twitch):
    await twitch.privmsg("#chan", "!hermes hi", login="friend", user_id="3000", badges="broadcaster/1",
                         room_id="3000")  # their own room's badge, seen in ours: not privileged in #chan
    other = {"platform": "twitch", "chat_id": "#elsewhere", "user_id": MOD_ID}
    assert hook("twitch_timeout", {"login": "x"}, **other)["action"] == "block"


@pytest.mark.parametrize("tool", CORE + MODERATOR_SUBSET)
async def test_the_owner_is_unrestricted(chat, tool):
    assert hook(tool, {"login": "spammer"}, **twitch_turn(chat.room)) is None


async def test_an_owner_turn_cannot_even_ask_about_a_channel_the_bot_does_not_serve(chat):
    owner = twitch_turn(chat.room)
    for tool in ("twitch_ban", "twitch_announce", "twitch_create_clip"):
        blocked = hook(tool, {"login": "spammer", "message": "hi", "channel": "#other"}, **owner)
        assert blocked["action"] == "block" and "not a channel this bot serves" in blocked["message"]
    assert hook("twitch_stream_info", {"channel": "other"}, **owner) is None
    inside_other = twitch_turn(chat.room, "#other")
    assert hook("twitch_ban", {"login": "spammer", "channel": "#other"}, **inside_other)["action"] == "block"
    assert hook("twitch_ban", {"login": "spammer", "channel": "#other"}, source="cli")["action"] == "approve"


async def test_bans_unbans_and_long_timeouts_need_approval(chat, monkeypatch):
    monkeypatch.setenv("TWITCH_CHANNEL", "chan,other")
    owner = twitch_turn(chat.room)
    assert hook("twitch_ban", {"login": "@Spammer", "reason": "bot\nspam"}, **owner) == {
        "action": "approve", "message": "Ban spammer from #chan (reason: bot spam)?",
        "rule_key": "twitch_ban:chan:spammer"}
    assert hook("twitch_unban", {"login": "spammer", "channel": "#Other"}, **owner) == {
        "action": "approve", "message": "Lift the ban or timeout on spammer in #other?",
        "rule_key": "twitch_unban:other:spammer"}
    assert hook("twitch_timeout", {"login": "spammer", "seconds": 7200}, **owner) == {
        "action": "approve", "message": "Time spammer out of #chan for 2h 0m?",
        "rule_key": "twitch_timeout:chan:spammer:7200"}
    assert hook("twitch_timeout", {"login": "spammer", "seconds": 600}, **owner) is None
    assert hook("twitch_ban", {"login": "not a login"}, **owner) == {
        "action": "block", "message": "twitch_ban refused: login must be a Twitch login."}


@pytest.mark.parametrize(("seconds", "parsed"), [("1209600.0", 1_209_600), ("1.2096e6", 1_209_600),
                                                 ("1e6", 1_000_000), (" 7200 ", 7200), (7200.0, 7200)])
async def test_long_timeouts_need_approval_however_the_number_is_written(chat, seconds, parsed):
    verdict = hook("twitch_timeout", {"login": "spammer", "seconds": seconds}, **twitch_turn(chat.room))
    assert verdict["action"] == "approve"
    assert verdict["rule_key"] == f"twitch_timeout:chan:spammer:{parsed}"


@pytest.mark.parametrize("seconds", ["inf", "nan", "600.5", 600.5, "ten", [600], {"s": 1}, 0, -5, 1_209_601])
async def test_unreadable_timeouts_are_blocked_for_the_owner_too(chat, seconds):
    verdict = hook("twitch_timeout", {"login": "spammer", "seconds": seconds}, **twitch_turn(chat.room))
    assert verdict == {"action": "block",
                       "message": "twitch_timeout refused: seconds must be a whole number from 1 to 1209600."}


@pytest.mark.parametrize("raw", ["1", "600", "600.0", "6e2", "1e6", "1209600.0", "1.2096e6", " 42 ", "+7", "1_000",
                                 "0", "-5", "600.5", "inf", "-inf", "nan", "1e400", "", "  ", "ten", "0x10"])
def test_seconds_are_read_the_way_hermes_coerces_them(raw):
    from tools.arg_coercion import _coerce_number

    coerced = _coerce_number(raw, integer_only=True)
    try:
        ours = twitch_tools.timeout_seconds({"seconds": raw})
    except twitch_tools.BadArguments:
        ours = None
    if raw.strip() == "":
        assert ours == twitch_tools.DEFAULT_TIMEOUT or ours is None
    elif isinstance(coerced, int) and 1 <= coerced <= 1_209_600:
        assert ours == coerced  # the hook sees the number the tool will get
    else:
        assert ours is None  # anything else is refused at both ends


# OBS tools


def test_no_obs_tool_is_in_the_moderator_subset():
    assert not set(obs_tools.OBS_TOOL_NAMES) & (twitch_tools.MODERATOR_TOOLS | sandbox.CATALOG_TOOLS)


@pytest.mark.parametrize("tool", obs_tools.OBS_TOOL_NAMES)
async def test_obs_tools_are_blocked_for_every_twitch_asker_but_the_owner(chat, tool):
    blocked = {"action": "block", "message": f"{tool} is not available to you in Twitch chat."}
    for user_id in (VIEWER_ID, MOD_ID, "999", ""):
        assert hook(tool, {"scene": "BRB"}, **twitch_turn(user_id)) == blocked
    elsewhere = {"platform": "twitch", "chat_id": "#elsewhere", "user_id": MOD_ID}  # a moderator of #chan only
    assert hook(tool, {"scene": "BRB"}, **elsewhere) == blocked
    assert hook(tool, {"scene": "BRB"}, **twitch_turn(chat.room)) is None
    assert hook(tool, {"scene": "BRB"}, platform="twitch", chat_id="#elsewhere", user_id=chat.room) is None


@pytest.mark.parametrize("who", [{"source": "cli"}, {"platform": "telegram", "chat_id": "1", "user_id": "9"},
                                 {"cron_session": "1"}])
def test_obs_tools_elsewhere_are_left_to_their_own_caller_check(who):
    for tool in obs_tools.OBS_TOOL_NAMES:
        assert hook(tool, {}, **who) is None


async def test_hermes_dispatch_blocks_obs_tools_for_moderators(chat, hermes_hook):
    message, _ = dispatch("obs_switch_scene", {"scene": "BRB"}, **twitch_turn(MOD_ID))
    assert message == "obs_switch_scene is not available to you in Twitch chat."
    assert dispatch("obs_switch_scene", {"scene": "BRB"}, **twitch_turn(chat.room)) == (None, None)


# other surfaces


@pytest.mark.parametrize("who", [{"source": "cli"}, {"source": "tui"}, {"source": "desktop"}, {"platform": "local"}])
def test_the_local_operator_runs_twitch_tools_with_approval_for_bans(who, monkeypatch):
    monkeypatch.setenv("TWITCH_CHANNEL", "chan")
    assert hook("twitch_announce", {"message": "hi"}, **who) is None
    assert hook("terminal", {"command": "ls"}, **who) is None
    assert hook("twitch_ban", {"login": "spammer"}, **who)["action"] == "approve"


def test_other_platforms_lose_twitch_tools_unless_allowed(monkeypatch):
    monkeypatch.setenv("TWITCH_CHANNEL", "chan")
    telegram = {"platform": "telegram", "chat_id": "1", "user_id": "9"}
    assert hook("twitch_stream_info", **telegram)["action"] == "block"
    assert hook("terminal", {"command": "ls"}, **telegram) is None  # not ours to judge
    monkeypatch.setattr(twitch_tools, "_get_config", {"tools_from_other_platforms": True}.get)
    assert hook("twitch_stream_info", **telegram) is None
    assert hook("twitch_ban", {"login": "spammer"}, **telegram)["action"] == "approve"


def test_peers_on_local_routes_are_not_the_operator(monkeypatch):
    monkeypatch.setenv("TWITCH_CHANNEL", "chan")
    for surface in ("a2a", "bot_room"):
        monkeypatch.setattr(roles, "_surface", lambda surface=surface: surface)
        assert hook("twitch_ban", {"login": "spammer"}, platform="local")["action"] == "block"
        assert hook("twitch_announce", {"message": "hi"}, platform="local")["action"] == "block"
    monkeypatch.setattr(roles, "_surface", lambda: "tui")
    assert hook("twitch_announce", {"message": "hi"}, platform="local") is None


def test_the_classic_cli_and_cron():
    assert hook("twitch_ban", {"login": "spammer", "channel": "chan"})["action"] == "approve"  # no binding: the CLI
    assert hook("twitch_ban", {"login": "spammer", "channel": "chan"}, cron_session="1")["action"] == "block"
    assert hook("web_search", {"query": "x"}, cron_session="1") is None


# odd input and internal errors


@pytest.mark.parametrize("kwargs", [{}, {"tool_name": None}, {"tool_name": "twitch_ban", "args": None},
                                    {"tool_name": "twitch_ban", "args": "login=x"}, {"args": {"x": object()}}])
def test_never_raises_on_odd_arguments(kwargs):
    with session(source="cli"):
        verdict = pre_tool_call(**kwargs)
    assert verdict is None or verdict["action"] in ("block", "approve")


async def test_an_internal_error_blocks_twitch_turns(chat, monkeypatch, caplog):
    def broken():
        raise RuntimeError("registry gone")

    monkeypatch.setattr(sandbox, "todo_tools", broken)
    assert hook("todo_list", **twitch_turn(VIEWER_ID)) == {"action": "block", "message": sandbox.FAILED}
    assert hook("twitch_ban", {"login": "x"}, source="cli")["action"] == "approve"  # never touched todo_tools
    assert "sandbox check for todo_list failed" in caplog.text


async def test_not_knowing_whose_turn_it_is_blocks_where_twitch_runs(chat, monkeypatch):
    def broken():
        raise RuntimeError("no session context")

    monkeypatch.setattr(roles, "caller", broken)
    assert hook("terminal", {"command": "ls"}, source="cli") == {"action": "block", "message": sandbox.FAILED}
    await chat.disconnect()
    assert hook("terminal", {"command": "ls"}, source="cli") is None  # no adapter: a plain CLI process
    assert hook("twitch_ban", {"login": "x"}, source="cli") == {"action": "block", "message": sandbox.FAILED}


# inside Hermes' own dispatch


@pytest.fixture
def hermes_hook():
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager

    ctx = PluginContext(PluginManifest(name="tallyhand", source="user"), get_plugin_manager())
    handle = ctx.register_hook("pre_tool_call", pre_tool_call)
    yield
    handle.dispose()


def dispatch(tool: str, args: dict, **who):
    from hermes_cli.plugins import _dispatch_pre_tool_call_hooks

    with session(**who):
        return _dispatch_pre_tool_call_hooks(tool, args, task_id="t", session_id="s", tool_call_id="c", turn_id="u")


async def test_hermes_dispatch_blocks_a_viewers_tool(chat, hermes_hook):
    message, _ = dispatch("terminal", {"command": "id"}, **twitch_turn(VIEWER_ID))
    assert message == "terminal is not available to you in Twitch chat."
    assert dispatch("todo_list", {}, **twitch_turn(VIEWER_ID)) == (None, None)


async def test_hermes_dispatch_fails_closed_on_a_ban_with_nobody_to_approve(chat, hermes_hook):
    message, _ = dispatch("twitch_ban", {"login": "spammer"}, **twitch_turn(chat.room))
    assert message and "Ban spammer from #chan?" in message


def test_hermes_dispatch_refuses_bans_in_cron(hermes_hook, monkeypatch):
    monkeypatch.setenv("TWITCH_CHANNEL", "chan")
    message, _ = dispatch("twitch_ban", {"login": "spammer"}, cron_session="1")
    assert message == ("Twitch tools are off here; the streamer can allow them with the tools_from_other_platforms "
                       "plugin setting.")
    # Allowed for other surfaces, a ban still needs a person, and cron has none.
    monkeypatch.setattr(twitch_tools, "_get_config", {"tools_from_other_platforms": True}.get)
    message, _ = dispatch("twitch_ban", {"login": "spammer"}, cron_session="1")
    assert message and message.startswith("BLOCKED") and "cron" in message


@pytest.fixture
def hermes_tools(hermes_hook):
    """The twitch tools in Hermes' real registry, beside the real hook."""
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager

    class Handles:
        get_config = None

        def __init__(self, ctx):
            self.ctx, self.handles = ctx, []

        def register_tool(self, **kwargs):
            self.handles.append(self.ctx.register_tool(**kwargs))

    tools = Handles(PluginContext(PluginManifest(name="tallyhand", source="user"), get_plugin_manager()))
    twitch_tools.register_tools(tools)
    yield
    for handle in tools.handles:
        if handle is not None:
            handle.dispose()


def run_tool(tool: str, args: dict, **who) -> dict:
    """Hermes' own dispatch after the hooks have passed: argument coercion, then the registry."""
    from model_tools import handle_function_call

    with session(**who):
        return json.loads(handle_function_call(tool, dict(args), task_id="t", skip_pre_tool_call_hook=True))


async def test_a_coerced_long_timeout_cannot_skip_approval(helix_api, chat, hermes_tools):
    """The agent loop's order: hooks see the raw arguments, Hermes coerces them, then the tool runs."""
    chat.helix.base_url = helix_api.url
    helix_api.add_user("spammer", "77")
    for seconds in ("1209600.0", "1e6"):
        args = {"login": "spammer", "seconds": seconds}
        block, _ = dispatch("twitch_timeout", args, **twitch_turn(chat.room))
        assert block and "Time spammer out of #chan for" in block  # nobody here to approve it
        ran = run_tool("twitch_timeout", args, **twitch_turn(chat.room))
        assert ran.get("seconds") == int(float(seconds)), ran  # what would have run without the hook's verdict
    block, _ = dispatch("twitch_timeout", {"login": "spammer", "seconds": "300.0"}, **twitch_turn(chat.room))
    assert block is None


async def test_an_approved_ban_reaches_twitch(helix_api, chat, hermes_tools, monkeypatch):
    """The whole escalation: the hook asks, Hermes' approval grants it (here approvals.mode off), the ban runs."""
    monkeypatch.setattr("tools.approval_context._get_approval_mode", lambda: "off")
    chat.helix.base_url = helix_api.url
    helix_api.add_user("spammer", "77")
    args = {"login": "spammer", "reason": "bot"}
    assert hook("twitch_ban", args, **twitch_turn(chat.room))["action"] == "approve"
    block, _ = dispatch("twitch_ban", args, **twitch_turn(chat.room))
    assert block is None
    assert run_tool("twitch_ban", args, **twitch_turn(chat.room))["banned"] == "spammer"
    assert helix_api.bans == {(chat.room, "77"): None}


@pytest.mark.parametrize("tool, args", [("twitch_ban", {"login": "spammer"}), ("twitch_unban", {"login": "spammer"}),
                                        ("twitch_timeout", {"login": "spammer", "seconds": 3600})])
def test_single_query_runs_never_ban(hermes_hook, monkeypatch, tool, args):
    """hermes -z runs in yolo mode, where Hermes would approve without asking anyone; chat -q has nobody to ask."""
    monkeypatch.setenv("TWITCH_CHANNEL", "chan")
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    assert hook(tool, args) == {"action": "block",
                                "message": f"{tool} needs a person to approve it; run it from an interactive session."}
    message, _ = dispatch(tool, args)
    assert message == f"{tool} needs a person to approve it; run it from an interactive session."
    assert hook("twitch_timeout", {"login": "spammer", "seconds": 300}) is None  # nothing to approve: allowed
    assert hook("twitch_announce", {"message": "hi"}) is None


def test_hermes_dispatch_refuses_bans_without_a_person(hermes_hook, monkeypatch):
    monkeypatch.setenv("TWITCH_CHANNEL", "chan")
    message, _ = dispatch("twitch_ban", {"login": "spammer"}, source="cli")
    assert message and message.startswith("BLOCKED")
    assert dispatch("twitch_timeout", {"login": "spammer", "seconds": 60}, source="cli") == (None, None)


# toolsets with the plugin loaded by Hermes' own loader

_PROBE = textwrap.dedent("""
    import json, os, shutil, sys, tempfile
    home = tempfile.mkdtemp()
    os.environ["HERMES_HOME"] = home
    shutil.copytree(sys.argv[1], os.path.join(home, "plugins", "tallyhand"),
                    ignore=shutil.ignore_patterns(".git", "tests", "__pycache__", ".github"))
    with open(os.path.join(home, "config.yaml"), "w") as fh:
        fh.write("plugins:\\n  enabled:\\n    - tallyhand\\n")
    from hermes_cli.plugins import discover_plugins
    discover_plugins()
    from hermes_cli.tools_config import _get_platform_tools
    cases = json.loads(sys.argv[2])
    out = {}
    for label, config, override in cases:  # the body of gateway/run_turn.py _resolve_enabled_toolsets_for_source
        config = {"mcp_servers": {"filesystem": {"command": "npx", "args": ["fs"]}}, **config}
        if override:
            config["platform_toolsets"] = {**(config.get("platform_toolsets") or {}), "twitch": override}
        out[label] = sorted(_get_platform_tools(config, "twitch"))
    print(json.dumps(out))
""")
CONFIGS = {
    "unset": {},
    "recommended": {"platform_toolsets": {"twitch": ["todo"]}, "known_plugin_toolsets": {"twitch": ["obs", "twitch"]}},
    "own list": {"platform_toolsets": {"twitch": ["web"]}, "known_plugin_toolsets": {"twitch": ["obs", "twitch"]}},
    "known only": {"known_plugin_toolsets": {"twitch": ["obs", "twitch"]}},  # privacy.check warns about this one
}


def test_toolsets_per_role_with_the_plugin_really_loaded(tmp_path, monkeypatch):
    """The toolsets each role's turn is built with, as Hermes resolves them from toolsets_for_source with the plugin
    loaded by its own loader: with known_plugin_toolsets.twitch set, viewers get the to-do list alone and
    moderators the twitch tools but not OBS, while the owner keeps both."""
    from tallyhand.adapter import MODERATOR_TOOLSETS, VIEWER_TOOLSETS, owner_toolsets

    cases = []
    for name, config in CONFIGS.items():
        monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda config=config: config)
        cases += [(f"{name}/owner", config, owner_toolsets()), (f"{name}/moderator", config, list(MODERATOR_TOOLSETS)),
                  (f"{name}/viewer", config, list(VIEWER_TOOLSETS))]
    hermes = os.environ.get("HERMES_AGENT_PATH") or str(ROOT.parent / "hermes-agent")
    env = {**os.environ, "PYTHONPATH": hermes, "TWITCH_BOT_TOKEN": "x"}
    run = subprocess.run([sys.executable, "-c", _PROBE, str(ROOT), json.dumps(cases)], capture_output=True,
                         text=True, env=env, cwd=tmp_path, timeout=180)
    assert run.returncode == 0, run.stderr[-2000:]
    got = json.loads(run.stdout.strip().splitlines()[-1])
    assert {"twitch", "obs", "terminal", "filesystem"} <= set(got["unset/owner"])
    # Without known_plugin_toolsets Hermes adds every plugin toolset to every list: the model sees the twitch and
    # obs tools in a viewer's turn, and only the sandbox hook keeps it from running them.
    assert got["unset/moderator"] == got["unset/viewer"] == ["obs", "todo", "twitch"]
    assert set(got["recommended/owner"]) == {"obs", "todo", "twitch", "filesystem"}  # MCP stays the owner's choice
    assert got["recommended/moderator"] == got["own list/moderator"] == ["todo", "twitch"]
    assert got["recommended/viewer"] == got["own list/viewer"] == ["todo"]
    assert {"obs", "twitch", "web"} <= set(got["own list/owner"]) and "terminal" not in got["own list/owner"]
    assert "twitch" not in got["known only/owner"] and "obs" not in got["known only/owner"]
