"""Shared test helpers: a TwitchAdapter wired to FakeTwitch and FakeValidate with Hermes' handle_message replaced by
a recorder, a fake clock, polling, Hermes' session binding and a recording plugin context."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import socket
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageEvent, ProcessingOutcome
from hermes_cli.plugins import PluginContext

from tallyhand.adapter import TwitchAdapter

if TYPE_CHECKING:
    from fake_twitch import FakeTwitch
    from fake_validate import FakeValidate

TOKEN = "goodtoken"
MOD_ID, VIEWER_ID = "47", "42"
PRIVACY_LIMIT = 300  # the !privacy answer stays one short chat line


class Clock:
    """A settable clock: tests move ``now`` by hand."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def closed_port() -> int:
    """A loopback port nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def until(predicate: Callable[[], object], timeout: float = 10.0,
                what: str | Callable[[], str] = "condition") -> None:
    """Poll ``predicate`` until it is truthy or fail after ``timeout`` seconds; a callable ``what`` is read at the
    timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {what() if callable(what) else what}")
        await asyncio.sleep(0.005)


@contextlib.contextmanager
def session(**values) -> Iterator[None]:
    """Hermes' own session context for one tool call or hook, as the gateway or the CLI binds it."""
    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(**values)
    try:
        yield
    finally:
        clear_session_vars(tokens)


def twitch_turn(user_id: str, channel: str = "#chan") -> dict:
    return {"platform": "twitch", "chat_id": channel, "user_id": user_id}


def bound(method: Callable, *args: Any, **kwargs: Any) -> dict:
    """The call as Hermes' own signature binds it, ``**entry_kwargs`` flattened; a call Hermes rejects raises."""
    arguments = inspect.signature(method).bind(None, *args, **kwargs).arguments
    del arguments["self"]
    extra = arguments.pop("entry_kwargs", {})
    return {**arguments, **extra}


class RecordingContext:
    """Stands in for Hermes' PluginContext: records registrations and serves settings from a dict."""

    surface = "cli"

    def __init__(self, settings: dict | None = None):
        self.settings = dict(settings or {})
        self.platforms: list[dict] = []
        self.tools: list[dict] = []
        self.hooks: list[dict] = []
        self.redactions: list[dict] = []
        self.reads: list[str] = []

    def register_platform(self, *args: Any, **kwargs: Any) -> None:
        self.platforms.append(bound(PluginContext.register_platform, *args, **kwargs))

    def register_tool(self, *args: Any, **kwargs: Any) -> None:
        self.tools.append(bound(PluginContext.register_tool, *args, **kwargs))

    def register_hook(self, *args: Any, **kwargs: Any) -> None:
        self.hooks.append(bound(PluginContext.register_hook, *args, **kwargs))

    def register_redaction_patterns(self, *args: Any, **kwargs: Any) -> int:
        self.redactions.append(bound(PluginContext.register_redaction_patterns, *args, **kwargs))
        return len(self.redactions[-1]["patterns"])

    def get_config(self, key: str, default: Any = None) -> Any:
        self.reads.append(key)
        return self.settings.get(key, default)

    def session_surface(self) -> str:
        return self.surface


def collect_events(adapter: TwitchAdapter) -> list:
    """The raids, subs and gifts the adapter handles, which it keeps nowhere itself."""
    seen, handle = [], adapter._on_channel_event

    def record(event):
        seen.append(event)
        handle(event)

    adapter._on_channel_event = record
    return seen


class Recorder:
    """Stands in for ``BasePlatformAdapter.handle_message``: records each event, then plays the processing hooks
    from a separate task the way the base class does. With ``hold=True`` a turn stays open until ``finish()``."""

    def __init__(self, adapter: TwitchAdapter, *, hold: bool = False):
        self.adapter, self.hold = adapter, hold
        self.events: list[MessageEvent] = []
        self.open: list[MessageEvent] = []
        self._tasks: set[asyncio.Task] = set()

    async def __call__(self, event: MessageEvent) -> None:
        self.events.append(event)
        task = asyncio.get_running_loop().create_task(self._process(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process(self, event: MessageEvent) -> None:
        await self.adapter.on_processing_start(event)
        if self.hold:
            self.open.append(event)
        else:
            await self.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    async def finish(self) -> MessageEvent:
        """Complete the oldest open turn from its own short-lived task, as Hermes' processing task does."""
        event = self.open.pop(0)
        await asyncio.get_running_loop().create_task(
            self.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS))
        return event


class Harness:
    """Builds adapters pointed at the fakes and disconnects them all at teardown."""

    def __init__(self, twitch: FakeTwitch, validate: FakeValidate):
        self.twitch, self.validate = twitch, validate
        self.adapters: list[TwitchAdapter] = []

    def adapter(self, *, channels: str = "#chan", token: str = f"oauth:{TOKEN}", settings: dict | None = None,
                hold: bool = False, record: bool = True, **extra) -> TwitchAdapter:
        """``record=False`` keeps Hermes' own handle_message, for tests that wire a real GatewayRunner."""
        config = PlatformConfig(enabled=True, extra={
            "token": token, "channel": channels, "_test_host": "127.0.0.1", "_test_port": self.twitch.port,
            "_test_tls": False, "_test_validate_url": self.validate.url, **extra})
        # The tail is off by default in production; tests keep 12 lines unless they pass their own settings.
        adapter = TwitchAdapter(config, settings={"chat_tail": 12} if settings is None else settings)
        adapter.backoff_base, adapter.backoff_cap = 0.01, 0.05
        if record:
            adapter.recorder = Recorder(adapter, hold=hold)
            adapter.handle_message = adapter.recorder
            adapter.set_authorization_check(lambda user_id, chat_type=None, chat_id=None, **kwargs: True)
        self.adapters.append(adapter)
        return adapter

    async def connected(self, **kwargs) -> TwitchAdapter:
        """A connected adapter that has seen ROOMSTATE for every channel it joined."""
        adapter = self.adapter(**kwargs)
        assert await adapter.connect(), adapter.fatal_error_message
        await until(lambda: len(adapter._room_ids) >= len(adapter.channels), what="ROOMSTATE")
        return adapter

    async def close(self) -> None:
        for adapter in self.adapters:
            await adapter.disconnect()
