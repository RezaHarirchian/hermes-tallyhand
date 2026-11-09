"""Stream event lines: one short, fixed thank-you line in chat for a raid, a sub, a resub or gift subs, with no model
call. Off unless the ``event_messages`` setting is "thanks"."""

from __future__ import annotations

import time
from collections.abc import Callable

from .irc import ChannelEvent
from .triggers import Cooldowns, one_line

OFF, THANKS = "off", "thanks"
MODES = (OFF, THANKS)
COOLDOWN = 30.0  # seconds between two lines for the same kind of event in one channel
NAME_LIMIT = 25
# Every line this module can post. {name} is the raider's, subscriber's or gifter's Twitch display name.
TEMPLATES = {
    "raid": "Welcome, raiders! Thanks for the raid, {name}.",
    "sub": "Thanks for subscribing, {name}!",
    "resub": "Thanks for resubscribing, {name}!",
    "gift": "Thanks for gifting, {name}!",
}
# USERNOTICE msg-ids per template. A community gift arrives as one submysterygift and then one subgift per
# recipient, each carrying the gift's id; only the first is thanked.
KINDS = {"raid": "raid", "sub": "sub", "resub": "resub", "subgift": "gift", "anonsubgift": "gift",
         "submysterygift": "gift", "anonsubmysterygift": "gift"}
ANONYMOUS = "anonymous gifter"
ANONYMOUS_LOGIN = "ananonymousgifter"  # what Twitch puts in an anonymous gift's login, "AnAnonymousGifter" as its name
ANONYMOUS_KINDS = frozenset({"anonsubgift", "anonsubmysterygift"})


def parse_mode(value: object) -> str | None:
    """The mode a setting names, "off" or "thanks" (case and spaces ignored); None for anything else. A boolean
    counts too: ``hermes config set ... event_messages off`` stores false, and YAML reads a bare off or on as one."""
    if isinstance(value, bool):
        return THANKS if value else OFF
    text = str(value).strip().lower() if value is not None else OFF
    return text if text in MODES else None


class EventLines:
    """Picks the line for a channel event, or none: below the raid minimum, inside the cooldown for that kind of
    event in that channel, or a recipient's notice of a community gift already thanked."""

    def __init__(self, min_raid: int = 1, cooldown: float = COOLDOWN, clock: Callable[[], float] = time.monotonic):
        self.min_raid = max(0, int(min_raid))
        self._cooldowns = Cooldowns(cooldown, clock=clock)

    def line(self, event: ChannelEvent) -> str | None:
        kind = KINDS.get(event.kind)
        if kind is None:
            return None
        if kind == "raid" and (event.viewer_count or 0) < self.min_raid:
            return None
        if event.kind == "subgift" and event.params.get("community-gift-id"):
            return None
        name = one_line(event.display_name or event.login)[:NAME_LIMIT].strip()
        if event.kind in ANONYMOUS_KINDS or event.login == ANONYMOUS_LOGIN:
            name = ""
        if not name:
            if kind != "gift":
                return None
            name = ANONYMOUS
        key = (event.channel, kind)
        if not self._cooldowns.ready(key):
            return None
        self._cooldowns.start(key)
        return TEMPLATES[kind].format(name=name)
