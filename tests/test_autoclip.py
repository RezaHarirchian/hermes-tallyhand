"""Auto clips: both rules on a controllable clock, the controller against FakeHelix (live only, clips:edit, cooldown,
announcement), and the feature through a connected adapter in the owner's channel only."""

from __future__ import annotations

import asyncio
import logging

import pytest

from harness import Clock, until
from tallyhand import autoclip, helix
from tallyhand.autoclip import AutoClip, ChatBurst


def steady(burst: ChatBurst, clock: Clock, seconds: float, every: float, user: str = "viewer") -> list[str | None]:
    """One line every ``every`` seconds for ``seconds``, as ``user``; the rules' answers."""
    answers = []
    end = clock.now + seconds
    while clock.now + every <= end:
        clock.now += every
        answers.append(burst.observe(user, "hello there"))
    return answers


def burst_of(burst: ChatBurst, clock: Clock, count: int, within: float = 10.0) -> list[str | None]:
    answers = []
    for index in range(count):
        clock.now += within / count
        answers.append(burst.observe(f"user{index}", "WOW"))
    return answers


# the rules


def test_a_burst_three_times_the_usual_pace_fires():
    clock = Clock(10_000.0)
    burst = ChatBurst(clock)
    assert set(steady(burst, clock, 330, 4.0)) == {None}  # five calm minutes: one line every 4 s, 5 per 20 s
    answers = burst_of(burst, clock, 15)
    assert answers[0] is None and answers[-1] == "rate"


def test_double_the_usual_pace_is_not_a_burst():
    clock = Clock(10_000.0)
    burst = ChatBurst(clock)
    steady(burst, clock, 330, 1.0)  # a busy chat: 20 lines per 20 s
    assert set(burst_of(burst, clock, 40, within=19.0)) == {None}
    answers = burst_of(burst, clock, 25, within=0.5)  # past three times the pace
    assert answers[0] is None and "rate" in answers


def test_a_burst_needs_fifteen_messages():
    clock = Clock(10_000.0)
    burst = ChatBurst(clock)
    steady(burst, clock, 400, 60.0)  # an almost silent chat
    clock.now += autoclip.RATE_WINDOW + 5
    assert set(burst_of(burst, clock, 14)) == {None}
    assert burst.observe("late", "!") == "rate"


def test_no_pace_verdict_before_a_full_baseline():
    clock = Clock(10_000.0)
    burst = ChatBurst(clock)
    assert set(burst_of(burst, clock, 50)) == {None}  # a fresh start has no usual pace to compare with
    clock.now += autoclip.BASELINE_WINDOW + autoclip.RATE_WINDOW
    assert burst_of(burst, clock, 15)[-1] == "rate"


@pytest.mark.parametrize(("texts", "fires"), [
    (["clip", "CLIP IT", "clip that!"], True),
    (["Clip", "omg clip", "clip it"], True),
    (["!clip", "Clip", "omg clip"], False),  # !clip is a command, which answers itself
    (["Clip", "omg clip", "Clip: https://clips.twitch.tv/abc"], False),  # another bot's link
    (["clipping", "clips", "eclipse"], False),
])
def test_three_people_typing_clip(texts, fires):
    clock = Clock(10_000.0)
    burst = ChatBurst(clock)
    answers = []
    for index, text in enumerate(texts):
        clock.now += 4.0
        answers.append(burst.observe(f"user{index}", text))
    assert answers[:2] == [None, None]
    assert answers[2] == ("votes" if fires else None)


def test_the_pace_rule_matches_a_full_scan():
    """The constant-time counters give the same verdicts as counting every line in the windows afresh."""
    import random

    rng, clock = random.Random(7), Clock(10_000.0)
    burst, times, fired = ChatBurst(clock), [], 0
    start = clock.now
    # Calm stretches with bursts of every size in between, on jittered timing.
    steps = [step for _ in range(12) for step in (
        [rng.uniform(1.0, 4.0) for _ in range(rng.randint(60, 140))]
        + [rng.uniform(0.05, 0.4) for _ in range(rng.randint(5, 60))])]
    for step in steps:
        clock.now += step
        times.append(clock.now)
        answer = burst.observe("viewer", "hello")
        burst._resting_until = 0.0
        recent = sum(1 for at in times if clock.now - at <= autoclip.RATE_WINDOW)
        earlier = sum(1 for at in times if autoclip.RATE_WINDOW < clock.now - at
                      <= autoclip.RATE_WINDOW + autoclip.BASELINE_WINDOW)
        usual = earlier * autoclip.RATE_WINDOW / autoclip.BASELINE_WINDOW
        fires = (clock.now - start >= autoclip.RATE_WINDOW + autoclip.BASELINE_WINDOW
                 and recent >= autoclip.RATE_MIN and recent >= autoclip.RATE_FACTOR * usual)
        assert (answer == "rate") is fires
        fired += fires
    assert fired > 0


def test_votes_count_people_not_lines():
    clock = Clock(10_000.0)
    burst = ChatBurst(clock)
    for _ in range(5):
        clock.now += 1.0
        assert burst.observe("same", "clip clip clip") is None
    clock.now += 1.0
    assert burst.observe("other", "clip") is None
    clock.now += 1.0
    assert burst.observe("third", "clip") == "votes"


def test_votes_older_than_fifteen_seconds_lapse():
    clock = Clock(10_000.0)
    burst = ChatBurst(clock)
    burst.observe("a", "clip")
    clock.now += 8.0
    burst.observe("b", "clip")
    clock.now += 8.0  # a's vote is now 16 s old
    assert burst.observe("c", "clip") is None
    clock.now += 1.0
    assert burst.observe("d", "clip") == "votes"


def test_rest_spends_the_votes_and_holds_both_rules():
    clock = Clock(10_000.0)
    burst = ChatBurst(clock)
    steady(burst, clock, 330, 60.0)
    for user in ("a", "b", "c"):
        clock.now += 1
        verdict = burst.observe(user, "clip")
    assert verdict == "votes"
    burst.rest(autoclip.COOLDOWN)
    assert set(burst_of(burst, clock, 30)) == {None}
    for user in ("d", "e", "f"):
        clock.now += 1
        assert burst.observe(user, "clip") is None
    clock.now += autoclip.COOLDOWN
    for user in ("g", "h"):
        clock.now += 1
        burst.observe(user, "clip")
    clock.now += 1
    assert burst.observe("i", "clip") == "votes"


# the controller


@pytest.fixture
def owner(helix_api):
    helix_api.add_user("chan", "2000", "Chan")
    return helix_api


def controller(api, clock: Clock, posted: list[str] | None = None) -> AutoClip:
    client = helix.Helix("goodtoken", base_url=api.url, validate_url=api.validate.url)

    async def post(text: str) -> None:
        posted.append(text)

    return AutoClip(client, lambda: "2000", post if posted is not None else None, clock=clock)


def votes(clip: AutoClip, clock: Clock, start: int = 0) -> str | None:
    verdict = None
    for index in range(start, start + 3):
        clock.now += 1.0
        verdict = clip.observe(f"voter{index}", "clip") or verdict
    return verdict


async def test_a_live_stream_is_clipped_and_the_link_posted(owner):
    clock, posted = Clock(10_000.0), []
    clip = controller(owner, clock, posted)
    owner.go_live("2000")
    assert votes(clip, clock) == "votes"
    assert await clip.clip("votes") == "https://clips.twitch.tv/" + owner.clips[-1]
    assert posted == [f"Clip: https://clips.twitch.tv/{owner.clips[-1]}"]


async def test_no_clip_while_offline(owner):
    clock, posted = Clock(10_000.0), []
    clip = controller(owner, clock, posted)
    assert votes(clip, clock) == "votes"
    assert await clip.clip("votes") is None
    assert (owner.clips, posted, owner.calls("clips")) == ([], [], [])
    assert votes(clip, clock, 3) is None  # resting after the offline check
    clock.now += autoclip.OFFLINE_REST
    assert votes(clip, clock, 6) == "votes"


async def test_five_minutes_between_clips(owner):
    clock = Clock(10_000.0)
    clip = controller(owner, clock)
    owner.go_live("2000")
    assert votes(clip, clock) and await clip.clip("votes")
    clock.now += autoclip.COOLDOWN - 10
    assert votes(clip, clock, 3) is None
    clock.now += 10
    assert votes(clip, clock, 6) == "votes"
    assert await clip.clip("votes")
    assert len(owner.clips) == 2


async def test_announce_off_posts_nothing(owner):
    clock = Clock(10_000.0)
    clip = controller(owner, clock, posted=None)
    owner.go_live("2000")
    assert votes(clip, clock) and await clip.clip("votes")
    assert len(owner.clips) == 1


async def test_one_clip_at_a_time(owner):
    clock = Clock(10_000.0)
    clip = controller(owner, clock)
    assert votes(clip, clock) == "votes"
    clock.now += 1
    clip.burst.rest(0)
    assert votes(clip, clock, 3) is None  # the first attempt still runs


async def test_missing_scope_warns_once_and_makes_no_clip(owner, caplog):
    owner.validate.respond(scopes=["chat:read", "chat:edit"])
    clock = Clock(10_000.0)
    clip = controller(owner, clock)
    owner.go_live("2000")
    await clip.helix.validate()
    with caplog.at_level(logging.WARNING):
        assert clip.scope_missing() and clip.scope_missing()
        for start in (0, 3):
            if votes(clip, clock, start):
                assert await clip.clip("votes") is None
            clock.now += autoclip.COOLDOWN
    assert caplog.text.count("clips:edit scope") == 1
    assert owner.calls("clips") == []


async def test_a_twitch_error_is_logged(owner, caplog):
    clock = Clock(10_000.0)
    clip = controller(owner, clock)
    owner.go_live("2000")
    owner.force(403, times=2, message="Clips are restricted")
    with caplog.at_level(logging.WARNING):
        assert votes(clip, clock) and await clip.clip("votes") is None
    assert "auto clip failed" in caplog.text


# through the adapter


async def test_chat_asking_for_a_clip_in_the_owners_channel(harness, twitch, helix_api):
    twitch.badges["#chan"] = "moderator/1"
    adapter = await harness.connected(channels="#chan,#friend", settings={"auto_clip": True},
                                      _test_helix_url=helix_api.url)
    helix_api.go_live(twitch.room_id("#chan"))
    helix_api.go_live(twitch.room_id("#friend"))
    for index in range(3):
        await twitch.privmsg("#friend", "clip it", login=f"guest{index}", user_id=f"9{index}")
    await asyncio.sleep(0.3)
    assert helix_api.clips == []  # another channel never clips
    for index in range(3):
        await twitch.privmsg("#chan", "CLIP", login=f"fan{index}", user_id=f"8{index}")
    await twitch.wait_for(lambda: twitch.privmsgs(), timeout=5.0)
    assert twitch.privmsgs() == [f"PRIVMSG #chan :Clip: https://clips.twitch.tv/{helix_api.clips[0]}"]
    assert [call.query["broadcaster_id"] for call in helix_api.calls("clips")] == [[twitch.room_id("#chan")]]
    assert adapter._autoclip is not None


async def test_auto_clip_is_off_by_default(harness, twitch, helix_api):
    adapter = await harness.connected(_test_helix_url=helix_api.url)
    helix_api.go_live(twitch.room_id("#chan"))
    for index in range(4):
        await twitch.privmsg("#chan", "clip", login=f"fan{index}", user_id=f"8{index}")
    await until(lambda: len(adapter._tail.lines("#chan")) == 4)
    await asyncio.sleep(0.2)
    assert (adapter._autoclip, helix_api.calls("clips"), twitch.privmsgs()) == (None, [], [])


async def test_the_scope_warning_comes_at_connect(harness, validate, caplog):
    with caplog.at_level(logging.WARNING):
        await harness.connected(settings={"auto_clip": True})
    assert caplog.text.count("lacks the clips:edit scope") == 1
