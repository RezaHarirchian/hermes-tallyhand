"""Local stand-in for https://id.twitch.tv/oauth2/validate on 127.0.0.1."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeValidate:
    """Answers GET with 200 and the configured token info, or with the configured error status."""

    def __init__(self, *, login: str = "tallybot", user_id: str = "1000",
                 scopes: tuple[str, ...] = ("chat:read", "chat:edit"), expires_in: int = 5_000_000,
                 status: int = 200, client_id: str = "fakeclientid"):
        self.login, self.user_id, self.scopes = login, user_id, list(scopes)
        self.expires_in, self.status, self.client_id = expires_in, status, client_id
        self.requests: list[str] = []  # Authorization header of each request
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}/oauth2/validate"

    def respond(self, **changes) -> None:
        """Change later answers, e.g. ``respond(status=401)`` or ``respond(scopes=["chat:read"])``."""
        for key, value in changes.items():
            setattr(self, key, list(value) if key == "scopes" else value)

    def _body(self) -> tuple[int, bytes]:
        if self.status == 200:
            return 200, json.dumps({"client_id": self.client_id, "login": self.login, "scopes": self.scopes,
                                    "user_id": self.user_id, "expires_in": self.expires_in}).encode()
        message = "invalid access token" if self.status == 401 else "upstream error"
        return self.status, json.dumps({"status": self.status, "message": message}).encode()

    def start(self) -> FakeValidate:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                fake.requests.append(self.headers.get("Authorization", ""))
                status, body = fake._body()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
