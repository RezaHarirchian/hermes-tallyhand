"""Stream event lines: the templates, the raid minimum, community gifts, the cooldown per kind of event, the setting,
and the lines through a connected adapter."""

from __future__ import annotations

import asyncio
import logging

import pytest

from harness import Clock, collect_events, until
from tallyhand import events, irc
from tallyhand.events import TEMPLATES, EventLines


def event(kind: str, *, channel: str = "#chan", name: str = "Raider", login: str = "raider",
          params: dict | None = None) -> irc.ChannelEvent:
    return irc.ChannelEvent(kind=kind, channel=channel, login=login, display_name=name, params=dict(params or {}))


@pytest.mark.parametrize(("kind", "params", "line"), [
    ("raid", {"viewerCount": "12"}, "Welcome, raiders! Thanks for the raid, Raider."),
    ("sub", {}, "Thanks for subscribing, Raider!"),
    ("resub", {"cumulative-months": "7"}, "Thanks for resubscribing, Raider!"),
    ("subgift", {"recipient-display-name": "Lucky"}, "Thanks for gifting, Raider!"),
    ("submysterygift", {"mass-gift-count": "20"}, "Thanks for gifting, Raider!"),
    ("anonsubgift", {}, "Thanks for gifting, anonymous gifter!"),
    ("anonsubmysterygift", {}, "Thanks for gifting, anonymous gifter!"),
])
def test_each_kind_gets_its_template(kind, params, line):
    assert EventLines().line(event(kind, params=params)) == line


def test_templates_live_in_one_constant():
    assert set(TEMPLATES) == set(events.KINDS.values()) == {"raid", "sub", "resub", "gift"}
    assert set(events.KINDS) == irc.EVENT_KINDS  # irc parses exactly the kinds events can thank
    assert all(len(text.format(name="x" * 25)) < 100 for text in TEMPLATES.values())
    assert all(not text.startswith(("/", ".", "!")) for text in TEMPLATES.values())


@pytest.mark.parametrize(("viewers", "minimum", "thanked"), [
    ("5", 5, True), ("4", 5, False), ("1", 1, True), ("", 1, False), ("lots", 1, False), ("0", 0, True)])
def test_raid_minimum(viewers, minimum, thanked):
    assert (EventLines(min_raid=minimum).line(event("raid", params={"viewerCount": viewers})) is not None) is thanked


def test_a_community_gifts_recipient_notices_are_not_thanked_again():
    lines = EventLines()
    assert lines.line(event("submysterygift", params={"mass-gift-count": "5", "community-gift-id": "77"}))
    assert lines.line(event("subgift", params={"community-gift-id": "77"})) is None
    assert EventLines().line(event("subgift", params={"community-gift-id": "77"})) is None


@pytest.mark.parametrize(("kind", "name", "login"), [
    ("anonsubgift", "AnAnonymousGifter", "ananonymousgifter"),
    ("anonsubmysterygift", "AnAnonymousGifter", "ananonymousgifter"),
    ("subgift", "AnAnonymousGifter", "ananonymousgifter")])  # a gifter who hid their name
def test_twitchs_anonymous_gifter_is_thanked_as_one(kind, name, login):
    assert EventLines().line(event(kind, name=name, login=login)) == "Thanks for gifting, anonymous gifter!"


def test_an_anonymous_gift_without_a_name():
    assert EventLines().line(event("anonsubgift", name="", login="")) == "Thanks for gifting, anonymous gifter!"
    assert EventLines().line(event("sub", name="", login="")) is None


def test_names_are_one_short_line():
    line = EventLines().line(event("sub", name="Bad\nName\u202e" + "x" * 40))
    assert line == f"Thanks for subscribing, Bad Name{'x' * 17}!"


def test_cooldown_per_kind_of_event_and_channel():
    clock = Clock()
    lines = EventLines(clock=clock)
    assert lines.line(event("sub"))
    assert lines.line(event("sub", name="Other")) is None  # same kind, same channel, inside the cooldown
    assert lines.line(event("resub"))  # another kind has its own cooldown
    assert lines.line(event("subgift"))
    assert lines.line(event("submysterygift")) is None  # every gift kind shares one
    assert lines.line(event("sub", channel="#other"))  # another channel has its own
    clock.now += events.COOLDOWN
    assert lines.line(event("sub", name="Later")) == "Thanks for subscribing, Later!"


def test_other_kinds_get_nothing():
    assert EventLines().line(event("announcement")) is None


@pytest.mark.parametrize(("value", "mode"), [
    (None, "off"), ("off", "off"), ("thanks", "thanks"), (" Thanks ", "thanks"), ("on", None), (True, "thanks"),
    (False, "off"), (1, None)])
def test_parse_mode(value, mode):
    assert events.parse_mode(value) == mode


# through the adapter


async def lines_after(twitch, count: int, timeout: float = 3.0) -> list[str]:
    await twitch.wait_for(lambda: len(twitch.privmsgs()) >= count, timeout=timeout)
    await asyncio.sleep(0.1)
    return twitch.texts()


async def test_thanks_in_chat(harness, twitch):
    twitch.badges["#chan"] = "moderator/1"
    await harness.connected(settings={"event_messages": "thanks", "min_raid_size": 10})
    await twitch.usernotice("#chan", "raid", login="small", params={"viewerCount": "3"})
    await twitch.usernotice("#chan", "raid", login="big", params={"viewerCount": "40"})
    await twitch.usernotice("#chan", "sub", login="newsub")
    await twitch.usernotice("#chan", "sub", login="secondsub")
    assert await lines_after(twitch, 2) == ["Welcome, raiders! Thanks for the raid, Big.",
                                            "Thanks for subscribing, Newsub!"]
    assert all(line.startswith("PRIVMSG #chan :") for line in twitch.privmsgs())  # no reply tag


async def test_off_by_default(harness, twitch):
    events = collect_events(await harness.connected())
    await twitch.usernotice("#chan", "raid", params={"viewerCount": "400"})
    await twitch.usernotice("#chan", "sub")
    await until(lambda: len(events) == 2)
    await asyncio.sleep(0.2)
    assert twitch.privmsgs() == []


async def test_an_unknown_mode_stays_off(harness, twitch, caplog):
    with caplog.at_level(logging.WARNING):
        adapter = await harness.connected(settings={"event_messages": "loud"})
    assert "event_messages='loud'" in caplog.text
    events = collect_events(adapter)
    await twitch.usernotice("#chan", "sub")
    await until(lambda: len(events) == 1)
    await asyncio.sleep(0.2)
    assert twitch.privmsgs() == []
