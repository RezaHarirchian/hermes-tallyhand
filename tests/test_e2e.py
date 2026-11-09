"""One full session: validate, connect, join, answer a question in chunks, survive RECONNECT, answer again."""

from __future__ import annotations

from harness import TOKEN, until


async def test_full_session(harness, twitch, validate):
    adapter = harness.adapter(channels="#streamer,#friend", settings={"max_reply_lines": 3, "chat_tail": 12})
    assert await adapter.connect()
    assert validate.requests == [f"OAuth {TOKEN}"]
    await twitch.wait_for(lambda: twitch.lines("JOIN") == ["JOIN #streamer,#friend"])
    await until(lambda: len(adapter._room_ids) == 2, what="ROOMSTATE")

    await twitch.privmsg("#streamer", "this boss is hard", login="bob", user_id="43", display_name="Bob")
    asked = await twitch.privmsg("#streamer", "!hermes how do I beat the boss?", login="alice", user_id="42",
                                 display_name="Alice")
    await until(lambda: adapter.recorder.events, what="dispatch")
    event = adapter.recorder.events[0]
    assert (event.text, event.source.chat_id, event.source.user_id, event.message_id) == (
        "how do I beat the boss?", "#streamer", "42", asked)
    assert "Bob: this boss is hard" in event.channel_context

    answer = "**Short version:**\n- dodge left\n- hit the core\n\n" + " ".join(["Then keep the pressure on."] * 80)
    result = await adapter.send(event.source.chat_id, answer, reply_to=event.message_id)
    assert result.success
    await twitch.wait_for(lambda: len(twitch.privmsgs()) == 3, timeout=5.0)
    lines = twitch.privmsgs()
    assert lines[0].startswith(f"@reply-parent-msg-id={asked} PRIVMSG #streamer :Short version: dodge left / hit")
    assert all(line.startswith("PRIVMSG #streamer :") for line in lines[1:]) and lines[2].endswith("...")
    assert all(len(text) <= 480 for text in twitch.texts())

    await twitch.reconnect()
    await twitch.wait_for(lambda: len(twitch.lines("JOIN")) == 2, timeout=5.0)
    assert twitch.lines("JOIN")[1] == "JOIN #streamer,#friend" and twitch.connections == 2
    assert twitch.lines("PASS") == [f"PASS oauth:{TOKEN}"] * 2
    await until(lambda: adapter.is_connected, what="reconnected")

    await twitch.privmsg("#friend", "!h still with us?", login="carol", user_id="44")
    await until(lambda: len(adapter.recorder.events) == 2, what="dispatch after reconnect")
    assert adapter.recorder.events[1].source.chat_id == "#friend"
    assert (await adapter.send("#friend", "Yes.")).success
    await twitch.wait_for(lambda: twitch.privmsgs()[-1] == "PRIVMSG #friend :Yes.")
