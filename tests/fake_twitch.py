"""Local stand-in for Twitch chat (irc.chat.twitch.tv): plain TCP on 127.0.0.1, enough of the protocol for the
adapter tests. Every line a client sends is kept in ``received``."""

from __future__ import annotations

import asyncio
import itertools
import uuid
from collections.abc import Callable

from harness import until

_ESCAPES = {";": "\\:", " ": "\\s", "\\": "\\\\", "\r": "\\r", "\n": "\\n"}
_ids = itertools.count(1)


def escape(value: str) -> str:
    return "".join(_ESCAPES.get(ch, ch) for ch in str(value))


def tagged(tags: dict, rest: str) -> str:
    """``@k=v;... rest`` with IRCv3 escaping."""
    return "@" + ";".join(f"{key}={escape(value)}" for key, value in tags.items()) + " " + rest


def new_id() -> str:
    return str(uuid.uuid4())


class FakeTwitch:
    """Twitch chat server fake: CAP ACK/NAK, welcome burst, GLOBALUSERSTATE, JOIN replies with USERSTATE and ROOMSTATE,
    PING/PONG, USERSTATE (with id) after each PRIVMSG the client sends, and tagged traffic on demand."""

    def __init__(self, *, login: str = "tallybot", user_id: str = "1000", token: str = "goodtoken",
                 nak: bool = False, auth_failure: str | None = None, badges: dict[str, str] | None = None,
                 suspended: tuple[str, ...] = (), pong: bool = True, rooms: dict[str, str] | None = None):
        self.login, self.user_id, self.token = login, user_id, token
        self.nak, self.auth_failure, self.pong = nak, auth_failure, pong
        self.badges = dict(badges or {})
        self.suspended = set(suspended)
        self.slow: dict[str, int] = {}  # channel -> slow-mode seconds announced in ROOMSTATE
        self.refuse = ""  # a NOTICE msg-id that answers every PRIVMSG instead of USERSTATE (msg_ratelimit, ...)
        self.rooms = dict(rooms or {})
        self.received: list[str] = []
        self.sent_ids: list[str] = []
        self.connections = 0
        self.port = 0
        self._server: asyncio.base_events.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> FakeTwitch:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        for writer in self._writers:
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for task in list(self._tasks):
            task.cancel()

    # inspection

    def lines(self, prefix: str) -> list[str]:
        return [line for line in self.received if line.startswith(prefix)]

    def privmsgs(self) -> list[str]:
        """Outgoing chat lines, tags included."""
        return [line for line in self.received if "PRIVMSG " in line.split(" :", 1)[0]]

    def texts(self) -> list[str]:
        """The text of each outgoing chat line."""
        return [line.split(" :", 1)[1] for line in self.privmsgs()]

    async def wait_for(self, predicate: Callable[[], object], timeout: float = 10.0) -> None:
        await until(predicate, timeout, what=lambda: f"the condition; received so far: {self.received[-8:]}")

    def room_id(self, channel: str) -> str:
        return self.rooms.setdefault(channel, str(2000 + len(self.rooms)))

    # server side

    @property
    def connected(self) -> bool:
        return bool(self._writers) and not self._writers[-1].is_closing()

    async def send(self, line: str) -> None:
        """Write one raw line to the newest client connection."""
        writer = self._writers[-1]
        writer.write((line + "\r\n").encode("utf-8"))
        await writer.drain()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.append(writer)
        state: dict[str, str] = {}
        try:
            while raw := await reader.readline():
                line = raw.decode("utf-8").rstrip("\r\n")
                self.received.append(line)
                await self._on_line(line, writer, state)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _reply(self, writer: asyncio.StreamWriter, *lines: str) -> None:
        for line in lines:
            writer.write((line + "\r\n").encode("utf-8"))
        await writer.drain()

    async def _on_line(self, line: str, writer: asyncio.StreamWriter, state: dict[str, str]) -> None:
        body = line.split(" ", 1)[1] if line.startswith("@") else line
        command, _, rest = body.partition(" ")
        if command == "CAP":
            caps = rest.split(":", 1)[1] if ":" in rest else ""
            await self._reply(writer, f"CAP * NAK :{caps}" if self.nak else f":tmi.twitch.tv CAP * ACK :{caps}")
        elif command == "PASS":
            state["pass"] = rest
        elif command == "NICK":
            if self.auth_failure or state.get("pass") != f"oauth:{self.token}":
                reason = self.auth_failure or "Login authentication failed"
                await self._reply(writer, f":tmi.twitch.tv NOTICE * :{reason}")
                writer.close()
                return
            nick = rest.strip()
            await self._reply(writer, *(f":tmi.twitch.tv {num} {nick} :{text}" for num, text in (
                ("001", "Welcome, GLHF!"), ("002", "Your host is tmi.twitch.tv"), ("003", "This server is rather new"),
                ("004", "-"), ("375", "-"), ("372", "You are in a maze of twisty passages."), ("376", ">"))))
            await self._reply(writer, tagged({"badge-info": "", "badges": "", "color": "", "display-name": self.login,
                                              "emote-sets": "0", "user-id": self.user_id, "user-type": ""},
                                             ":tmi.twitch.tv GLOBALUSERSTATE"))
        elif command == "JOIN":
            for channel in rest.strip().split(","):
                if channel in self.suspended:
                    await self._reply(writer, tagged({"msg-id": "msg_channel_suspended"},
                                                     f":tmi.twitch.tv NOTICE {channel} :This channel does not exist "
                                                     "or has been suspended."))
                    continue
                await self._reply(
                    writer, f":{self.login}!{self.login}@{self.login}.tmi.twitch.tv JOIN {channel}",
                    f":{self.login}.tmi.twitch.tv 353 {self.login} = {channel} :{self.login}",
                    f":{self.login}.tmi.twitch.tv 366 {self.login} {channel} :End of /NAMES list",
                    self._userstate(channel),
                    tagged({"emote-only": "0", "followers-only": "-1", "r9k": "0", "room-id": self.room_id(channel),
                            "slow": str(self.slow.get(channel, 0)), "subs-only": "0"},
                           f":tmi.twitch.tv ROOMSTATE {channel}"))
        elif command == "PING":
            if self.pong:
                text = rest[1:] if rest.startswith(":") else rest
                await self._reply(writer, f":tmi.twitch.tv PONG tmi.twitch.tv :{text}")
        elif command == "PRIVMSG":
            channel = rest.split(" ", 1)[0]
            if self.refuse:
                await self._reply(writer, tagged({"msg-id": self.refuse},
                                                 f":tmi.twitch.tv NOTICE {channel} :Your message was not sent."))
                return
            message_id = new_id()
            self.sent_ids.append(message_id)
            await self._reply(writer, self._userstate(channel, message_id))

    def _userstate(self, channel: str, message_id: str = "") -> str:
        badges = self.badges.get(channel, "")
        tags = {"badge-info": "", "badges": badges, "color": "", "display-name": self.login, "emote-sets": "0",
                "mod": "1" if "moderator" in badges else "0", "subscriber": "0", "user-type": ""}
        if message_id:
            tags["id"] = message_id
        return tagged(tags, f":tmi.twitch.tv USERSTATE {channel}")

    # traffic on demand

    async def privmsg(self, channel: str, text: str, *, login: str = "viewer", user_id: str = "5000",
                      display_name: str | None = None, msg_id: str | None = None, badges: str = "",
                      mod: bool = False, subscriber: bool = False, vip: bool = False, first_msg: bool = False,
                      room_id: str | None = None, reply_to: dict[str, str] | None = None,
                      extra: dict[str, str] | None = None) -> str:
        """Send a tagged chat line as ``login``; returns its message id."""
        msg_id = msg_id or new_id()
        tags = {"badge-info": "", "badges": badges, "color": "", "display-name": display_name or login.title(),
                "emotes": "", "first-msg": "1" if first_msg else "0", "flags": "", "id": msg_id,
                "mod": "1" if mod else "0", "room-id": room_id or self.room_id(channel),
                "subscriber": "1" if subscriber else "0", "tmi-sent-ts": str(1_700_000_000_000 + next(_ids)),
                "turbo": "0", "user-id": user_id, "user-type": ""}
        if vip:
            tags["vip"] = "1"
        for key, value in (reply_to or {}).items():
            tags[f"reply-parent-{key}"] = value
        tags.update(extra or {})
        await self.send(tagged(tags, f":{login}!{login}@{login}.tmi.twitch.tv PRIVMSG {channel} :{text}"))
        return msg_id

    async def usernotice(self, channel: str, kind: str, *, login: str = "raider", user_id: str = "6000",
                         params: dict[str, str] | None = None, system_msg: str = "", text: str = "") -> str:
        msg_id = new_id()
        tags = {"badge-info": "", "badges": "", "color": "", "display-name": login.title(), "emotes": "", "id": msg_id,
                "login": login, "mod": "0", "msg-id": kind, "room-id": self.room_id(channel), "subscriber": "0",
                "system-msg": system_msg, "tmi-sent-ts": "1700000000000", "user-id": user_id, "user-type": ""}
        tags.update({f"msg-param-{key}": value for key, value in (params or {}).items()})
        await self.send(tagged(tags, f":tmi.twitch.tv USERNOTICE {channel}" + (f" :{text}" if text else "")))
        return msg_id

    async def clearchat(self, channel: str, login: str | None = None, user_id: str | None = None) -> None:
        """A timeout or ban of ``login`` (Twitch adds the person's ``target-user-id``), or a cleared chat."""
        tags = {"room-id": self.room_id(channel), "tmi-sent-ts": "1700000000000"}
        if user_id:
            tags["target-user-id"] = user_id
        await self.send(tagged(tags, f":tmi.twitch.tv CLEARCHAT {channel}" + (f" :{login}" if login else "")))

    async def clearmsg(self, channel: str, msg_id: str, login: str = "viewer") -> None:
        await self.send(tagged({"login": login, "room-id": "", "target-msg-id": msg_id,
                                "tmi-sent-ts": "1700000000000"}, f":tmi.twitch.tv CLEARMSG {channel} :gone"))

    async def ping(self, text: str = "tmi.twitch.tv") -> None:
        await self.send(f"PING :{text}")

    async def reconnect(self, close: bool = True) -> None:
        """Send RECONNECT and, like Twitch, drop the connection."""
        await self.send(":tmi.twitch.tv RECONNECT")
        if close:
            self._writers[-1].close()

    def drop(self) -> None:
        """Close the newest connection without a word."""
        self._writers[-1].close()
