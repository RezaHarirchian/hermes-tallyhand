"""The one-shot sender: validate, log in, join, send at Twitch's pace, part and close, against FakeTwitch."""

from __future__ import annotations

import asyncio
import time

import pytest

from harness import TOKEN, closed_port, until
from tallyhand import sender
from tallyhand.sender import Endpoint, SendError


@pytest.fixture
def endpoint(twitch, validate) -> Endpoint:
    return Endpoint(host="127.0.0.1", port=twitch.port, tls=False, validate_url=validate.url)


async def test_a_delivery_from_login_to_close(twitch, endpoint):
    ids = await sender.post(f"oauth:{TOKEN}", "#chan", "Daily recap: 3 raids, 12 subs.", endpoint=endpoint)
    assert ids == twitch.sent_ids and len(ids) == 1
    await until(lambda: "PART #chan" in twitch.received, what="the part")
    assert twitch.received == ["CAP REQ :twitch.tv/commands twitch.tv/tags", f"PASS oauth:{TOKEN}", "NICK tallybot",
                               "JOIN #chan", "PRIVMSG #chan :Daily recap: 3 raids, 12 subs.", "PART #chan"]
    await until(lambda: not twitch.connected, what="the close")


async def test_long_text_is_cut_like_a_reply_and_paced(twitch, endpoint):
    started = time.monotonic()
    ids = await sender.post(TOKEN, "#chan", "word " * 400, endpoint=endpoint)
    assert len(ids) == 3 and time.monotonic() - started >= 2.0  # one line a second: the bot is no moderator here
    lines = twitch.texts()
    assert len(lines) == 3 and lines[-1].endswith("...") and all(len(line) <= 480 for line in lines)


async def test_no_pause_where_the_bot_moderates(twitch, endpoint):
    twitch.badges["#chan"] = "moderator/1"
    started = time.monotonic()
    assert len(await sender.post(TOKEN, "#chan", "word " * 400, max_lines=2, endpoint=endpoint)) == 2
    assert time.monotonic() - started < 1.0


async def test_slow_mode_means_one_line(twitch, endpoint):
    twitch.slow["#chan"] = 30
    assert len(await sender.post(TOKEN, "#chan", "word " * 400, endpoint=endpoint)) == 1
    assert twitch.texts()[0].endswith("...")
    twitch.badges["#chan"] = "vip/1"
    twitch.received.clear()
    assert len(await sender.post(TOKEN, "#chan", "word " * 400, endpoint=endpoint)) == 3


async def test_a_refused_line_says_how_many_went_out(twitch, endpoint):
    twitch.refuse = "msg_ratelimit"
    with pytest.raises(SendError) as caught:
        await sender.post(TOKEN, "#chan", "hello", endpoint=endpoint)
    assert caught.value.message == "Twitch did not deliver the message (msg_ratelimit)"
    assert caught.value.sent == 0


async def test_a_suspended_channel(twitch, endpoint):
    twitch.suspended.add("#gone")
    with pytest.raises(SendError, match="msg_channel_suspended"):
        await sender.post(TOKEN, "#gone", "hello", endpoint=endpoint)
    assert twitch.privmsgs() == []


async def test_a_rejected_login(twitch, endpoint):
    twitch.auth_failure = "Login authentication failed"
    with pytest.raises(SendError, match="rejected the login") as caught:
        await sender.post(TOKEN, "#chan", "hello", endpoint=endpoint)
    assert TOKEN not in caught.value.message and not caught.value.retryable


async def test_an_invalid_token_never_reaches_chat(twitch, validate, endpoint):
    validate.respond(status=401)
    with pytest.raises(SendError, match="invalid or expired") as caught:
        await sender.post(TOKEN, "#chan", "hello", endpoint=endpoint)
    assert not caught.value.retryable and twitch.connections == 0


async def test_unreachable_chat_is_retryable(validate):
    with pytest.raises(SendError, match="could not reach Twitch chat") as caught:
        await sender.post(TOKEN, "#chan", "hello",
                          endpoint=Endpoint(host="127.0.0.1", port=closed_port(), tls=False, validate_url=validate.url))
    assert caught.value.retryable


async def test_silence_times_out(twitch, endpoint, monkeypatch):
    monkeypatch.setattr(sender, "STEP_TIMEOUT", 0.3)

    async def mute(line, writer, state):  # never answers the JOIN
        if not line.startswith("JOIN"):
            await original(line, writer, state)

    original = twitch._on_line
    monkeypatch.setattr(twitch, "_on_line", mute)
    with pytest.raises(SendError, match="did not confirm the join in time") as caught:
        await sender.post(TOKEN, "#chan", "hello", endpoint=endpoint)
    assert caught.value.retryable


async def test_nothing_to_send(twitch, validate, endpoint):
    with pytest.raises(SendError, match="nothing to send"):
        await sender.post(TOKEN, "#chan", " \n ", endpoint=endpoint)
    assert (twitch.connections, validate.requests) == (0, [])


@pytest.mark.parametrize(("target", "allowed"), [
    ("#home", "#home"), ("Home", "#home"), ("#chan", "#chan"), ("friend", "#friend"), ("#elsewhere", ""),
    ("", ""), (None, ""), ("not a channel!", "")])
def test_deliveries_go_only_to_the_home_channel_or_a_joined_one(monkeypatch, target, allowed):
    monkeypatch.setenv("TWITCH_HOME_CHANNEL", "home")
    monkeypatch.setenv("TWITCH_CHANNEL", "chan,#Friend")
    assert sender.allowed_target(target) == allowed


def test_without_a_home_channel_only_joined_channels(monkeypatch):
    monkeypatch.setenv("TWITCH_CHANNEL", "chan")
    assert (sender.allowed_target("#chan"), sender.allowed_target("#home")) == ("#chan", "")


# Hermes' hooks: the standalone sender, target parsing, and cron delivery through Hermes


@pytest.fixture
def pconfig(twitch, validate, monkeypatch):
    """The platform config Hermes hands the sender, pointed at the fakes; the bot serves #chan and #home."""
    from gateway.config import PlatformConfig

    monkeypatch.setenv("TWITCH_BOT_TOKEN", f"oauth:{TOKEN}")
    monkeypatch.setenv("TWITCH_CHANNEL", "chan")
    monkeypatch.setenv("TWITCH_HOME_CHANNEL", "home")
    return PlatformConfig(enabled=True, extra={"channel": "#chan", "_test_host": "127.0.0.1", "_test_port": twitch.port,
                                               "_test_tls": False, "_test_validate_url": validate.url})


async def test_the_standalone_sender_posts_where_the_bot_serves(twitch, pconfig):
    result = await sender.send_standalone(pconfig, "Home", "The stream starts in ten minutes.", thread_id=None,
                                          media_files=[], force_document=False)
    assert result == {"success": True, "message_id": twitch.sent_ids[0]}
    assert twitch.texts() == ["The stream starts in ten minutes."]
    assert "JOIN #home" in twitch.received


@pytest.mark.parametrize("target", ["#elsewhere", "", "not a channel!"])
async def test_the_standalone_sender_refuses_other_channels(twitch, pconfig, target):
    result = await sender.send_standalone(pconfig, target, "hello")
    assert set(result) == {"error"} and "not a channel this bot serves" in result["error"]
    assert twitch.connections == 0


async def test_the_standalone_sender_reports_what_went_out(twitch, pconfig, monkeypatch):
    twitch.refuse = "msg_ratelimit"
    result = await sender.send_standalone(pconfig, "#chan", "hello")
    assert result == {"error": "Twitch did not deliver the message (msg_ratelimit)"}
    monkeypatch.delenv("TWITCH_BOT_TOKEN")
    assert await sender.send_standalone(pconfig, "#chan", "hello") == {"error": "TWITCH_BOT_TOKEN is not set"}


async def test_the_standalone_sender_has_an_overall_deadline(twitch, pconfig, monkeypatch):
    monkeypatch.setattr(sender, "TOTAL_TIMEOUT", 0.3)

    async def mute(line, writer, state):
        if not line.startswith("JOIN"):
            await original(line, writer, state)

    original = twitch._on_line
    monkeypatch.setattr(twitch, "_on_line", mute)
    result = await sender.send_standalone(pconfig, "#chan", "hello")
    assert result == {"error": "Twitch chat did not take the message within 0 s"}


@pytest.mark.parametrize(("ref", "parsed"), [
    ("#chan", ("#chan", None)), ("Chan", ("#chan", None)), (" #home ", ("#home", None)), ("#chan:123", None),
    ("not a channel!", None), ("", None)])
def test_parse_target_ref(ref, parsed):
    assert sender.parse_target_ref(ref) == parsed


def test_validate_target_ref(monkeypatch):
    monkeypatch.setenv("TWITCH_CHANNEL", "chan")
    assert sender.validate_target_ref("#chan") is True
    assert sender.validate_target_ref("#elsewhere") == sender.NOT_SERVED


async def test_cron_delivery_through_hermes(registered_twitch, twitch, pconfig, monkeypatch):
    """Hermes' own cron delivery in a process without the gateway: our target hooks resolve it, our sender posts."""
    from cron.scheduler_delivery import _deliver_result
    from gateway.config import GatewayConfig, Platform

    registered_twitch()
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda *a, **k: GatewayConfig(
        platforms={Platform("twitch"): pconfig}))
    # A process without a gateway: no runner from an earlier test may lend its adapter (gateway/run.py keeps the
    # newest GatewayRunner in _gateway_runner_ref).
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: None)
    job = {"id": "j1", "name": "recap", "deliver": "twitch:#chan"}
    assert await asyncio.to_thread(_deliver_result, job, "Three raids today.", adapters=None, loop=None) is None
    assert any("Three raids today." in line for line in twitch.texts())
    refused = await asyncio.to_thread(_deliver_result, {**job, "deliver": "twitch:#elsewhere"}, "x",
                                      adapters=None, loop=None)
    assert refused and twitch.lines("JOIN #elsewhere") == []


async def test_a_delivery_takes_at_most_twenty_lines(twitch, endpoint):
    twitch.badges["#chan"] = "moderator/1"
    assert len(await sender.post(TOKEN, "#chan", "word " * 4000, max_lines=50, endpoint=endpoint)) == sender.MAX_LINES


async def test_a_delivery_redacts_the_token(twitch, endpoint):
    await sender.post(TOKEN, "#chan", f"the token is `{TOKEN[:4]}`{TOKEN[4:]}", endpoint=endpoint)
    (line,) = twitch.texts()
    assert TOKEN not in line and "[redacted]" in line
