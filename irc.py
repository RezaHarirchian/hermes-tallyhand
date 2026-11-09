"""Twitch IRC wire format: line parsing, Twitch message shapes and outbound line building. Pure, no I/O."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_UNESCAPES = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}
_ESCAPES = {";": "\\:", " ": "\\s", "\\": "\\\\", "\r": "\\r", "\n": "\\n"}
_MSG_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SPACE_RE = re.compile(r"\s+")
_LOGIN_RE = re.compile(r"[a-z0-9_]{1,25}")
# Hiding tools clients normalize away (not the script/bidi marks in KEEP_FORMAT).
_FILLERS = frozenset("\u034f\u115f\u1160\u17b4\u17b5\u3164\uffa0")
# Persian half-space, emoji ZWJ, and bidi marks: kept in body and inbound text, invisible on a command lead.
KEEP_FORMAT = frozenset("\u200c\u200d\u200e\u200f")  # ZWNJ, ZWJ, LRM, RLM

EVENT_KINDS = frozenset({"raid", "sub", "resub", "subgift", "anonsubgift", "submysterygift", "anonsubmysterygift"})
MAX_LINE = 480

# The bundled IRC adapter's markdown rules, changed so plain text survives: code spans and URLs are set aside first, an
# asterisk must hug its text and touch no word character or slash ("2**10", "*.py" and "a/**/b" stay as written), and
# underscores stay, since they cannot be told from names like __init__ or snake_case. Fences are handled per line.
_IMAGE_RE = re.compile(r"!\[([^\]\n]{0,300})\]\(([^)\n]{1,1000})\)")
_LINK_RE = re.compile(r"\[([^\]\n]{1,300})\]\(([^)\n]{1,1000})\)")
_CODE_SPAN_RE = re.compile(r"(`{1,3})(.+?)\1")
_URL_RE = re.compile(r"https?://\S+")
_EMPHASIS_RULES = tuple(re.compile(p) for p in (
    r"(?<![\w*/])\*\*(?=[^\s/])(.+?)(?<=[^\s/])\*\*(?![\w*/])",
    r"(?<![\w*/])\*(?=[^\s/])(.+?)(?<=[^\s/])\*(?![\w*/])",
))
# Where each rule's closing marker can stand. No match ends after the last one, so a rule runs only up to it: markers
# that never close would otherwise each scan the rest of the line, quadratic in its length.
_EMPHASIS_CLOSERS = tuple(re.compile(p) for p in (r"(?<=[^\s/])\*\*(?![\w*/])", r"(?<=[^\s/])\*(?![\w*/])"))
_HELD_RE = re.compile("\x00(\\d+)\x00")
_FENCE_LINE_RE = re.compile(r"\s*(```|~~~)[\w+.#-]*\s*")
_LIST_RE = re.compile(r"^\s*(?:[-*+•]|\d{1,3}[.)])\s+(.*)$")
_HEADING_RE = re.compile(r"^#{1,6}\s+")
_QUOTE_RE = re.compile(r"^(?:>\s?)+")
_RULE_RE = re.compile(r"^([-*_])(\s*\1){2,}$")
_TABLE_SEP_RE = re.compile(r"^[\s:|-][\s:-]*\|[\s:|-]*$")
_AT_NAME_RE = re.compile(r"@[A-Za-z0-9_]{1,25}\b")
# Em and en dashes only; ASCII hyphen-minus is left alone.
_EM_DASH_RE = re.compile(r"\s*\u2014\s*")
_EN_DASH_RE = re.compile(r"\s*\u2013\s*")


def unescape_tag(value: str) -> str:
    """IRCv3 tag-value unescaping. An unknown escape keeps its character; a trailing lone backslash is dropped."""
    if "\\" not in value:
        return value
    out, i = [], 0
    while i < len(value):
        if value[i] != "\\":
            out.append(value[i])
            i += 1
            continue
        if i + 1 < len(value):
            out.append(_UNESCAPES.get(value[i + 1], value[i + 1]))
        i += 2
    return "".join(out)


def escape_tag(value: str) -> str:
    return "".join(_ESCAPES.get(ch, ch) for ch in value)


@dataclass(slots=True)
class Line:
    """One parsed IRC line. ``params`` includes the trailing parameter when present."""

    command: str
    params: list[str] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)
    prefix: str = ""

    @property
    def nick(self) -> str:
        return self.prefix.split("!", 1)[0]

    def param(self, index: int, default: str = "") -> str:
        return self.params[index] if -len(self.params) <= index < len(self.params) else default


def parse_line(raw: str) -> Line | None:
    """Parse one IRC line (IRCv3 tags, prefix, command, params, trailing); None when there is no command."""
    line = raw.rstrip("\r\n")
    tags: dict[str, str] = {}
    if line.startswith("@"):
        tag_part, _, line = line[1:].partition(" ")
        for item in tag_part.split(";"):
            if item:
                key, _, value = item.partition("=")
                tags[key] = unescape_tag(value)
        line = line.lstrip(" ")
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
        line = line.lstrip(" ")
    trailing = None
    if (idx := line.find(" :")) != -1:
        line, trailing = line[:idx], line[idx + 2:]
    parts = line.split()
    if not parts:
        return None
    params = parts[1:] + ([trailing] if trailing is not None else [])
    return Line(command=parts[0].upper(), params=params, tags=tags, prefix=prefix)


def parse_badges(value: str | None) -> dict[str, str]:
    """``broadcaster/1,subscriber/12`` -> ``{"broadcaster": "1", "subscriber": "12"}``."""
    badges: dict[str, str] = {}
    for item in (value or "").split(","):
        if item:
            name, _, version = item.partition("/")
            badges[name] = version
    return badges


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


@dataclass(slots=True)
class ChatMessage:
    """A Twitch PRIVMSG reduced to what the adapter uses; ``tags`` keeps the raw tags."""

    channel: str
    text: str
    login: str
    user_id: str
    display_name: str
    message_id: str
    room_id: str
    badges: dict[str, str]
    sent_ts: int | None
    reply_parent_msg_id: str
    reply_parent_user_id: str
    reply_parent_login: str
    reply_parent_display_name: str
    reply_parent_msg_body: str
    tags: dict[str, str]


def parse_privmsg(line: Line) -> ChatMessage | None:
    """PRIVMSG -> ChatMessage. CTCP ACTION (/me) becomes ``* name text``; any other CTCP returns None."""
    if line.command != "PRIVMSG" or len(line.params) < 2:
        return None
    tags = line.tags
    login = line.nick.lower()
    display = tags.get("display-name") or login
    text = line.params[-1]
    if text.startswith("\x01"):
        body = text[1:-1] if len(text) > 1 and text.endswith("\x01") else text[1:]
        verb, _, rest = body.partition(" ")
        if verb != "ACTION":
            return None
        text = f"* {display} {rest}".rstrip()
    return ChatMessage(
        channel=line.params[0].lower(), text=text, login=login, user_id=tags.get("user-id", ""),
        display_name=display, message_id=tags.get("id", ""), room_id=tags.get("room-id", ""),
        badges=parse_badges(tags.get("badges")), sent_ts=_int_or_none(tags.get("tmi-sent-ts")),
        reply_parent_msg_id=tags.get("reply-parent-msg-id", ""),
        reply_parent_user_id=tags.get("reply-parent-user-id", ""),
        reply_parent_login=tags.get("reply-parent-user-login", "").lower(),
        reply_parent_display_name=tags.get("reply-parent-display-name", ""),
        reply_parent_msg_body=tags.get("reply-parent-msg-body", ""),
        tags=dict(tags))


@dataclass(slots=True)
class ChannelEvent:
    """A USERNOTICE the bot may thank: a raid, a sub or resub, or gift subs (one or a community gift)."""

    kind: str
    channel: str
    login: str
    display_name: str
    params: dict[str, str]

    @property
    def viewer_count(self) -> int | None:
        return _int_or_none(self.params.get("viewerCount"))


def parse_usernotice(line: Line) -> ChannelEvent | None:
    """USERNOTICE -> ChannelEvent for the kinds in EVENT_KINDS, else None. ``params`` drops the msg-param- prefix."""
    kind = line.tags.get("msg-id", "")
    if line.command != "USERNOTICE" or kind not in EVENT_KINDS or not line.params:
        return None
    tags = line.tags
    login = tags.get("login", "").lower()
    return ChannelEvent(
        kind=kind, channel=line.params[0].lower(), login=login, display_name=tags.get("display-name") or login,
        params={k[len("msg-param-"):]: v for k, v in tags.items() if k.startswith("msg-param-")})


# outbound

def is_message_id(value: object) -> bool:
    """True for a Twitch chat message id (a UUID), the only thing a reply tag may carry."""
    return isinstance(value, str) and bool(_MSG_ID_RE.fullmatch(value))


def normalize_channel(value: str) -> str:
    """``Foo`` / ``#Foo`` -> ``#foo``; empty string when the result is not a valid Twitch login."""
    login = (value or "").strip().lstrip("#").lower()
    return f"#{login}" if _LOGIN_RE.fullmatch(login) else ""


def parse_channels(value: object) -> list[str]:
    """Comma-separated (or list) channel logins -> ``["#a", "#b"]``, lowercased, deduplicated, order kept."""
    items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    seen: dict[str, None] = {}
    for item in items:
        if channel := normalize_channel(str(item)):
            seen.setdefault(channel, None)
    return list(seen)


def clean_line(text: str) -> str:
    """Drop CR, LF and NUL so a line can never carry a second IRC command."""
    return text.replace("\r", "").replace("\n", "").replace("\x00", "")


def strip_controls(text: str) -> str:
    return _CONTROL_RE.sub("", text)


def privmsg(channel: str, text: str, reply_to: str | None = None) -> str:
    """Build a PRIVMSG line; ``reply_to`` (a message id) adds the reply-parent tag."""
    tag = f"@reply-parent-msg-id={escape_tag(reply_to)} " if reply_to else ""
    return f"{tag}PRIVMSG {channel} :{clean_line(text)}"


def _restore(text: str, held: list[str]) -> str:
    """``text`` with its held spans put back, including one held inside another (a span holds only earlier ones)."""
    return _HELD_RE.sub(lambda m: _restore(held[int(m.group(1))], held), text)


def _strip_inline(text: str) -> str:
    held: list[str] = []

    def hold(value: str) -> str:
        held.append(value)
        return f"\x00{len(held) - 1}\x00"

    text = _IMAGE_RE.sub(lambda m: hold(m.group(2)), text.replace("\x00", ""))
    text = _LINK_RE.sub(lambda m: f"{m.group(1)} ({hold(m.group(2))})", text)
    text = _CODE_SPAN_RE.sub(lambda m: hold(m.group(2)), text)
    text = _URL_RE.sub(lambda m: hold(m.group(0)), text)
    for pattern, closer in zip(_EMPHASIS_RULES, _EMPHASIS_CLOSERS, strict=True):
        end = max((match.end() for match in closer.finditer(text)), default=0)
        text = pattern.sub(r"\1", text[:end]) + text[end:]
    return _restore(text, held)


def flatten(text: str) -> str:
    """A markdown reply as one plain-text run: fence markers dropped, headings and quotes unwrapped, list
    items joined with " / " into one sentence, blocks joined with spaces."""
    blocks: list[str] = []
    items: list[str] = []
    in_fence = False

    def flush() -> None:
        if items:
            joined = " / ".join(items)
            blocks.append(joined if joined[-1] in ".!?:;" else joined + ".")
            items.clear()

    for raw in text.splitlines():
        if _FENCE_LINE_RE.fullmatch(raw):
            in_fence = not in_fence
            continue
        line = raw.strip()
        if not line:
            continue
        if in_fence:
            flush()
            blocks.append(line)
            continue
        if _RULE_RE.match(line) or _TABLE_SEP_RE.match(line):
            continue
        if match := _LIST_RE.match(line):
            if item := _strip_inline(match.group(1)).strip():
                items.append(item)
            continue
        flush()
        line = _HEADING_RE.sub("", _QUOTE_RE.sub("", line)).strip()
        if line.startswith("|") and line.endswith("|"):
            line = " / ".join(cell.strip() for cell in line.strip("|").split("|") if cell.strip())
        blocks.append(_strip_inline(line).strip())
    flush()
    return " ".join(block for block in blocks if block)


def fit(text: str, limit: int) -> str:
    """Longest prefix of ``text`` within ``limit`` characters, cut on a word boundary when one exists."""
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit + 1)
    return text[:cut if cut > 0 else limit].rstrip()


def split_words(text: str, limit: int = MAX_LINE) -> list[str]:
    """Split ``text`` into chunks of at most ``limit`` characters on word boundaries (long words are cut)."""
    chunks: list[str] = []
    text = text.strip()
    while text:
        chunk = fit(text, limit)
        chunks.append(chunk)
        text = text[len(chunk):].lstrip()
    return chunks


def visible(ch: str) -> bool:
    """True when ``ch`` should stay in chat text. Hiding tools (tag characters, ZWSP, BOM, fillers) are out;
    ZWNJ, ZWJ, LRM and RLM stay for Persian/Arabic spelling and emoji sequences."""
    if ch in KEEP_FORMAT:
        return True
    return ch not in _FILLERS and not 0xE0000 <= ord(ch) <= 0xE007F and unicodedata.category(ch) != "Cf"


def _defang(line: str) -> str:
    # Twitch treats a leading "/" or "." as a chat command. Only the lead is inspected: walk while the character
    # is whitespace, "/" or ".", a kept format mark, or otherwise not visible. If that run held a "/" or ".",
    # drop the whole run; never rewrite the rest of the line (ZWNJ and ZWJ in the body must survive).
    i, saw_command = 0, False
    while i < len(line):
        ch = line[i]
        if ch in "/.":
            saw_command = True
            i += 1
            continue
        if ch.isspace() or ch in KEEP_FORMAT or not visible(ch):
            i += 1
            continue
        break
    return line[i:] if saw_command else line


def normalize_dashes(text: str) -> str:
    """Replace em and en dashes with a comma or an ASCII hyphen. URLs, backtick spans and @names are left alone;
    ZWNJ, ZWJ, LRM and RLM are untouched (this never runs the body through ``visible``)."""
    held: list[str] = []

    def hold(match: re.Match[str]) -> str:
        held.append(match.group(0))
        return f"\x00{len(held) - 1}\x00"

    text = text.replace("\x00", "")
    text = _URL_RE.sub(hold, text)
    text = _CODE_SPAN_RE.sub(hold, text)
    text = _AT_NAME_RE.sub(hold, text)

    def em_dash(match: re.Match[str]) -> str:
        return ", " if match.group(0) != "\u2014" else "-"

    def en_dash(match: re.Match[str]) -> str:
        return "-" if match.group(0) == "\u2013" else " - "

    text = _EM_DASH_RE.sub(em_dash, text)
    text = _EN_DASH_RE.sub(en_dash, text)
    return _restore(text, held)


def format_reply(text: str, *, limit: int = MAX_LINE, max_lines: int = 3) -> list[str]:
    """Chat lines for a model reply: plain text, chunked on word boundaries, at most ``max_lines`` lines with
    "..." closing the last one when the reply was cut. Every line is free of CR, LF and NUL."""
    # Only max_lines lines are ever posted: a reply many times longer is cut first, so the formatting work stays
    # small however long (or hostile) the text is.
    flat = clean_line(strip_controls(_SPACE_RE.sub(" ", flatten(text[:max(1, max_lines) * limit * 2])))).strip()
    flat = normalize_dashes(flat)
    lines = [line for line in (_defang(chunk) for chunk in split_words(flat, limit)) if line]
    if len(lines) > max(1, max_lines):
        lines = lines[:max(1, max_lines)]
        cut = fit(lines[-1], limit - 3)
        # The dots never open a line: when trimming leaves nothing visible before them, the cut stays whole.
        lines[-1] = next(closing for closing in (cut.rstrip(" .,;:-") + "...", cut + "...", cut)
                         if _defang(closing) == closing)
    return lines
