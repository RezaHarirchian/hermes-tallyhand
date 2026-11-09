"""Helix client against the local Helix and validate fakes."""

from __future__ import annotations

import logging
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fake_helix import ALL_SCOPES
from harness import TOKEN, Clock
from tallyhand import auth, helix
from tallyhand.helix import Helix, HelixError

BOT, CHAN, ALICE = "1000", "2000", "42"
MSG = "b34ccfc7-4977-403a-8a94-33c6bac34fb8"


@pytest.fixture
def api(helix_api):
    helix_api.add_user("chan", CHAN, "Chan")
    helix_api.add_user("alice", ALICE, "Alice")
    return helix_api


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def client(api, validate, sleeps):
    async def sleep(seconds):
        sleeps.append(seconds)

    clock = Clock(1_704_103_200.0)  # 2024-01-01T10:00:00Z
    built = Helix(f"oauth:{TOKEN}", base_url=api.url, validate_url=validate.url, clock=clock, wall=clock, sleep=sleep)
    built.retry_delay = 0.5
    return built


def secrets_absent(api, *texts: str) -> None:
    for seen in api.requests:
        assert TOKEN not in seen.url and "fakeclientid" not in seen.url
        assert TOKEN not in repr(seen.body) and "fakeclientid" not in repr(seen.body)
    for text in texts:
        assert TOKEN not in text and "fakeclientid" not in text


# authorisation


async def test_headers_come_from_the_cached_validation(client, api, validate):
    assert await client.user("@Chan") == helix.User(CHAN, "chan", "Chan")
    assert await client.user("alice") == helix.User(ALICE, "alice", "Alice")
    seen = api.requests[0]
    assert (seen.method, seen.path, seen.query) == ("GET", "users", {"login": ["chan"]})
    assert (seen.authorization, seen.client_id) == (f"Bearer {TOKEN}", "fakeclientid")
    assert validate.requests == [f"OAuth {TOKEN}"]  # validated once, reused
    assert client.token_info.user_id == BOT


async def test_users_are_cached_with_a_ttl(client, api):
    await client.user("chan")
    await client.user("CHAN")
    assert len(api.calls("users")) == 1
    client.clock.now += helix.USER_TTL + 1
    await client.user("chan")
    assert len(api.calls("users")) == 2


async def test_unknown_and_invalid_logins(client, api):
    assert await client.user("nobody") is None
    assert await client.user("not a login!") is None
    assert await client.user("") is None
    assert len(api.calls("users")) == 1


async def test_remembered_validation_is_reused(api, validate):
    built = Helix(TOKEN, base_url=api.url, validate_url=validate.url)
    built.remember(auth.TokenInfo("tallybot", BOT, ALL_SCOPES, 5_000_000, "fakeclientid"), TOKEN)
    await built.user("chan")
    assert validate.requests == []
    assert api.requests[0].client_id == "fakeclientid"


async def test_a_new_token_is_validated_again(api, validate):
    token = TOKEN
    built = Helix(lambda: token, base_url=api.url, validate_url=validate.url)
    await built.user("chan")
    token = "othertoken"
    assert built.token_info is None
    with pytest.raises(HelixError) as caught:
        await built.user("alice")
    # The fake validates any token; Helix refuses the new one, which re-validates once and then fails.
    assert caught.value.code == "token_invalid"
    assert validate.requests == [f"OAuth {TOKEN}", "OAuth othertoken", "OAuth othertoken"]


async def test_missing_token(api, validate):
    with pytest.raises(HelixError) as caught:
        await Helix(lambda: "", base_url=api.url, validate_url=validate.url).user("chan")
    assert caught.value.code == "config_missing" and api.requests == [] and validate.requests == []


# endpoints


async def test_stream_live_and_offline(client, api):
    assert await client.stream(CHAN) is None
    api.go_live(CHAN, title="Any% \x07practice\n", game="Celeste", viewers=17, started_at="2024-01-01T08:45:00Z")
    assert await client.stream(CHAN) is None  # still the answer from a moment ago
    client.clock.now += helix.STREAM_TTL
    stream = await client.stream(CHAN)
    assert (stream.title, stream.game, stream.viewers, stream.login) == ("Any% practice", "Celeste", 17, "chan")
    assert stream.uptime(client.clock()) == 4500 + helix.STREAM_TTL
    assert helix.format_duration(stream.uptime(client.clock())) == "1h 15m"


async def test_stream_info_is_reused_for_a_while(client, api):
    api.go_live(CHAN)
    for _ in range(5):
        assert await client.stream(CHAN)
    assert len(api.calls("streams")) == 1
    client.clock.now += helix.STREAM_TTL - 1
    await client.stream(CHAN)
    assert len(api.calls("streams")) == 1
    client.clock.now += 1
    await client.stream(CHAN)
    assert len(api.calls("streams")) == 2


async def test_create_clip(client, api):
    with pytest.raises(HelixError) as caught:
        await client.create_clip(CHAN)
    assert (caught.value.code, caught.value.status) == ("not_found", 404)
    assert "broadcasting live" in caught.value.message
    api.go_live(CHAN)
    clip = await client.create_clip(CHAN)
    assert clip.url == f"https://clips.twitch.tv/{api.clips[-1]}"
    assert api.calls("clips")[-1].query == {"broadcaster_id": [CHAN]}


async def test_timeout_ban_and_unban(client, api):
    await client.ban(CHAN, ALICE, duration=600, reason="spam\nlinks")
    seen = api.calls("moderation/bans", "POST")[-1]
    assert seen.query == {"broadcaster_id": [CHAN], "moderator_id": [BOT]}
    assert seen.body == {"data": {"user_id": ALICE, "duration": 600, "reason": "spam links"}}
    await client.unban(CHAN, ALICE)
    assert api.bans == {}
    await client.ban(CHAN, ALICE)
    assert api.bans == {(CHAN, ALICE): None}
    with pytest.raises(HelixError) as caught:
        await client.ban(CHAN, ALICE)
    assert (caught.value.code, caught.value.message) == ("bad_request",
                                                          "The user specified in the user_id field is already banned.")
    await client.unban(CHAN, ALICE)
    with pytest.raises(HelixError) as caught:
        await client.unban(CHAN, ALICE)
    assert "not banned" in caught.value.message


@pytest.mark.parametrize("duration", [0, -5, helix.MAX_TIMEOUT + 1])
async def test_timeout_range_is_checked_before_the_call(client, api, duration):
    with pytest.raises(HelixError):
        await client.ban(CHAN, ALICE, duration=duration)
    assert api.calls("moderation/bans") == []


async def test_delete_announce_and_shoutout(client, api):
    api.messages.add(MSG)
    await client.delete_message(CHAN, MSG)
    assert api.calls("moderation/chat")[-1].query == {"broadcaster_id": [CHAN], "moderator_id": [BOT],
                                                      "message_id": [MSG]}
    with pytest.raises(HelixError) as caught:
        await client.delete_message(CHAN, MSG)
    assert caught.value.code == "not_found"
    with pytest.raises(HelixError) as caught:
        await client.delete_message(CHAN, "not-an-id")
    assert caught.value.code == "bad_request" and len(api.calls("moderation/chat")) == 2
    await client.announce(CHAN, "  hello\r\nchat ", color="purple")
    await client.announce(CHAN, "plain", color="neon")
    assert api.announcements == [(CHAN, "hello chat", "purple"), (CHAN, "plain", "primary")]
    with pytest.raises(HelixError):
        await client.announce(CHAN, " \n ")
    api.go_live(CHAN)
    await client.shoutout(CHAN, ALICE)
    assert api.shoutouts == [(CHAN, ALICE)]
    assert api.calls("chat/shoutouts")[-1].query["moderator_id"] == [BOT]


# errors


async def test_missing_scope_fails_before_the_call(client, api, validate):
    validate.respond(scopes=[s for s in ALL_SCOPES if s != "clips:edit"])
    api.go_live(CHAN)
    with pytest.raises(HelixError) as caught:
        await client.create_clip(CHAN)
    assert caught.value.code == "missing_scope"
    assert "clips:edit" in caught.value.message and "new TWITCH_BOT_TOKEN" in caught.value.message
    assert api.calls("clips") == []


async def test_401_revalidates_once_and_retries(client, api, validate):
    await client.user("chan")
    api.force(401)
    assert await client.user("alice")
    assert len(validate.requests) == 2 and len(api.calls("users")) == 3


async def test_401_with_a_dead_token_is_final(client, api, validate):
    await client.user("chan")
    validate.respond(status=401)
    with pytest.raises(HelixError) as caught:
        await client.user("alice")
    assert (caught.value.code, caught.value.message, caught.value.retryable) == (
        "token_invalid", auth.INVALID_TOKEN, False)
    assert client.token_info is None


async def test_second_401_fails(client, api, validate):
    api.force(401, times=2)
    with pytest.raises(HelixError) as caught:
        await client.user("chan")
    assert caught.value.code == "token_invalid" and len(api.calls("users")) == 2


async def test_401_that_reveals_a_lost_scope(client, api, validate):
    api.go_live(CHAN)
    await client.user("chan")
    validate.respond(scopes=["chat:read", "chat:edit"])
    api.force(401)
    with pytest.raises(HelixError) as caught:
        await client.create_clip(CHAN)
    assert caught.value.code == "missing_scope" and "clips:edit" in caught.value.message


async def test_403_gives_twitchs_reason_and_the_moderator_role(client, api):
    api.moderated = set()
    with pytest.raises(HelixError) as caught:
        await client.ban(CHAN, ALICE, duration=60)
    error = caught.value
    assert (error.code, error.status, error.retryable) == ("forbidden", 403, False)
    assert error.message == ("Twitch refused this (The user in moderator_id is not one of the broadcaster's "
                             "moderators.); the bot account must be a moderator of the channel")
    assert "TWITCH_BOT_TOKEN" not in error.message  # the scope was checked before the call; a new token cannot help


async def test_429_waits_for_the_reset_once(client, api, sleeps):
    api.force(429, reset=client.clock() + 7)
    assert await client.user("chan")
    assert sleeps == [7.0]


async def test_a_429_cooldown_fails_at_once_with_twitchs_reason(client, api, sleeps):
    api.go_live(CHAN)
    reason = "The broadcaster may not give the same broadcaster a Shoutout more than once every 60 minutes."
    api.force(429, remaining=799, message=reason)
    with pytest.raises(HelixError) as caught:
        await client.shoutout(CHAN, ALICE)
    assert (caught.value.code, caught.value.message, sleeps) == ("rate_limited", reason, [])
    assert len(api.calls("chat/shoutouts")) == 1


async def test_429_twice_fails(client, api, sleeps):
    api.force(429, times=2, reset=client.clock() + 3)
    with pytest.raises(HelixError) as caught:
        await client.user("chan")
    assert (caught.value.code, caught.value.retryable) == ("rate_limited", True)
    assert sleeps == [3.0] and len(api.calls("users")) == 2


async def test_429_with_a_distant_reset_fails_at_once(client, api, sleeps):
    api.force(429, reset=client.clock() + 3600)
    with pytest.raises(HelixError) as caught:
        await client.user("chan")
    assert caught.value.code == "rate_limited" and sleeps == []


async def test_a_get_retries_once_after_a_5xx(client, api, sleeps):
    api.force(503)
    assert await client.user("chan")
    assert sleeps == [0.5] and len(api.calls("users")) == 2
    api.force(500, times=2)
    with pytest.raises(HelixError) as caught:
        await client.user("alice")
    assert (caught.value.code, caught.value.status, caught.value.retryable) == ("unavailable", 500, True)
    assert len(api.calls("users")) == 4 and caught.value.message == "the Twitch API failed (HTTP 500)"


async def test_unreachable_api(api, validate, sleeps):
    api.stop()

    async def sleep(seconds):
        sleeps.append(seconds)

    built = Helix(TOKEN, base_url=api.url, validate_url=validate.url, sleep=sleep)
    with pytest.raises(HelixError) as caught:
        await built.user("chan")
    assert caught.value.code == "unavailable" and caught.value.retryable
    assert sleeps == [helix.RETRY_DELAY] and helix.MAYBE_DONE not in caught.value.message


WRITES = {  # one call of each POST and DELETE endpoint, with the path and method it uses
    "clip": (lambda client: client.create_clip(CHAN), "clips", "POST"),
    "announce": (lambda client: client.announce(CHAN, "Giveaway at nine"), "chat/announcements", "POST"),
    "shoutout": (lambda client: client.shoutout(CHAN, ALICE), "chat/shoutouts", "POST"),
    "ban": (lambda client: client.ban(CHAN, ALICE, reason="spam"), "moderation/bans", "POST"),
    "unban": (lambda client: client.unban(CHAN, ALICE), "moderation/bans", "DELETE"),
    "delete": (lambda client: client.delete_message(CHAN, MSG), "moderation/chat", "DELETE"),
}


def flaky(monkeypatch, failures: int) -> list[str]:
    """The first ``failures`` Helix requests lose their connection; the methods of all requests are kept."""
    real, methods = helix._request, []

    def request(method, url, headers, body, timeout):
        methods.append(method)
        if len(methods) <= failures:
            raise urllib.error.URLError(ConnectionResetError("connection reset by peer"))
        return real(method, url, headers, body, timeout)

    monkeypatch.setattr(helix, "_request", request)
    return methods


@pytest.mark.parametrize("status", [502])
@pytest.mark.parametrize("write", list(WRITES))
async def test_a_write_is_never_repeated_after_a_5xx(client, api, sleeps, write, status):
    call, path, method = WRITES[write]
    api.messages.add(MSG)
    api.force(status)
    with pytest.raises(HelixError) as caught:
        await call(client)
    assert (caught.value.code, caught.value.status, caught.value.retryable) == ("unavailable", status, False)
    assert caught.value.message == f"the Twitch API failed (HTTP {status}){helix.MAYBE_DONE}"
    assert len(api.calls(path, method)) == 1 and sleeps == []


@pytest.mark.parametrize("write", list(WRITES))
async def test_a_write_is_never_repeated_after_a_network_error(client, api, sleeps, monkeypatch, write):
    call, _path, method = WRITES[write]
    await client.validate()
    methods = flaky(monkeypatch, failures=1)
    with pytest.raises(HelixError) as caught:
        await call(client)
    assert (caught.value.code, caught.value.retryable) == ("unavailable", False)
    assert caught.value.message.endswith(helix.MAYBE_DONE) and "connection reset by peer" in caught.value.message
    assert methods == [method] and sleeps == []


async def test_a_get_retries_once_after_a_network_error(client, api, sleeps, monkeypatch):
    methods = flaky(monkeypatch, failures=1)
    assert await client.user("chan") == helix.User(CHAN, "chan", "Chan")
    assert methods == ["GET", "GET"] and sleeps == [0.5]
    methods = flaky(monkeypatch, failures=2)
    with pytest.raises(HelixError) as caught:
        await client.user("alice")
    assert (caught.value.code, caught.value.retryable) == ("unavailable", True)
    assert methods == ["GET", "GET"] and helix.MAYBE_DONE not in caught.value.message


async def test_redirects_are_not_followed(api, validate):
    seen: list[str] = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization", ""))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=target.serve_forever, daemon=True).start()

    class Redirect(Target):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_address[1]}/stolen")
            self.end_headers()

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=redirect.serve_forever, daemon=True).start()
    try:
        built = Helix(TOKEN, base_url=f"http://127.0.0.1:{redirect.server_address[1]}/helix",
                      validate_url=validate.url)
        with pytest.raises(HelixError) as caught:
            await built.user("chan")
        assert caught.value.status == 302 and seen == []
    finally:
        for server in (redirect, target):
            server.shutdown()
            server.server_close()


async def test_no_token_or_client_id_in_requests_errors_or_logs(client, api, validate, caplog):
    caplog.set_level(logging.DEBUG)
    expected = {400: "bad_request", 401: "token_invalid", 403: "forbidden", 404: "not_found", 409: "conflict",
                429: "rate_limited", 500: "unavailable"}
    errors = []
    for status, code in expected.items():
        api.forced.clear()
        api.force(status, times=2, reset=client.clock() + 1, message=f"echo {TOKEN} fakeclientid")
        with pytest.raises(HelixError) as caught:
            await client.ban(CHAN, ALICE, duration=60, reason=f"reason with {TOKEN}")
        assert caught.value.code == code, (status, caught.value.message)
        errors.append(caught.value.message)
    assert "echo [redacted] [redacted]" in errors[0]  # Twitch's own message is passed on, scrubbed
    secrets_absent(api, caplog.text, *errors)
    assert all(seen.authorization == f"Bearer {TOKEN}" for seen in api.requests)


# helpers


@pytest.mark.parametrize(("seconds", "text"), [
    (0, "0s"), (45, "45s"), (60, "1m"), (3599, "59m"), (3600, "1h 0m"), (8100, "2h 15m"), (90061, "1d 1h 1m")])
def test_format_duration(seconds, text):
    assert helix.format_duration(seconds) == text


@pytest.mark.parametrize(("value", "login"), [
    ("@Alice", "alice"), ("#chan", "chan"), (" bob_1 ", "bob_1"), ("not valid", ""), ("x" * 26, ""), (None, "")])
def test_login_of(value, login):
    assert helix.login_of(value) == login


async def test_one_client_serves_other_threads_and_loops(api, validate):
    """Hermes runs async tools on worker threads with their own loops, while the adapter lives on the gateway's."""
    import asyncio
    import concurrent.futures

    shared = Helix(TOKEN, base_url=api.url, validate_url=validate.url)
    await shared.user("chan")

    def worker(login: str):
        return asyncio.run(shared.user(login))

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        found = await asyncio.gather(*(asyncio.get_running_loop().run_in_executor(pool, worker, login)
                                       for login in ("chan", "alice", "alice", "nobody")))
    assert [user.login if user else None for user in found] == ["chan", "alice", "alice", None]
    assert len(validate.requests) == 1


async def test_users_in_batches_of_a_hundred_with_the_cache(client, api):
    for index in range(150):
        api.add_user(f"fan{index}", str(5000 + index))
    others = [f"fan{index}" for index in range(1, 149)]
    found = await client.users(["@Alice", "fan0", "#FAN149", "nobody", "not a login!", "fan0", *others])
    assert found["alice"].id == ALICE and found["fan149"].id == "5149" and "nobody" not in found
    assert len(found) == 151
    batches = [seen.query["login"] for seen in api.calls("users")]
    assert [len(batch) for batch in batches] == [100, 52] and "not a login!" not in sum(batches, [])
    assert (await client.users(["alice", "fan7"])).keys() == {"alice", "fan7"}
    assert len(api.calls("users")) == 2  # both answered from the cache
    assert await client.user("fan99") == found["fan99"] and len(api.calls("users")) == 2


async def test_users_refuses_nothing_and_asks_nothing_for_no_logins(client, api):
    assert await client.users([]) == {} and await client.users(["???"]) == {}
    assert api.calls("users") == []


def test_missing_scopes_by_feature():
    report = dict(helix.missing_by_feature(["chat:read", "clips:edit"]))
    assert report["Chat"] == ["chat:edit"]
    assert report["Clips: !clip, twitch_create_clip, automatic clips"] == []
    assert report["Timeouts, bans and unbans"] == ["moderator:manage:banned_users"]
    assert all(not missing for _, missing in helix.missing_by_feature(ALL_SCOPES))
    assert {scope for _, needed in helix.FEATURES for scope in needed} == set(ALL_SCOPES)


async def test_caches_run_on_the_monotonic_clock_and_a_validation_is_reused_for_an_hour(client, api, validate):
    client.wall = lambda: 0.0  # a wall clock stepped far back leaves the caches alone
    await client.stream(CHAN)
    client.clock.now += helix.STREAM_TTL + 1
    await client.stream(CHAN)
    assert [seen.path for seen in api.requests] == ["streams", "streams"]
    assert validate.requests == [f"OAuth {TOKEN}"]
    client.clock.now += auth.REVALIDATE_SECONDS
    await client.user("alice")
    assert validate.requests == [f"OAuth {TOKEN}"] * 2
