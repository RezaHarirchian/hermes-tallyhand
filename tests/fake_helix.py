"""Local stand-in for https://api.twitch.tv/helix on 127.0.0.1: the endpoints Tallyhand calls, with Twitch's token,
client id, scope and moderator checks, plus forced 401, 403, 429 (with Ratelimit-Reset) and 5xx answers."""

from __future__ import annotations

import itertools
import json
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fake_validate import FakeValidate

ALL_SCOPES = ("chat:read", "chat:edit", "clips:edit", "moderator:manage:banned_users",
              "moderator:manage:chat_messages", "moderator:manage:announcements", "moderator:manage:shoutouts")
SCOPES = {
    ("POST", "clips"): "clips:edit",
    ("POST", "moderation/bans"): "moderator:manage:banned_users",
    ("DELETE", "moderation/bans"): "moderator:manage:banned_users",
    ("DELETE", "moderation/chat"): "moderator:manage:chat_messages",
    ("POST", "chat/announcements"): "moderator:manage:announcements",
    ("POST", "chat/shoutouts"): "moderator:manage:shoutouts",
}
_clip_ids = itertools.count(1)


@dataclass
class Seen:
    """One request as the fake received it."""

    method: str
    path: str
    query: dict[str, list[str]]
    body: object
    url: str
    authorization: str
    client_id: str


@dataclass
class Forced:
    status: int
    reset: float | None = None
    message: str = ""
    remaining: int = 0


@dataclass
class FakeHelix:
    """Answers like Helix for the token and client id that ``validate`` hands out. ``moderated`` holds the
    broadcaster ids where the bot is a moderator (None: everywhere)."""

    validate: FakeValidate
    token: str = "goodtoken"
    users: dict[str, dict] = field(default_factory=dict)
    streams: dict[str, dict] = field(default_factory=dict)
    channels: dict[str, dict] = field(default_factory=dict)  # broadcaster_id -> channel information row
    moderated: set[str] | None = None
    bans: dict[tuple[str, str], int | None] = field(default_factory=dict)
    messages: set[str] = field(default_factory=set)
    announcements: list[tuple[str, str, str]] = field(default_factory=list)
    shoutouts: list[tuple[str, str]] = field(default_factory=list)
    clips: list[str] = field(default_factory=list)
    requests: list[Seen] = field(default_factory=list)
    forced: deque[Forced] = field(default_factory=deque)

    def __post_init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._lock = threading.Lock()

    # setup

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}/helix"

    def add_user(self, login: str, user_id: str, display_name: str | None = None) -> dict:
        user = {"id": user_id, "login": login, "display_name": display_name or login.title(), "type": "",
                "broadcaster_type": "", "description": "", "created_at": "2020-01-01T00:00:00Z"}
        self.users[login] = user
        return user

    def go_live(self, user_id: str, *, title: str = "Speedrun", game: str = "Celeste", viewers: int = 42,
                started_at: str = "2024-01-01T10:00:00Z") -> None:
        login = next((u["login"] for u in self.users.values() if u["id"] == user_id), "")
        self.streams[user_id] = {"id": "9", "user_id": user_id, "user_login": login, "user_name": login.title(),
                                 "game_id": "1", "game_name": game, "type": "live", "title": title,
                                 "viewer_count": viewers, "started_at": started_at, "language": "en",
                                 "thumbnail_url": "", "tags": [], "is_mature": False}

    def set_channel(self, user_id: str, *, language: str = "en", title: str = "Speedrun",
                    game: str = "Celeste", tags: list[str] | None = None) -> dict:
        """Channel information for Get Channel Information (broadcaster_language and friends)."""
        login = next((u["login"] for u in self.users.values() if u["id"] == user_id), "")
        row = {"broadcaster_id": user_id, "broadcaster_login": login, "broadcaster_name": login.title(),
               "broadcaster_language": language, "game_id": "1", "game_name": game, "title": title, "delay": 0,
               "tags": list(tags or []), "content_classification_labels": [], "is_branded_content": False}
        self.channels[user_id] = row
        return row

    def force(self, status: int, *, times: int = 1, reset: float | None = None, message: str = "",
              remaining: int = 0) -> None:
        """The next ``times`` requests get ``status``; ``reset`` sets Ratelimit-Reset (epoch seconds). A 429 with
        ``remaining`` points left is a per-feature cooldown (Twitch's shoutout limits), not an empty bucket."""
        self.forced.extend(Forced(status, reset, message, remaining) for _ in range(times))

    def calls(self, path: str, method: str | None = None) -> list[Seen]:
        return [r for r in self.requests if r.path == path and (method is None or r.method == method)]

    # server

    def start(self) -> FakeHelix:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                status, body = fake._answer(self.command, self.path, raw, self.headers)
                payload = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Ratelimit-Limit", "800")
                self.send_header("Ratelimit-Remaining", str(fake._remaining))
                self.send_header("Ratelimit-Reset", str(fake._reset))
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_DELETE = _serve

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # behaviour

    _reset = 0
    _remaining = 799

    @staticmethod
    def _error(status: int, message: str) -> tuple[int, dict]:
        names = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 409: "Conflict",
                 429: "Too Many Requests", 500: "Internal Server Error", 503: "Service Unavailable"}
        return status, {"error": names.get(status, "Error"), "status": status, "message": message}

    def _answer(self, method: str, target: str, raw: bytes, headers) -> tuple[int, dict | None]:
        parts = urllib.parse.urlsplit(target)
        path = parts.path.removeprefix("/helix/")
        query = urllib.parse.parse_qs(parts.query)
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode("utf-8", "replace")
        with self._lock:
            self.requests.append(Seen(method, path, query, body, target, headers.get("Authorization", ""),
                                      headers.get("Client-Id", "")))
            self._reset, self._remaining = int(time.time()) + 60, 799
            if self.forced:
                forced = self.forced.popleft()
                if forced.reset is not None:
                    self._reset = forced.reset
                if forced.status == 429:
                    self._remaining = forced.remaining
                return self._error(forced.status, forced.message or "forced")
            return self._route(method, path, query, body, headers)

    def _route(self, method: str, path: str, query: dict, body: object, headers) -> tuple[int, dict | None]:
        if headers.get("Authorization") != f"Bearer {self.token}" or self.validate.status == 401:
            return self._error(401, "Invalid OAuth token")
        if headers.get("Client-Id") != self.validate.client_id:
            return self._error(401, "Client ID and OAuth token do not match")
        q = {key: values[0] for key, values in query.items()}
        if (scope := SCOPES.get((method, path))) and scope not in self.validate.scopes:
            return self._error(401, f"Missing scope: {scope}")
        if "moderator_id" in q:
            if q["moderator_id"] != self.validate.user_id:
                return self._error(403, "The ID in moderator_id must match the user ID in the user access token.")
            channel = q.get("broadcaster_id", q.get("from_broadcaster_id"))
            if self.moderated is not None and channel not in self.moderated:
                return self._error(403, "The user in moderator_id is not one of the broadcaster's moderators.")
        handler = getattr(self, f"_{method.lower()}_{path.replace('/', '_')}", None)
        if handler is None:
            return self._error(404, "unknown endpoint")
        return handler(q, query, body)

    def _get_users(self, q, query, body):
        rows = [self.users[login] for login in query.get("login", []) if login in self.users]
        rows += [u for u in self.users.values() if u["id"] in query.get("id", [])]
        return 200, {"data": rows}

    def _get_streams(self, q, query, body):
        return 200, {"data": [self.streams[i] for i in query.get("user_id", []) if i in self.streams],
                     "pagination": {}}

    def _get_channels(self, q, query, body):
        rows = [self.channels[i] for i in query.get("broadcaster_id", []) if i in self.channels]
        return 200, {"data": rows}

    def _post_clips(self, q, query, body):
        if q.get("broadcaster_id") not in self.streams:
            return self._error(404, "The broadcaster in the broadcaster_id query parameter must be broadcasting live.")
        clip = f"FakeClip{next(_clip_ids)}"
        self.clips.append(clip)
        return 202, {"data": [{"id": clip, "edit_url": f"https://clips.twitch.tv/{clip}/edit"}]}

    def _post_moderation_bans(self, q, query, body):
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or not data.get("user_id"):
            return self._error(400, "The user_id field is required.")
        duration = data.get("duration")
        if duration is not None and not (isinstance(duration, int) and 1 <= duration <= 1_209_600):
            return self._error(400, "The value in the duration field is not valid.")
        key = (q["broadcaster_id"], data["user_id"])
        if key in self.bans and self.bans[key] is None:
            return self._error(400, "The user specified in the user_id field is already banned.")
        self.bans[key] = duration
        return 200, {"data": [{"broadcaster_id": key[0], "moderator_id": q["moderator_id"], "user_id": key[1],
                               "created_at": "2024-01-01T10:00:00Z",
                               "end_time": None if duration is None else "2024-01-01T10:10:00Z"}]}

    def _delete_moderation_bans(self, q, query, body):
        if self.bans.pop((q.get("broadcaster_id"), q.get("user_id")), "missing") == "missing":
            return self._error(400, "The user specified in the user_id field is not banned.")
        return 204, None

    def _delete_moderation_chat(self, q, query, body):
        if q.get("message_id") not in self.messages:
            return self._error(404, "The ID in message_id was not found.")
        self.messages.discard(q["message_id"])
        return 204, None

    def _post_chat_announcements(self, q, query, body):
        if not isinstance(body, dict) or not body.get("message"):
            return self._error(400, "The message field is required.")
        self.announcements.append((q["broadcaster_id"], body["message"], body.get("color", "primary")))
        return 204, None

    def _post_chat_shoutouts(self, q, query, body):
        if q.get("from_broadcaster_id") == q.get("to_broadcaster_id"):
            return self._error(400, "The broadcaster may not give themselves a Shoutout.")
        if q.get("from_broadcaster_id") not in self.streams:
            return self._error(400, "The broadcaster is not streaming live or does not have one or more viewers.")
        self.shoutouts.append((q["from_broadcaster_id"], q["to_broadcaster_id"]))
        return 204, None
