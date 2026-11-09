"""caption_answers: a turn's final reply in the owner's channel also goes to the OBS caption, without ever delaying or
failing the chat send; nothing else is captioned."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from fake_obs import FakeObs
from harness import closed_port, until
from tallyhand import obs, obs_tools

FINAL = {"notify": True}  # what Hermes passes with a turn's final reply


class ManualTimer:
    """threading.Timer stand-in that never fires on its own."""

    def __init__(self, interval, function, args=(), kwargs=None):
        self.interval, self.args = interval, args
        self.daemon = False

    def start(self) -> None:
        pass

    def cancel(self) -> None:
        pass


@pytest.fixture(autouse=True)
def timers(monkeypatch):
    monkeypatch.setattr(obs_tools, "CAPTION", obs_tools.CaptionTimer(ManualTimer))


@pytest.fixture
async def fake(monkeypatch):
    server = await FakeObs().start()
    monkeypatch.setenv(obs.URL_ENV, server.url)
    yield server
    await server.stop()


@pytest.fixture
async def caster(harness, twitch):
    twitch.badges["#chan"] = "moderator/1"
    return await harness.connected(channels="#chan,#friend", settings={"caption_answers": True})


async def test_the_final_reply_in_the_owners_channel_is_captioned(caster, twitch, fake):
    result = await caster.send("#chan", "**Celeste** is the game. It came out in 2018.", metadata=FINAL)
    assert result.success
    await until(lambda: fake.caption() == "Celeste is the game. It came out in 2018.", what="the caption")
    assert twitch.privmsgs() == ["PRIVMSG #chan :Celeste is the game. It came out in 2018."]


async def test_a_long_reply_is_cut_for_the_caption(caster, twitch, fake):
    await caster.send("#chan", "word " * 200, metadata=FINAL)
    await until(lambda: fake.caption(), what="the caption")
    assert len(fake.caption()) <= obs_tools.CAPTION_LIMIT and fake.caption().endswith("...")


@pytest.mark.parametrize(("channel", "metadata"), [
    ("#chan", None), ("#chan", {}), ("#chan", {"notify": False}), ("#chan", {"thread_id": "x"}),
    ("#friend", FINAL)])
async def test_nothing_else_is_captioned(caster, twitch, fake, channel, metadata):
    assert (await caster.send(channel, "not an answer for the stream", metadata=metadata)).success
    await asyncio.sleep(0.3)
    assert fake.connections == 0 and fake.caption() is None


async def test_command_replies_and_redeliveries_are_not_captioned(caster, twitch, fake):
    await twitch.privmsg("#chan", "!bot", login="alice", user_id="42")
    await twitch.wait_for(lambda: twitch.privmsgs(), timeout=5.0)
    await caster.send("#chan", "\u267b\ufe0f Recovered reply\nthe answer again", metadata=FINAL)
    await asyncio.sleep(0.3)
    assert fake.connections == 0


@pytest.mark.parametrize(("text", "captioned"), [
    ("!hermes what game is this?", True), ("!hermes /new", False), ("!hermes approve", False),
    ("!hermes Approve session", False), ("!hermes no", False), ("!hermes always", False)])
async def test_the_result_of_an_owners_command_or_answer_is_not_captioned(caster, twitch, fake, text, captioned):
    """Hermes answers a confirmation ("approve" after /new asks) as a turn's reply: a session reset with the
    model and provider in it, which is not for the stream."""
    sent = await twitch.privmsg("#chan", text, login="chan", user_id=twitch.room_id("#chan"), badges="broadcaster/1")
    await until(lambda: caster.recorder.events, what="the owner's message")
    assert (await caster.send("#chan", "Session reset! Model: m", reply_to=sent, metadata=FINAL)).success
    await asyncio.sleep(0.3)
    assert (fake.caption() == "Session reset! Model: m") is captioned


async def test_off_by_default(harness, twitch, fake):
    adapter = await harness.connected()
    await adapter.send("#chan", "an answer", metadata=FINAL)
    await asyncio.sleep(0.3)
    assert fake.connections == 0


async def test_needs_obs_to_be_set_up(caster, twitch, monkeypatch):
    mirrored = []

    async def mirror(text):
        mirrored.append(text)

    monkeypatch.setattr(obs_tools, "mirror", mirror)
    assert not obs.configured()  # neither OBS_WEBSOCKET_URL nor OBS_WEBSOCKET_PASSWORD is set
    assert (await caster.send("#chan", "an answer", metadata=FINAL)).success
    monkeypatch.setenv(obs.PASSWORD_ENV, "secret")
    assert (await caster.send("#chan", "another answer", metadata=FINAL)).success
    await until(lambda: mirrored, what="the caption")
    assert mirrored == ["another answer"]


async def test_obs_down_never_fails_the_chat_send(caster, twitch, monkeypatch, caplog):
    monkeypatch.setenv(obs.URL_ENV, f"ws://127.0.0.1:{closed_port()}")
    with caplog.at_level(logging.INFO):
        assert (await caster.send("#chan", "an answer", metadata=FINAL)).success
        await until(lambda: "answer caption not shown" in caplog.text, what="the log line")
    assert twitch.privmsgs() == ["PRIVMSG #chan :an answer"]
    assert "background task failed" not in caplog.text


async def test_a_stuck_obs_never_delays_the_chat_send(caster, twitch, fake):
    fake.handshake = "silent"  # takes the connection and never answers the WebSocket upgrade
    started = time.monotonic()
    assert (await caster.send("#chan", "an answer", metadata=FINAL)).success
    assert time.monotonic() - started < 0.5
    await until(lambda: fake.connections == 1, what="the caption's connection")
    assert twitch.privmsgs() == ["PRIVMSG #chan :an answer"]
