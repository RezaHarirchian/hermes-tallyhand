"""Viewer roles: flags from Twitch chat tags and the bounded per-room registry."""

from __future__ import annotations

import pytest

from harness import session, until
from tallyhand.roles import MAX_ENTRIES, Role, RoleRegistry, role_from_tags

ROOM, OTHER_ROOM, VIEWER = "1000", "2000", "42"


def registry(max_entries: int = MAX_ENTRIES) -> RoleRegistry:
    return RoleRegistry(max_entries=max_entries)


def stored(roles: RoleRegistry, users: str) -> list[str]:
    return [user for user in users if roles.role_of(ROOM, user) is not None]


@pytest.mark.parametrize(("room_id", "user_id", "badges", "broadcaster"), [
    (ROOM, ROOM, {}, True),
    (ROOM, VIEWER, {"broadcaster": "1"}, True),
    ("", VIEWER, {"broadcaster": "1"}, True),
    (ROOM, VIEWER, {"moderator": "1", "vip": "1"}, False),
    (ROOM, "", {}, False),
    ("", "", {}, False),
])
def test_broadcaster(room_id, user_id, badges, broadcaster):
    assert role_from_tags(room_id, user_id, {}, badges).broadcaster is broadcaster


@pytest.mark.parametrize(("tags", "badges", "moderator"), [
    ({}, {"moderator": "1"}, True),
    ({"mod": "1"}, {}, True),
    ({"mod": "0"}, {}, False),
    ({}, {}, False),
])
def test_moderator_flag(tags, badges, moderator):
    assert role_from_tags(ROOM, VIEWER, tags, badges).moderator is moderator


@pytest.mark.parametrize(("user_id", "tags", "badges", "privileged"), [
    (ROOM, {}, {}, True),
    (VIEWER, {}, {"broadcaster": "1"}, True),
    (VIEWER, {"mod": "1"}, {}, True),
    (VIEWER, {}, {"moderator": "1"}, True),
    (VIEWER, {"vip": "1", "subscriber": "1"}, {"vip": "1", "subscriber": "12", "founder": "0"}, False),
    (VIEWER, {}, {}, False),
])
def test_privileged(user_id, tags, badges, privileged):
    assert role_from_tags(ROOM, user_id, tags, badges).privileged is privileged


def test_role_from_tags_full():
    role = role_from_tags(ROOM, ROOM, {"mod": "0", "subscriber": "1"}, {"broadcaster": "1", "subscriber": "0"})
    assert role == Role(broadcaster=True)
    assert role_from_tags(ROOM, VIEWER, {}, {}) == Role()


def test_registry_lookup():
    roles = registry()
    role = roles.observe(ROOM, VIEWER, {"mod": "1"}, {})
    roles.observe(OTHER_ROOM, VIEWER, {}, {})
    roles.observe(ROOM, ROOM, {}, {"broadcaster": "1"})
    assert roles.role_of(ROOM, VIEWER) is role and role.moderator and len(roles) == 3
    assert roles.is_privileged(ROOM, VIEWER) and roles.is_privileged(ROOM, ROOM)
    assert not roles.is_privileged(OTHER_ROOM, VIEWER) and not roles.is_privileged("3000", VIEWER)
    assert roles.role_of("3000", VIEWER) is None


def test_registry_refreshes_role_on_every_line():
    roles = registry()
    roles.observe(ROOM, VIEWER, {"mod": "1"}, {"moderator": "1"})
    assert roles.is_privileged(ROOM, VIEWER)
    roles.observe(ROOM, VIEWER, {"mod": "0"}, {})
    assert not roles.is_privileged(ROOM, VIEWER) and len(roles) == 1


def test_registry_keeps_newest_entries():
    roles = registry(max_entries=3)
    for user in "12345":
        roles.observe(ROOM, user, {}, {})
    assert len(roles) == 3 and stored(roles, "12345") == ["3", "4", "5"]


def test_registry_reobserve_moves_entry_to_newest():
    roles = registry(max_entries=3)
    for user in "1231":
        roles.observe(ROOM, user, {}, {})
    roles.observe(ROOM, "4", {}, {})
    assert len(roles) == 3 and stored(roles, "1234") == ["1", "3", "4"]


def test_registry_default_bound():
    roles = RoleRegistry()
    for user in range(MAX_ENTRIES + 1):
        roles.observe(ROOM, str(user), {}, {})
    assert len(roles) == MAX_ENTRIES
    assert roles.role_of(ROOM, "0") is None and roles.role_of(ROOM, str(MAX_ENTRIES)) is not None


@pytest.mark.parametrize(("room_id", "user_id"), [("", VIEWER), (ROOM, ""), ("", "")])
def test_registry_skips_missing_ids(room_id, user_id):
    roles = registry()
    assert roles.observe(room_id, user_id, {"mod": "1"}, {}).moderator
    assert len(roles) == 0 and roles.role_of(room_id, user_id) is None
    assert not roles.is_privileged(room_id, user_id)


# the running adapter as tools and hooks see it


async def test_live_view_follows_the_connected_adapter(harness, twitch):
    from tallyhand import roles

    first = harness.adapter()
    assert roles.live() is None and roles.owner_channel() == ""
    assert await first.connect()
    assert roles.live() is first
    await twitch.wait_for(lambda: first.owner_id())
    assert (roles.live().owner_id(), roles.owner_channel()) == (twitch.room_id("#chan"), "chan")
    second = await harness.connected(channels="#other")
    assert roles.live() is second
    await first.disconnect()  # an older adapter leaving does not unhook the newer one
    assert roles.live() is second
    await second.disconnect()
    assert roles.live() is None


async def test_level_of(harness, twitch):
    from tallyhand import roles

    adapter = await harness.connected(channels="#chan,#friend")
    room, friend = twitch.room_id("#chan"), twitch.room_id("#friend")
    await twitch.privmsg("#chan", "hi", login="mo", user_id="47", mod=True, badges="moderator/1")
    await twitch.privmsg("#friend", "hi", login="friend", user_id=friend, badges="broadcaster/1")
    await twitch.privmsg("#chan", "hi", login="vip", user_id="48", vip=True, badges="vip/1")
    await until(lambda: len(adapter.roles) == 3, what="roles")
    assert roles.level_of(room, "#friend") == roles.level_of(room, "chan") == roles.OWNER
    assert (roles.level_of("47", "#chan"), roles.level_of("47", "friend")) == (roles.MODERATOR, roles.VIEWER)
    assert (roles.level_of(friend, "friend"), roles.level_of(friend, "chan")) == (roles.MODERATOR, roles.VIEWER)
    assert roles.level_of("48", "chan") == roles.VIEWER  # VIP is not a moderator
    assert roles.level_of("47", "#notjoined") == roles.level_of("", "chan") == roles.level_of(None, "chan") == "viewer"
    await adapter.disconnect()
    assert roles.level_of(room, "chan") == roles.VIEWER


@pytest.mark.parametrize(("values", "platform", "local", "twitch"), [
    ({"platform": "twitch", "chat_id": "#chan", "user_id": "42"}, "twitch", False, True),
    ({"source": "cli"}, "cli", True, False),
    ({"source": "tui"}, "tui", True, False),
    ({"source": "desktop"}, "desktop", True, False),
    ({"platform": "Local"}, "local", True, False),
    ({"platform": "telegram", "source": "cli"}, "telegram", False, False),  # the gateway's binding wins
    ({}, "cli", True, False),  # the classic CLI binds neither
    ({"source": "oneshot"}, "oneshot", True, False),
    ({"source": "kanban"}, "kanban", False, False),
    ({"platform": "api_server"}, "api_server", False, False),
    ({"cron_session": "1"}, "cron", False, False),
    ({"platform": "twitch", "cron_session": "1"}, "cron", False, False),
])
def test_caller_from_the_session(values, platform, local, twitch):
    from tallyhand import roles

    with session(**values):
        who = roles.caller()
    assert (who.platform, who.local, who.twitch) == (platform, local, twitch)
    assert who.level() == (roles.OWNER if local else roles.VIEWER)  # no adapter runs: Twitch askers are nobody
    assert who.user_id == values.get("user_id", "") and who.chat_id == values.get("chat_id", "")


def test_rank():
    from tallyhand import roles

    assert [roles.rank(level) for level in ("viewer", "moderator", "owner", "admin")] == [0, 1, 2, -1]


@pytest.mark.parametrize(("surface", "local", "twitch"), [
    ("cli", True, False), ("tui", True, False), ("desktop", True, False), ("acp", True, False),
    ("a2a", False, False), ("bot_room", False, False), ("twitch", False, True), ("telegram", False, False)])
def test_caller_trusts_the_session_surface_over_the_local_route(monkeypatch, surface, local, twitch):
    from tallyhand import roles

    monkeypatch.setattr(roles, "_surface", lambda: surface)
    with session(platform="local"):  # every local route binds this, peers' too
        who = roles.caller()
    assert (who.platform, who.local, who.twitch) == (surface, local, twitch)


def test_an_empty_surface_falls_back_to_the_session_env(monkeypatch):
    from tallyhand import roles

    monkeypatch.setattr(roles, "_surface", lambda: "")
    with session(source="desktop"):  # the dashboard's in-process chat
        assert roles.caller().platform == "desktop"


def test_bind_keeps_the_contexts_session_surface():
    from tallyhand import roles

    class Context:
        def session_surface(self):
            return "tui"

    roles.bind(Context())
    assert roles._surface() == "tui"
    roles.bind(object())
    assert roles._surface is None

