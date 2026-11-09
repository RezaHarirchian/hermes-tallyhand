"""Bot token checks: prefix handling, validate responses, the validate endpoint and the expiry warning."""

from __future__ import annotations

import json

import pytest

from harness import Clock, closed_port
from tallyhand import auth
from tallyhand.auth import AuthError, ExpiryNotice, TokenInfo

TOKEN = "zq9s3cr3tt0ken7x"
DAY = 86400


def body(**changes) -> bytes:
    """A validate response body; a None value leaves that key out."""
    data = {"client_id": "cid", "login": "TallyBot", "scopes": ["chat:read", "chat:edit"], "user_id": "1000",
            "expires_in": 5_000_000, **changes}
    return json.dumps({key: value for key, value in data.items() if value is not None}).encode()


def parse_error(status: int, raw: bytes) -> AuthError:
    with pytest.raises(AuthError) as caught:
        auth.parse_validation(status, raw)
    return caught.value


async def validate_error(token: str, url: str) -> AuthError:
    with pytest.raises(AuthError) as caught:
        await auth.validate_token(token, url=url, timeout=5.0)
    return caught.value


def token_info(expires_in: int) -> TokenInfo:
    return TokenInfo(login="tallybot", user_id="1000", scopes=auth.REQUIRED_SCOPES, expires_in=expires_in,
                     client_id="cid")


def assert_token_free(err: AuthError) -> None:
    for text in (err.code, err.message, str(err), repr(err)):
        assert TOKEN not in text


# pure helpers

@pytest.mark.parametrize("value, expected", [
    ("oauth:abc", "abc"),
    ("OAuth:abc", "abc"),
    ("OAUTH:abc", "abc"),
    ("abc", "abc"),
    (None, ""),
    ("", ""),
    ("   ", ""),
    ("oauth:", ""),
    ("  oauth:abc  ", "abc"),
    ("oauth: abc ", "abc"),
    (" abc\n", "abc"),
])
def test_strip_oauth_prefix(value, expected):
    assert auth.strip_oauth_prefix(value) == expected


@pytest.mark.parametrize("scopes, missing", [
    (["chat:read", "chat:edit"], []),
    (("chat:edit", "user:read:email", "chat:read"), []),
    (["chat:read"], ["chat:edit"]),
    (("chat:edit",), ["chat:read"]),
    ([], ["chat:read", "chat:edit"]),
])
def test_missing_scopes(scopes, missing):
    assert auth.missing_scopes(scopes) == missing


# parse_validation

def test_parse_validation_ok():
    assert auth.parse_validation(200, body()) == TokenInfo(
        login="tallybot", user_id="1000", scopes=("chat:read", "chat:edit"), expires_in=5_000_000, client_id="cid")


@pytest.mark.parametrize("scopes, missing", [
    (["chat:read"], ["chat:edit"]),
    (["chat:edit"], ["chat:read"]),
    ([], ["chat:read", "chat:edit"]),
])
def test_parse_validation_missing_scope(scopes, missing):
    err = parse_error(200, body(scopes=scopes))
    assert (err.code, err.retryable) == ("token_scope", False)
    assert all(scope in err.message for scope in missing)


def test_scope_message_differs_by_missing_scope():
    messages = {parse_error(200, body(scopes=scopes)).message for scopes in (["chat:read"], ["chat:edit"], [])}
    assert len(messages) == 3


def test_parse_validation_401():
    assert auth.INVALID_TOKEN == "token is invalid or expired; generate a new one and set TWITCH_BOT_TOKEN"
    err = parse_error(401, b'{"status": 401, "message": "invalid access token"}')
    assert err.retryable is False
    assert err.message == str(err) == auth.INVALID_TOKEN


@pytest.mark.parametrize("status", [500, 502, 503, 429])
def test_parse_validation_retryable_status(status):
    assert parse_error(status, b'{"status": %d, "message": "upstream error"}' % status).retryable is True


@pytest.mark.parametrize("status", [400, 403])
def test_parse_validation_rejected_status(status):
    assert parse_error(status, b'{"status": %d, "message": "rejected"}' % status).retryable is False


@pytest.mark.parametrize("raw", [b"<html>busy</html>", b"", b"{truncated", b"\xff\xfe\xfd", b"[]", b"null"])
def test_parse_validation_unreadable_body_is_retryable(raw):
    assert parse_error(200, raw).retryable is True


@pytest.mark.parametrize("changes", [
    {"login": None, "user_id": None, "scopes": []},
    {"login": None, "user_id": None},
    {"login": ""},
    {"user_id": None},
])
def test_parse_validation_app_token_is_rejected(changes):
    assert parse_error(200, body(**changes)).retryable is False


# validate_token against the local endpoint

@pytest.mark.parametrize("token", [f"oauth:{TOKEN}", f"OAuth:{TOKEN}", TOKEN, f"  oauth:{TOKEN}\n"])
async def test_validate_token_success(validate, token):
    info = await auth.validate_token(token, url=validate.url)
    assert info == TokenInfo(login="tallybot", user_id="1000", scopes=("chat:read", "chat:edit"),
                             expires_in=5_000_000, client_id="fakeclientid")
    assert validate.requests == [f"OAuth {TOKEN}"]


async def test_validate_token_invalid(validate):
    validate.respond(status=401)
    err = await validate_error(TOKEN, validate.url)
    assert (err.retryable, err.message) == (False, auth.INVALID_TOKEN)


async def test_validate_token_server_error_is_retryable(validate):
    validate.respond(status=500)
    assert (await validate_error(TOKEN, validate.url)).retryable is True


async def test_validate_token_unreachable_is_retryable():
    err = await validate_error(TOKEN, f"http://127.0.0.1:{closed_port()}/oauth2/validate")
    assert (err.code, err.retryable) == ("validate_unavailable", True)


@pytest.mark.parametrize("token", ["", "   ", "oauth:", "OAuth: "])
async def test_validate_token_empty_is_not_retryable(validate, token):
    assert (await validate_error(token, validate.url)).retryable is False
    assert validate.requests == []


@pytest.mark.parametrize("changes", [
    {"status": 401}, {"status": 400}, {"status": 403}, {"status": 429}, {"status": 503},
    {"scopes": ["chat:read"]}, {"scopes": []}, {"login": "", "user_id": ""},
])
async def test_token_never_in_validate_errors(validate, changes):
    validate.respond(**changes)
    assert_token_free(await validate_error(f"oauth:{TOKEN}", validate.url))


async def test_token_never_in_transport_errors(validate):
    for token, url in ((TOKEN, f"http://127.0.0.1:{closed_port()}/oauth2/validate"), (TOKEN, "not a url"),
                       (f"{TOKEN}\r\nX-Injected: 1", validate.url)):
        assert_token_free(await validate_error(f"oauth:{token}", url))
    assert validate.requests == []


@pytest.mark.parametrize("status, raw", [(200, b"<html>"), (200, body(scopes=[])), (401, b""), (502, b""),
                                         (403, b"")])
def test_token_never_in_parse_errors(status, raw):
    assert_token_free(parse_error(status, raw))


# expiry warning

def test_expiry_notice_warns_once_a_day():
    clock = Clock()
    notice = ExpiryNotice(clock=clock)
    soon = token_info(3 * DAY)
    assert notice.check(soon)
    clock.now += DAY - 1
    assert notice.check(soon) is None
    clock.now += 2
    assert notice.check(soon)
    clock.now += 3600
    assert notice.check(soon) is None


@pytest.mark.parametrize("expires_in", [1, 3600, 7 * DAY - 1])
def test_expiry_notice_warns_inside_seven_days(expires_in):
    warning = ExpiryNotice(clock=Clock()).check(token_info(expires_in))
    assert isinstance(warning, str) and warning


@pytest.mark.parametrize("expires_in", [0, 7 * DAY, 7 * DAY + 1, 60 * DAY])
def test_expiry_notice_quiet(expires_in):
    notice = ExpiryNotice(clock=Clock())
    assert notice.check(token_info(expires_in)) is None
    assert notice.check(token_info(expires_in)) is None


async def test_expiry_warning_has_no_token(validate):
    validate.respond(expires_in=2 * DAY + 3600)
    info = await auth.validate_token(f"oauth:{TOKEN}", url=validate.url)
    warning = ExpiryNotice(clock=Clock()).check(info)
    assert warning and TOKEN not in warning


@pytest.mark.parametrize("text", ["x goodtoken y", "x good`token y", "x good\x07token y", "x good\u200btoken y",
                                  "x g.o.o.d.t.o.k.e.n y"])
def test_redact_finds_a_split_token(text):
    assert auth.redact(text, "goodtoken") == "x [redacted] y"
    assert auth.redact("good token", "goodtoken") == "good token" and auth.redact(text, "") == text
