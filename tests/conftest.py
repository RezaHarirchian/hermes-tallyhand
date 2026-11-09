"""Test setup: Hermes on sys.path ($HERMES_AGENT_PATH, else ../hermes-agent next to this repo), this repo importable
as the ``tallyhand`` package, and every Hermes state path sandboxed in a temp dir."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HERMES = Path(os.environ.get("HERMES_AGENT_PATH") or ROOT.parent / "hermes-agent").resolve()
if HERMES.is_dir() and str(HERMES) not in sys.path:
    sys.path.insert(0, str(HERMES))

# Before any Hermes import: logs, status files and machine-wide locks must not touch the real ~/.hermes.
_SANDBOX = Path(tempfile.mkdtemp(prefix="tallyhand-tests-"))
os.environ["HERMES_HOME"] = str(_SANDBOX / "home")
os.environ["HERMES_GATEWAY_LOCK_DIR"] = str(_SANDBOX / "locks")
os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(
    filter(None, ["127.0.0.1", "localhost", os.environ.get("NO_PROXY", "")]))


def _load_package(name: str = "tallyhand"):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load_package()

_ENV = ("TWITCH_BOT_TOKEN", "TWITCH_CHANNEL", "TWITCH_ALLOWED_USERS", "TWITCH_ALLOW_ALL_USERS", "TWITCH_HOME_CHANNEL",
        "TWITCH_HOME_CHANNEL_NAME", "TALLYHAND_TRIGGERS", "TALLYHAND_USER_COOLDOWN", "TALLYHAND_REPLY_LANGUAGE",
        "OBS_WEBSOCKET_URL", "OBS_WEBSOCKET_PASSWORD", "TALLYHAND_OBS_ALLOW_REMOTE", "TALLYHAND_BRB_SCENE",
        "TALLYHAND_LIVE_SCENE", "TALLYHAND_CAPTION_SOURCE")


@pytest.fixture(scope="session", autouse=True)
def twitch_platform():
    """Register ``twitch`` so ``Platform("twitch")`` resolves the way it does once the plugin is loaded."""
    from gateway.config import Platform
    from gateway.platform_registry import PlatformEntry, platform_registry

    from tallyhand.adapter import TwitchAdapter

    platform_registry.register(PlatformEntry(
        name="twitch", label="Twitch", adapter_factory=TwitchAdapter, check_fn=lambda: True, source="builtin"))
    Platform("twitch")
    yield
    platform_registry.unregister("twitch")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """No Twitch, Tallyhand or OBS settings leak in from the developer's shell, no test's plugin settings, chat
    commands, CLI Helix client or viewer notes leak into the next, and an adapter built without ``_test_helix_url``
    points at a closed loopback port rather than at Twitch."""
    from tallyhand import adapter, commands, roles, twitch_tools, viewer_notes

    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(twitch_tools, "_get_config", None)
    monkeypatch.setattr(twitch_tools, "_local_client", None)
    monkeypatch.setattr(roles, "_surface", None)
    monkeypatch.setattr(commands, "_registry", dict(commands._registry))
    monkeypatch.setattr(adapter, "HELIX_URL", "http://127.0.0.1:9/helix")
    notes_file = tmp_path / "notes" / viewer_notes.FILE_NAME
    monkeypatch.setattr(viewer_notes, "default_path", lambda: notes_file)


class _Capture:
    """Just enough plugin context for register(): keeps the register_platform keywords."""

    kwargs: dict = {}

    def register_platform(self, **kwargs) -> None:
        self.kwargs = kwargs

    def register_tool(self, **kwargs) -> None:
        pass

    def register_hook(self, name, callback) -> None:
        pass


@pytest.fixture
def registered_twitch():
    """Install the plugin's real PlatformEntry (with overrides) for one test, then restore the session one."""
    from gateway.platform_registry import PlatformEntry, platform_registry

    import tallyhand

    ctx = _Capture()
    tallyhand.register(ctx)
    original = platform_registry.get("twitch")

    def install(**overrides) -> PlatformEntry:
        entry = PlatformEntry(source="builtin", plugin_name="tallyhand", **{**ctx.kwargs, **overrides})
        platform_registry.register(entry)
        return entry

    yield install
    platform_registry.register(original)


@pytest.fixture
async def twitch():
    from fake_twitch import FakeTwitch

    fake = await FakeTwitch().start()
    yield fake
    await fake.stop()


@pytest.fixture
def validate():
    from fake_validate import FakeValidate

    fake = FakeValidate().start()
    yield fake
    fake.stop()


@pytest.fixture
def helix_api(validate):
    """Fake Helix for the fake validate endpoint's token; the token carries every scope Tallyhand uses."""
    from fake_helix import ALL_SCOPES, FakeHelix

    validate.respond(scopes=ALL_SCOPES)
    fake = FakeHelix(validate).start()
    yield fake
    fake.stop()


@pytest.fixture
async def harness(twitch, validate):
    from harness import Harness

    built = Harness(twitch, validate)
    yield built
    await built.close()


@pytest.fixture
async def chat(harness, twitch):
    """A connected adapter in this process: #chan's broadcaster is the owner, mo moderates #chan, alice watches."""
    from harness import MOD_ID, VIEWER_ID, until

    adapter = await harness.connected()
    room = twitch.room_id("#chan")
    await twitch.privmsg("#chan", "hi", login="mo", user_id=MOD_ID, mod=True, badges="moderator/1")
    await twitch.privmsg("#chan", "hello", login="alice", user_id=VIEWER_ID)
    await until(lambda: adapter.roles.role_of(room, MOD_ID) and adapter.roles.role_of(room, VIEWER_ID), what="roles")
    adapter.room = room
    return adapter
