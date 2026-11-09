"""Twitch IRC wire format: tag escaping, line parsing, message shapes, outbound lines and reply formatting."""

from __future__ import annotations

import itertools
import time
import unicodedata

import pytest

from tallyhand import irc, sender
from tallyhand.irc import parse_line

MSG_ID = "885196de-cb67-427a-baa8-82f9b0fcd05f"
PARENT_ID = "b34ccfc7-4977-403a-8a94-33c6bac34fb8"
WORDS = " ".join(f"word{i:03d}" + "x" * (i % 9) for i in range(200))
FILLER = " ".join(["lorem"] * 80)  # 479 chars, so whatever follows opens the second chat line
CONTROLS = "one\x00two \x01ACTION waves\x01 \x07\x1b[31mred\x7f \x85three\x9b\tfour\x0b\x0cfive\r\nsix\u2028seven"

PRIVMSG = "@" + ";".join((
    "badge-info=subscriber/14", "badges=moderator/1,subscriber/12", "client-nonce=4b1f0c", "color=#1E90FF",
    "display-name=Alice_42", "emotes=", "first-msg=0", "flags=", f"id={MSG_ID}", "mod=1",
    "reply-parent-display-name=Bob", r"reply-parent-msg-body=what\sgame\sis\sthis?", f"reply-parent-msg-id={PARENT_ID}",
    "reply-parent-user-id=12345", "reply-parent-user-login=bob", "returning-chatter=0", "room-id=713936733",
    "subscriber=1", "tmi-sent-ts=1642696567751", "turbo=0", "user-id=58355428", "user-type=mod",
)) + " :alice_42!alice_42@alice_42.tmi.twitch.tv PRIVMSG #Streamer :@Bob it's Celeste: a platformer"

RAID = "@" + ";".join((
    "badge-info=", "badges=", "color=#FF0000", "display-name=Raider", "emotes=", "flags=",
    "id=3d830f12-795c-447d-af3c-ea05e40fbddb", "login=raider", "mod=0", "msg-id=raid", "msg-param-displayName=Raider",
    "msg-param-login=raider", "msg-param-viewerCount=15", "room-id=713936733", "subscriber=0",
    r"system-msg=15\sraiders\sfrom\sRaider\shave\sjoined!", "tmi-sent-ts=1507246572675", "user-id=123456", "user-type=",
)) + " :tmi.twitch.tv USERNOTICE #Streamer"

RESUB = "@" + ";".join((
    "badge-info=subscriber/6", "badges=subscriber/6", "color=#008000", "display-name=Ronni", "emotes=",
    "id=db25007f-7a18-43eb-9379-80131e44d633", "login=ronni", "mod=0", "msg-id=resub", "msg-param-cumulative-months=6",
    "msg-param-months=0", "msg-param-should-share-streak=1", "msg-param-streak-months=2",
    r"msg-param-sub-plan-name=Channel\sSubscription", "msg-param-sub-plan=1000", "room-id=713936733", "subscriber=1",
    r"system-msg=Ronni\ssubscribed\sat\sTier\s1.\sThey've\ssubscribed\sfor\s6\smonths!", "tmi-sent-ts=1507246572675",
    "user-id=87654321", "user-type=",
)) + " :tmi.twitch.tv USERNOTICE #streamer :Great stream, keep it up!"


def tagged_line(tags: dict[str, str], rest: str) -> irc.Line | None:
    return parse_line("@" + ";".join(f"{key}={value}" for key, value in tags.items()) + " " + rest)


def chat(text: str = "hello", tags: dict[str, str] | None = None, *, nick: str = "alice",
         channel: str = "#chan") -> irc.ChatMessage | None:
    base = {"display-name": "Alice", "id": MSG_ID, "room-id": "1000", "user-id": "42"}
    rest = f":{nick}!{nick}@{nick}.tmi.twitch.tv PRIVMSG {channel} :{text}"
    return irc.parse_privmsg(tagged_line({**base, **(tags or {})}, rest))


def notice(kind: str, params: dict[str, str] | None = None, text: str = "") -> irc.ChannelEvent | None:
    tags = {"display-name": "Gifter", "id": MSG_ID, "login": "gifter", "msg-id": kind, "room-id": "1000",
            "system-msg": r"A\ssystem\smessage", "tmi-sent-ts": "1700000000000", "user-id": "77",
            **{f"msg-param-{key}": value for key, value in (params or {}).items()}}
    return irc.parse_usernotice(tagged_line(tags, ":tmi.twitch.tv USERNOTICE #chan" + (f" :{text}" if text else "")))


def control_chars(lines: list[str]) -> list[str]:
    return [ch for line in lines for ch in line if unicodedata.category(ch) == "Cc"]


@pytest.mark.parametrize(("raw", "value"), [
    (r"a\:b", "a;b"),
    (r"a\sb", "a b"),
    (r"a\\b", "a\\b"),
    (r"a\rb", "a\rb"),
    (r"a\nb", "a\nb"),
    (r"say\shi\:\sok", "say hi; ok"),
    (r"a\\sb", "a\\sb"),
    (r"a\bc", "abc"),
    ("abc\\", "abc"),
    ("no escapes, at all", "no escapes, at all"),
    ("", ""),
])
def test_unescape_tag(raw, value):
    assert irc.unescape_tag(raw) == value


@pytest.mark.parametrize("value", ["", "plain", "a;b c\\d\re\nf", "ends with\\", r"\s stays", "x\\:y", ";;  \\\\"])
def test_escape_tag_round_trip(value):
    escaped = irc.escape_tag(value)
    assert not set(escaped) & set("; \r\n")
    assert irc.unescape_tag(escaped) == value


def test_escape_tag():
    assert irc.escape_tag("a;b c\\d\r\n") == r"a\:b\sc\\d\r\n"


def test_parse_line_tags_prefix_command_and_trailing():
    line = parse_line(r"@flag;empty=;;k=a\sb=c;badges=vip/1; :Nick!nick@nick.tmi.twitch.tv privmsg #Chan mid "
                      ":hi: you :) a:b\r\n")
    assert line.tags == {"flag": "", "empty": "", "k": "a b=c", "badges": "vip/1"}
    assert (line.prefix, line.nick, line.command) == ("Nick!nick@nick.tmi.twitch.tv", "Nick", "PRIVMSG")
    assert line.params == ["#Chan", "mid", "hi: you :) a:b"]


@pytest.mark.parametrize(("raw", "command", "params"), [
    (":tmi.twitch.tv ROOMSTATE #chan", "ROOMSTATE", ["#chan"]),
    (":tmi.twitch.tv 353 bot = #chan :bot", "353", ["bot", "=", "#chan", "bot"]),
    ("ping :tmi.twitch.tv\r\n", "PING", ["tmi.twitch.tv"]),
    ("PING", "PING", []),
    ("PRIVMSG #c :", "PRIVMSG", ["#c", ""]),
    ("PRIVMSG #c :a :b c: d ::", "PRIVMSG", ["#c", "a :b c: d ::"]),
    ("@k=v  :pre  CMD  a  b  :t", "CMD", ["a", "b", "t"]),
])
def test_parse_line_shapes(raw, command, params):
    line = parse_line(raw)
    assert (line.command, line.params) == (command, params)


@pytest.mark.parametrize("raw", [
    "", "\r\n", "   ", ":tmi.twitch.tv", ":tmi.twitch.tv  \r\n", "@a=b", "@a=b :tmi.twitch.tv",
])
def test_parse_line_without_command(raw):
    assert parse_line(raw) is None


def test_line_nick_and_param():
    line = parse_line(":alice!alice@alice.tmi.twitch.tv PRIVMSG #chan :hello there")
    assert line.nick == "alice" and line.param(0) == "#chan"
    assert line.param(-1) == "hello there" and line.param(-2) == "#chan"
    assert line.param(2) == "" and line.param(2, "x") == "x" and line.param(-3, "x") == "x"
    assert parse_line(":tmi.twitch.tv NOTICE * :Login authentication failed").nick == "tmi.twitch.tv"
    bare = parse_line("PING")
    assert bare.nick == "" and bare.param(0, "tmi.twitch.tv") == "tmi.twitch.tv" and bare.param(-1) == ""


@pytest.mark.parametrize(("value", "badges"), [
    ("broadcaster/1,subscriber/12", {"broadcaster": "1", "subscriber": "12"}),
    ("moderator/1,", {"moderator": "1"}),
    ("", {}),
    (None, {}),
])
def test_parse_badges(value, badges):
    assert irc.parse_badges(value) == badges


def test_parse_privmsg_fields():
    line = parse_line(PRIVMSG)
    msg = irc.parse_privmsg(line)
    assert msg == irc.ChatMessage(
        channel="#streamer", text="@Bob it's Celeste: a platformer", login="alice_42", user_id="58355428",
        display_name="Alice_42", message_id=MSG_ID, room_id="713936733", badges={"moderator": "1", "subscriber": "12"},
        sent_ts=1642696567751,
        reply_parent_msg_id=PARENT_ID, reply_parent_user_id="12345", reply_parent_login="bob",
        reply_parent_display_name="Bob", reply_parent_msg_body="what game is this?", tags=line.tags)
    assert msg.tags["client-nonce"] == "4b1f0c" and msg.tags["badge-info"] == "subscriber/14"


def test_parse_privmsg_display_name_falls_back_to_login():
    msg = chat("hi", {"display-name": ""}, nick="Alice", channel="#Chan")
    assert (msg.display_name, msg.login, msg.channel) == ("alice", "alice", "#chan")


def test_parse_privmsg_without_tags():
    msg = irc.parse_privmsg(parse_line(":Bob!bob@bob.tmi.twitch.tv PRIVMSG #Chan :hi there"))
    assert (msg.login, msg.display_name, msg.channel, msg.text) == ("bob", "bob", "#chan", "hi there")
    assert (msg.user_id, msg.message_id, msg.room_id, msg.badges, msg.sent_ts, msg.tags) == ("", "", "", {}, None, {})
    assert chat(tags={"tmi-sent-ts": "soon"}).sent_ts is None


@pytest.mark.parametrize("text", ["\x01ACTION waves\x01", "\x01ACTION waves"])
def test_parse_privmsg_action(text):
    msg = chat(text)
    assert msg.text == "* Alice waves"


@pytest.mark.parametrize("text", ["\x01VERSION\x01", "\x01PING 1700000000\x01", "\x01CLIENTINFO"])
def test_parse_privmsg_drops_other_ctcp(text):
    assert chat(text) is None


@pytest.mark.parametrize("raw", [
    ":tmi.twitch.tv NOTICE #chan :hello",
    "@msg-id=raid :tmi.twitch.tv USERNOTICE #chan :hello",
    ":alice!alice@alice.tmi.twitch.tv PRIVMSG #chan",
    "PING :tmi.twitch.tv",
])
def test_parse_privmsg_rejects_other_lines(raw):
    assert irc.parse_privmsg(parse_line(raw)) is None


def test_parse_usernotice_raid():
    event = irc.parse_usernotice(parse_line(RAID))
    assert event == irc.ChannelEvent(kind="raid", channel="#streamer", login="raider", display_name="Raider",
                                     params={"displayName": "Raider", "login": "raider", "viewerCount": "15"})
    assert event.viewer_count == 15


def test_parse_usernotice_resub():
    event = irc.parse_usernotice(parse_line(RESUB))
    assert (event.kind, event.login, event.params["cumulative-months"]) == ("resub", "ronni", "6")
    assert event.params["sub-plan-name"] == "Channel Subscription" and event.params["months"] == "0"
    assert not [key for key in event.params if key.startswith("msg-param-")]


def test_parse_usernotice_sub():
    event = notice("sub", {"cumulative-months": "1", "sub-plan": "Prime"})
    assert (event.kind, event.channel, event.viewer_count) == ("sub", "#chan", None)
    assert event.params == {"cumulative-months": "1", "sub-plan": "Prime"}
    assert (event.login, event.display_name) == ("gifter", "Gifter")


@pytest.mark.parametrize("kind", ["submysterygift", "anonsubmysterygift"])
def test_usernotice_community_gift(kind):
    event = notice(kind, {"mass-gift-count": "20", "community-gift-id": "77"})
    assert (event.kind, event.params["mass-gift-count"], event.params["community-gift-id"]) == (kind, "20", "77")


@pytest.mark.parametrize("kind", ["announcement", "bitsbadgetier", "unraid", "ritual", "primepaidupgrade", ""])
def test_parse_usernotice_ignores_other_kinds(kind):
    assert notice(kind, {"viewerCount": "5"}) is None


@pytest.mark.parametrize("raw", [
    "@msg-id=raid :tmi.twitch.tv PRIVMSG #chan :hi", "@msg-id=raid :tmi.twitch.tv USERNOTICE",
])
def test_parse_usernotice_rejects_other_lines(raw):
    assert irc.parse_usernotice(parse_line(raw)) is None


@pytest.mark.parametrize("value", [MSG_ID, MSG_ID.upper(), "885196DE-cb67-427A-baa8-82F9b0fcd05f"])
def test_is_message_id_accepts_uuids(value):
    assert irc.is_message_id(value) is True


@pytest.mark.parametrize("value", [
    None, 5, "", "local-1700000000", MSG_ID.replace("-", ""), f"{{{MSG_ID}}}", f" {MSG_ID}", MSG_ID[:-1], MSG_ID + "0",
    "g" + MSG_ID[1:], MSG_ID + "\n",
])
def test_is_message_id_rejects_others(value):
    assert irc.is_message_id(value) is False


@pytest.mark.parametrize(("value", "channel"), [
    ("Foo", "#foo"), ("#Foo", "#foo"), ("  #Foo_Bar99 ", "#foo_bar99"),
    ("", ""), ("#", ""), ("foo bar", ""), ("foo-bar", ""), ("a" * 26, ""), ("#foo,#bar", ""), ("foo\r\nJOIN #x", ""),
])
def test_normalize_channel(value, channel):
    assert irc.normalize_channel(value) == channel


@pytest.mark.parametrize(("value", "channels"), [
    ("Foo,#Bar, baz", ["#foo", "#bar", "#baz"]),
    (["Foo", "#bar", "BAR", "foo"], ["#foo", "#bar"]),
    (("zeta", "alpha", "#Zeta"), ["#zeta", "#alpha"]),
    ("zeta,alpha,#ZETA,,bad name,-x,ok", ["#zeta", "#alpha", "#ok"]),
    ("", []),
    (None, []),
])
def test_parse_channels(value, channels):
    assert irc.parse_channels(value) == channels


def test_clean_line():
    assert irc.clean_line("a\rb\nc\x00d\r\n") == "abcd"
    assert irc.clean_line("plain: text") == "plain: text"


def test_privmsg():
    assert irc.privmsg("#chan", "hello there") == "PRIVMSG #chan :hello there"
    assert irc.privmsg("#chan", "text", MSG_ID) == f"@reply-parent-msg-id={MSG_ID} PRIVMSG #chan :text"
    assert parse_line(irc.privmsg("#chan", "text", MSG_ID)).tags == {"reply-parent-msg-id": MSG_ID}


@pytest.mark.parametrize(("text", "reply_to"), [
    ("hi\r\nPRIVMSG #other :pwned", None),
    ("hi\nJOIN #other", MSG_ID),
    ("hi\x00\rQUIT :bye", None),
    ("hi", "x\r\nQUIT :bye"),
    ("hi", "a b;c"),
])
def test_privmsg_cannot_carry_a_second_command(text, reply_to):
    wire = irc.privmsg("#chan", text, reply_to)
    assert not set(wire) & {"\r", "\n", "\x00"}
    line = parse_line(wire)
    assert (line.command, line.params) == ("PRIVMSG", ["#chan", irc.clean_line(text)])
    assert line.tags == ({"reply-parent-msg-id": reply_to} if reply_to else {})


@pytest.mark.parametrize(("markdown", "plain"), [
    ("**bold** and __strong__", "bold and __strong__"),
    ("*italic* and _emphasis_", "italic and _emphasis_"),
    ("snake_case_name stays, so does 2*3", "snake_case_name stays, so does 2*3"),
    ("run `pip install x` first", "run pip install x first"),
    ("see [the docs](https://example.com/docs)", "see the docs (https://example.com/docs)"),
    ("![diagram](https://example.com/d.png)", "https://example.com/d.png"),
    ("# Title\n\nBody text", "Title Body text"),
    ("### Sub heading", "Sub heading"),
    ("> quoted\n> > nested", "quoted nested"),
    ("- one\n- two\n- three", "one / two / three."),
    ("* one\n+ two\n\u2022 three", "one / two / three."),
    ("1. first\n2) second", "first / second."),
    ("- one\n- two!", "one / two!"),
    ("- **bold** item\n- `code`", "bold item / code."),
    ("Options:\n- a\n- b\nPick one.", "Options: a / b. Pick one."),
    ("Run this:\n```bash\npip install x\n```\nThen restart.", "Run this: pip install x Then restart."),
    ("~~~\nx = a**2 + b**2\n~~~", "x = a**2 + b**2"),
    ("```\nunclosed fence", "unclosed fence"),
    ("above\n\n---\n\nbelow", "above below"),
])
def test_flatten(markdown, plain):
    assert irc.flatten(markdown) == plain


def test_flatten_is_one_line():
    flat = irc.flatten("# Heading\r\n\r\nPara one\rline two\n\n- a\n- b\n```\ncode\n```\n> quote\n")
    assert flat == "Heading Para one line two a / b. code quote"


def test_format_reply_short_text():
    assert irc.format_reply("Hello **world**, try `!hermes`.") == ["Hello world, try !hermes."]
    assert irc.format_reply("Top picks:\n- Celeste\n- Hades") == ["Top picks: Celeste / Hades."]
    assert irc.format_reply("Hello,\tworld.  How \xa0 are\u3000you?") == ["Hello, world. How are you?"]


def test_format_reply_chunks_on_word_boundaries():
    lines = irc.format_reply(WORDS, max_lines=6)
    assert len(lines) == 6 and all(len(line) <= irc.MAX_LINE for line in lines)
    assert " ".join(lines) == WORDS
    assert all(len(a) + 1 + len(b.split(" ", 1)[0]) > irc.MAX_LINE for a, b in itertools.pairwise(lines))


def test_format_reply_hard_splits_a_long_word():
    word = "x" * 1000
    assert irc.format_reply(word) == ["x" * 480, "x" * 480, "x" * 40]
    assert irc.format_reply(word, max_lines=2) == ["x" * 480, "x" * 477 + "..."]


@pytest.mark.parametrize("max_lines", [1, 2, 3, 5])
def test_format_reply_cuts_to_max_lines(max_lines):
    lines = irc.format_reply(WORDS, max_lines=max_lines)
    assert len(lines) == max_lines and all(len(line) <= irc.MAX_LINE for line in lines)
    assert lines[-1].endswith("...") and WORDS.startswith(" ".join(lines)[:-3])


def test_format_reply_drops_control_characters():
    lines = irc.format_reply(CONTROLS)
    assert len(lines) == 1 and control_chars(lines) == []
    assert all(word in lines[0] for word in ("one", "two", "waves", "red", "three", "four", "five", "six", "seven"))
    many = irc.format_reply(CONTROLS * 100, max_lines=10)
    assert len(many) == 10 and many[-1].endswith("...") and control_chars(many) == []


@pytest.mark.parametrize("text", [
    "/ban someone", ".timeout x 600", "//ban someone", "./me waves", "/.ban someone", "> /ban someone",
    "`/ban someone`", "1. /ban someone\n2. then leave",
    "/ /ban someone", ". .timeout x",
    "\u200b/ban someone", "\ufeff.timeout x 600", "\u2060./me waves",
    "\u200b /ban someone", " \ufeff.timeout x 600",
    "\u3164/ban someone", "\u034f.timeout x 600",
    "\u200c/ban someone", "\u200f/me waves",
])
def test_format_reply_defangs_chat_commands(text):
    lines = irc.format_reply(text)
    assert lines and not [line for line in lines if line.lstrip().startswith(("/", "."))]


@pytest.mark.parametrize("command", ["/ban someone", ".timeout x 600"])
def test_format_reply_defangs_every_line(command):
    lines = irc.format_reply(f"{FILLER} {command}")
    assert len(lines) == 2 and lines[0] == FILLER and not lines[1].startswith(("/", "."))


def opening(line: str) -> str:
    """``line`` from its first visible character, the one Twitch reads as a command prefix."""
    return line.lstrip("".join({ch for ch in line if ch.isspace() or ch in irc.KEEP_FORMAT or not irc.visible(ch)}))


@pytest.mark.parametrize("cut", [";" * 479, ("-; " * 200)[:479], ("..; " * 200)[:479], "‍" * 479])
@pytest.mark.parametrize("max_lines", [1, 2])
def test_a_cut_reply_never_opens_a_line_with_its_dots(cut, max_lines):
    lead = FILLER + " " if max_lines == 2 else ""
    lines = irc.format_reply(f"{lead}{cut} and the rest of the answer", max_lines=max_lines)
    assert len(lines) == max_lines and all(len(line) <= irc.MAX_LINE for line in lines)
    assert not [line for line in lines if opening(line).startswith(("/", "."))], lines


def test_format_reply_keeps_persian_zwnj():
    # Half-space in می خواهم (Persian "I want"): U+200C must survive byte-identical.
    text = "من می\u200cخواهم بازی کنم"
    assert irc.format_reply(text) == [text]


def test_format_reply_keeps_arabic_joiners():
    text = "مرحبا\u200cبالعالم و\u200dأهلا"
    assert irc.format_reply(text) == [text]


def test_format_reply_keeps_zwj_emoji_sequences():
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466"
    flag = "\U0001f1ee\U0001f1f7"
    skin = "\U0001f44d\U0001f3fb"
    text = f"hi {family} {flag} {skin}"
    assert irc.format_reply(text) == [text]


def test_format_reply_keeps_leading_rlm_before_persian():
    text = "\u200fسلام دوستان"
    assert irc.format_reply(text) == [text]


def test_format_reply_keeps_mixed_bidi_marks():
    text = "score\u200e ۱۲\u200f و next"
    assert irc.format_reply(text) == [text]


def test_format_reply_chunks_long_persian_on_word_boundaries():
    word = "کلمه\u200cبلند"
    text = " ".join([word] * 80)
    lines = irc.format_reply(text, max_lines=6)
    assert 1 < len(lines) <= 6
    assert all(len(line) <= irc.MAX_LINE for line in lines)
    assert "\u200c" in "".join(lines)
    assert lines[-1].endswith("...") or " ".join(lines).startswith(word)


@pytest.mark.parametrize("text", ["", "   ", "\n \r\n\t", "```\n```"])
def test_format_reply_empty(text):
    assert irc.format_reply(text) == []


@pytest.mark.parametrize(("text", "expected"), [
    ("2**10 = 1024 and 3**2 = 9", "2**10 = 1024 and 3**2 = 9"),
    ("2 * 3 * 4 = 24", "2 * 3 * 4 = 24"),
    ("Run rm *.pyc then ls *.py", "Run rm *.pyc then ls *.py"),
    ("Define `__init__` first", "Define __init__ first"),
    ("See https://x.io/__init__.py and https://e.com/_a_", "See https://x.io/__init__.py and https://e.com/_a_"),
    ("snake_case_name and file_v2_final.txt stay", "snake_case_name and file_v2_final.txt stay"),
    ("**Bold**, *italic*, _under_ and __dunder__ words", "Bold, italic, _under_ and __dunder__ words"),
    ("src/__init__.py is empty", "src/__init__.py is empty"),
    ("if __name__ == '__main__': run()", "if __name__ == '__main__': run()"),
    ("see example.com/_a_ and the __pycache__ folder", "see example.com/_a_ and the __pycache__ folder"),
    ("globs like a/**/b/**/c and src/*/x/*", "globs like a/**/b/**/c and src/*/x/*"),
    ("```ls -la ~/.config```", "ls -la ~/.config"),
    ("Run:\n```pip install x```\nthen *restart* it", "Run: pip install x then restart it"),
    ("```python\nprint(a**2)\n```\ndone", "print(a**2) done"),
    ("[docs](https://a.b/c_d_e) and ![img](https://i.example/p.png)",
     "docs (https://a.b/c_d_e) and https://i.example/p.png"),
])
def test_plain_text_survives_markdown_stripping(text, expected):
    assert irc.format_reply(text) == [expected]


@pytest.mark.parametrize(("text", "expected"), [
    ("https://a.b![x](c)", "https://a.bc"),  # the image's link is set aside inside the link it touches
    ("```\nopen ` https://x.y/z ` now\n```", "open ` https://x.y/z ` now"),  # a link inside a code span
])
def test_text_set_aside_inside_other_set_aside_text_comes_back_whole(text, expected):
    assert irc.format_reply(text) == [expected]


@pytest.mark.parametrize("text", ["**a " * 40000, "[" * 40000, "![" * 20000, "|" * 40000 + "x", "|" * 40000])
def test_formatting_stays_quick_on_hostile_text(text):
    started = time.perf_counter()
    assert len(irc.format_reply(text, max_lines=3)) <= 3
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize("text", ["**a " * 40000, " **a" * 5000, " *a" * 7000, " **a *b" * 2800,
                                  " **a" * 2400 + "b" * 9600])
def test_formatting_stays_quick_on_hostile_text_at_the_line_ceiling(text):
    # Markers that never close: each one used to scan the rest of the line, quadratic in the 19,200 characters
    # that max_reply_lines' ceiling lets through.
    started = time.perf_counter()
    assert len(irc.format_reply(text, max_lines=sender.MAX_LINES)) <= sender.MAX_LINES
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(("line", "separator"), [
    ("|---|:--:|", True), ("--- | ---", True), ("| - |", True), ("|", False), ("|-", False), ("a | b", False)])
def test_table_separators(line, separator):
    assert bool(irc._TABLE_SEP_RE.match(line)) is separator
