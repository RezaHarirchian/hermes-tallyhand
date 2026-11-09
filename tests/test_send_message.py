"""Hermes' send_message tool path into a live TwitchAdapter: send() alone chunks and caps the text."""

from __future__ import annotations

import re
import types

import gateway.run
import pytest
from gateway.config import Platform

from harness import until
from tallyhand.adapter import RateLimiter

TEXT = " ".join(f"word{n}" for n in range(600))  # about 4,700 characters
PIECE_RE = re.compile(r"\(\d+/\d+\)")


@pytest.mark.parametrize(("overrides", "pieces"), [({}, False), ({"max_message_length": 480}, True)])
async def test_send_message_tool_respects_max_reply_lines(harness, twitch, monkeypatch, registered_twitch,
                                                          overrides, pieces):
    from tools.send_message_tool import _send_to_platform

    entry = registered_twitch(**overrides)
    adapter = await harness.connected(settings={"max_reply_lines": 3})
    adapter._channel_limiters["#chan"] = RateLimiter(20, 1.0)
    runner = types.SimpleNamespace(adapters={Platform("twitch"): adapter})
    monkeypatch.setattr(gateway.run, "_gateway_runner_ref", lambda: runner)

    result = await _send_to_platform(Platform("twitch"), adapter.config, "#chan", TEXT)
    assert result.get("success"), result
    if pieces:  # the old registration: Hermes pre-split the text and every piece became its own reply
        assert entry.max_message_length == 480
        await until(lambda: len(twitch.privmsgs()) >= 5, timeout=5.0, what="pre-split pieces")
        assert any(PIECE_RE.search(line) for line in twitch.privmsgs())
        return
    assert entry.max_message_length == 0
    await twitch.wait_for(lambda: len(twitch.privmsgs()) == 3)
    lines = twitch.texts()
    assert len(lines) == 3 and lines[-1].endswith("...") and all(len(line) <= 480 for line in lines)
    assert not any(PIECE_RE.search(line) for line in lines)
