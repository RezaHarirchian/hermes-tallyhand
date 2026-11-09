"""Opt-in automatic clips in the owner's channel. Fixed rules decide, with no model call: a burst of chat well above
the channel's usual pace, or several people typing "clip" at once. A clip is made only while the stream is live, and
then the rules rest for five minutes."""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable

from .helix import SCOPE_CLIPS, Helix, HelixError

logger = logging.getLogger(__name__)

RATE_WINDOW = 20.0  # seconds of chat a burst is counted over
BASELINE_WINDOW = 300.0  # the seconds before that window set the channel's usual pace
RATE_FACTOR, RATE_MIN = 3.0, 15  # a burst is at least 3x the usual pace and at least 15 messages
VOTE_WINDOW, VOTE_USERS = 15.0, 3  # or this many people typing "clip" within this many seconds
COOLDOWN = 300.0
OFFLINE_REST = 60.0  # after a check that made no clip (offline, Twitch error), the rules rest this long
_CLIP_RE = re.compile(r"\bclip\b", re.IGNORECASE)
# Not a vote: a chat command such as !clip, which answers itself, and a line with a link, such as another bot's
# "Clip: https://clips.twitch.tv/..." announcement.
_NOT_A_VOTE = re.compile(r"\A\s*!|://")


class ChatBurst:
    """The two clip rules over one channel's chat lines, on an injectable clock, in constant time per line however
    busy chat gets. The pace rule waits until it has seen a full baseline window, so a fresh start never mistakes
    ordinary chat for a burst."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._recent: deque[float] = deque()  # line times inside RATE_WINDOW
        self._earlier: deque[float] = deque()  # line times in the BASELINE_WINDOW before that
        self._votes: deque[tuple[float, str]] = deque()
        self._voters: dict[str, int] = {}  # voter -> their votes in self._votes
        self._since: float | None = None
        self._resting_until = 0.0

    def observe(self, user: str, text: str) -> str | None:
        """Count one chat line from ``user``; "rate" or "votes" when a rule fires, else None."""
        now = self.clock()
        if self._since is None:
            self._since = now
        self._recent.append(now)
        while now - self._recent[0] > RATE_WINDOW:
            self._earlier.append(self._recent.popleft())
        while self._earlier and now - self._earlier[0] > RATE_WINDOW + BASELINE_WINDOW:
            self._earlier.popleft()
        if user and _CLIP_RE.search(text) and not _NOT_A_VOTE.search(text):
            self._votes.append((now, user))
            self._voters[user] = self._voters.get(user, 0) + 1
        while self._votes and now - self._votes[0][0] > VOTE_WINDOW:
            _, voter = self._votes.popleft()
            if (left := self._voters[voter] - 1) > 0:
                self._voters[voter] = left
            else:
                del self._voters[voter]
        if now < self._resting_until:
            return None
        if now - self._since >= RATE_WINDOW + BASELINE_WINDOW:
            recent, usual = len(self._recent), len(self._earlier) * RATE_WINDOW / BASELINE_WINDOW
            if recent >= RATE_MIN and recent >= RATE_FACTOR * usual:
                return "rate"
        if len(self._voters) >= VOTE_USERS:
            return "votes"
        return None

    def rest(self, seconds: float) -> None:
        """No rule fires for ``seconds``; the "clip" votes so far are spent."""
        self._resting_until = self.clock() + seconds
        self._votes.clear()
        self._voters.clear()


class AutoClip:
    """Applies the rules to the owner's channel and makes the clip: only while the stream is live, only with the
    clips:edit scope (without it, one warning and no clips), one clip at a time, then COOLDOWN seconds of rest.
    ``post`` (None to stay quiet) puts the link in chat."""

    def __init__(self, helix: Helix, room_id: Callable[[], str], post: Callable[[str], Awaitable[object]] | None,
                 clock: Callable[[], float] = time.monotonic):
        self.helix, self.room_id, self.post = helix, room_id, post
        self.burst = ChatBurst(clock)
        self._busy = False
        self._warned = False

    def observe(self, user: str, text: str) -> str | None:
        """Count one chat line of the owner's channel. A rule's name when a clip should be tried now: the caller
        then awaits ``clip()`` with it."""
        reason = self.burst.observe(user, text)
        if reason is None or self._busy:
            return None
        self._busy = True
        return reason

    def reset(self) -> None:
        """Clear the in-flight flag (disconnect or a cancelled clip task that never started)."""
        self._busy = False

    def scope_missing(self) -> bool:
        """True when the token's validation lacks clips:edit; the first time, says so in the log."""
        info = self.helix.token_info
        if info is None or SCOPE_CLIPS in info.scopes:
            return False
        if not self._warned:
            self._warned = True
            logger.warning("Twitch: auto_clip is on but the bot token lacks the %s scope, so no clips are made; "
                           "generate a new TWITCH_BOT_TOKEN with %s and restart the gateway", SCOPE_CLIPS, SCOPE_CLIPS)
        return True

    async def clip(self, reason: str) -> str | None:
        """Make the clip if the stream is live; the clip's link, or None."""
        self.burst.rest(OFFLINE_REST)
        try:
            room = self.room_id()
            if not room or self.scope_missing():
                return None
            if await self.helix.stream(room) is None:
                return None
            self.burst.rest(COOLDOWN)
            made = await self.helix.create_clip(room)
        except HelixError as exc:
            if exc.code == "missing_scope":
                self.scope_missing()
            elif exc.code != "not_found":  # not_found: the stream ended a moment ago
                logger.warning("Twitch: auto clip failed: %s", exc.message)
            return None
        finally:
            self._busy = False
        logger.info("Twitch: auto clip (%s): %s", reason, made.url)
        if self.post is not None:
            await self.post(f"Clip: {made.url}")
        return made.url
