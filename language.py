"""Stream-language replies: map Twitch language codes, resolve the reply setting, and build the short guidance
lines that go with every model turn. Fixed command and event strings stay English (see the README)."""

from __future__ import annotations

import re
import unicodedata

# Twitch's stream language list has no Persian, so a Persian channel reports "other" or an unrelated language. The
# marker table below lets the title or tags name the real language instead. "other" is not a name here: the
# adapter falls back to the question's language.
LANGUAGE_NAMES: dict[str, str] = {
    "ar": "Arabic",
    "as": "Assamese",
    "bg": "Bulgarian",
    "bn": "Bengali",
    "ca": "Catalan",
    "cs": "Czech",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "es": "Spanish",
    "fa": "Persian",
    "fi": "Finnish",
    "fr": "French",
    "he": "Hebrew",
    "hi": "Hindi",
    "hu": "Hungarian",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "ms": "Malay",
    "nl": "Dutch",
    "no": "Norwegian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sk": "Slovak",
    "sv": "Swedish",
    "th": "Thai",
    "tl": "Tagalog",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "uz": "Uzbek",
    "vi": "Vietnamese",
    "zh": "Chinese",
    "sr": "Serbian",
    "hr": "Croatian",
    "sl": "Slovenian",
    "bs": "Bosnian",
    "sq": "Albanian",
    "mk": "Macedonian",
    "et": "Estonian",
    "lv": "Latvian",
    "lt": "Lithuanian",
    "is": "Icelandic",
    "ga": "Irish",
    "cy": "Welsh",
    "eu": "Basque",
    "gl": "Galician",
    "ka": "Georgian",
    "hy": "Armenian",
    "az": "Azerbaijani",
    "kk": "Kazakh",
    "ta": "Tamil",
    "te": "Telugu",
    "ml": "Malayalam",
    "mr": "Marathi",
    "gu": "Gujarati",
    "kn": "Kannada",
    "pa": "Punjabi",
    "ne": "Nepali",
    "si": "Sinhala",
    "sw": "Swahili",
    "af": "Afrikaans",
}

# code -> (native name, extra aliases). The English name lives in LANGUAGE_NAMES. Names and aliases match as whole
# words; bare two-letter codes never count (too many are ordinary words: it, no, he, as, id, hi, is). A code
# counts only written explicitly: in brackets or parentheses, after a hash, or after lang:/language:.
MARKERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "ar": ("العربية", ("arabic",)),
    "as": ("অসমীয়া", ("assamese",)),
    "bg": ("български", ("bulgarian",)),
    "bn": ("বাংলা", ("bengali", "bangla")),
    "ca": ("català", ("catalan",)),
    "cs": ("čeština", ("czech",)),
    "da": ("dansk", ("danish",)),
    "de": ("Deutsch", ("german", "deutsch")),
    "el": ("Ελληνικά", ("greek",)),
    "en": ("English", ("english",)),
    "es": ("Español", ("spanish", "castellano")),
    "fa": ("فارسی", ("persian", "farsi", "پارسی")),
    "fi": ("Suomi", ("finnish", "suomi")),
    "fr": ("Français", ("french",)),
    "he": ("עברית", ("hebrew",)),
    "hi": ("हिन्दी", ("hindi",)),
    "hu": ("Magyar", ("hungarian", "magyar")),
    "id": ("Bahasa Indonesia", ("indonesian",)),
    "it": ("Italiano", ("italian",)),
    "ja": ("日本語", ("japanese",)),
    "ko": ("한국어", ("korean",)),
    "ms": ("Melayu", ("malay", "melayu")),
    "nl": ("Nederlands", ("dutch", "nederlands")),
    "no": ("Norsk", ("norwegian", "norsk")),
    "pl": ("Polski", ("polish",)),
    "pt": ("Português", ("portuguese",)),
    "ro": ("Română", ("romanian",)),
    "ru": ("Русский", ("russian",)),
    "sk": ("Slovenčina", ("slovak",)),
    "sv": ("Svenska", ("swedish", "svenska")),
    "th": ("ไทย", ("thai",)),
    "tl": ("Tagalog", ("tagalog", "filipino")),
    "tr": ("Türkçe", ("turkish", "turkce")),
    "uk": ("Українська", ("ukrainian",)),
    "ur": ("اردو", ("urdu",)),
    "uz": ("O'zbekcha", ("uzbek",)),
    "vi": ("Tiếng Việt", ("vietnamese",)),
    "zh": ("中文", ("chinese", "mandarin")),
    "sr": ("Српски", ("serbian",)),
    "hr": ("Hrvatski", ("croatian",)),
    "sl": ("Slovenščina", ("slovenian",)),
    "bs": ("Bosanski", ("bosnian",)),
    "sq": ("Shqip", ("albanian", "shqip")),
    "mk": ("Македонски", ("macedonian",)),
    "et": ("Eesti", ("estonian", "eesti")),
    "lv": ("Latviešu", ("latvian",)),
    "lt": ("Lietuvių", ("lithuanian",)),
    "is": ("Íslenska", ("icelandic",)),
    "ga": ("Gaeilge", ("irish", "gaeilge")),
    "cy": ("Cymraeg", ("welsh", "cymraeg")),
    "eu": ("Euskara", ("basque", "euskara")),
    "gl": ("Galego", ("galician", "galego")),
    "ka": ("ქართული", ("georgian",)),
    "hy": ("Հայերեն", ("armenian",)),
    "az": ("Azərbaycanca", ("azerbaijani", "azeri")),
    "kk": ("Қазақша", ("kazakh",)),
    "ta": ("தமிழ்", ("tamil",)),
    "te": ("తెలుగు", ("telugu",)),
    "ml": ("മലയാളം", ("malayalam",)),
    "mr": ("मराठी", ("marathi",)),
    "gu": ("ગુજરાતી", ("gujarati",)),
    "kn": ("ಕನ್ನಡ", ("kannada",)),
    "pa": ("ਪੰਜਾਬੀ", ("punjabi",)),
    "ne": ("नेपाली", ("nepali",)),
    "si": ("සිංහල", ("sinhala",)),
    "sw": ("Kiswahili", ("swahili", "kiswahili")),
    "af": ("Afrikaans", ("afrikaans",)),
}

CHANNEL, VIEWER = "channel", "viewer"
MODES = (CHANNEL, VIEWER)
_CODE_RE = re.compile(r"^[a-z]{2}$")

# Short on purpose: these lines ride every turn.
_VOICE = ("Write like a regular chatter of that language: casual chat register, plain text, no markdown or lists, "
          "ASCII hyphens only, no emojis unless the asker used one, within the chat line limit.")


def parse_reply_language(raw: object | None) -> str | None:
    """``channel``, ``viewer``, or a two-letter ISO 639-1 code; None when the value is not one of those."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    if text in MODES or _CODE_RE.fullmatch(text):
        return text
    return None


def language_name(code: str | None) -> str | None:
    """English name for a Twitch (or fixed) language code; None for missing, ``other``, or unknown codes."""
    if not code:
        return None
    return LANGUAGE_NAMES.get(code.strip().lower())


def _fold(text: str) -> str:
    """Case-folded text for marker matching: compatibility-normalised, with the Turkish dotted capital I and the
    Persian and emoji joiners mapped to plain separators, and apostrophes dropped so O'zbekcha stays one word."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    folded = folded.replace("i\u0307", "i")
    folded = folded.replace("'", "").replace("\u2019", "")
    return folded.replace("\u200c", " ").replace("\u200d", " ")


# Aliases that are not an English language name or an ordinary English word. These stay strong, like the
# native names that differ from the English name and the explicit code forms. Every other alias is an English
# word (arabic, polish, mandarin, filipino, georgian, azeri, and each alias that repeats its English name) and
# counts as weak.
STRONG_ALIASES = frozenset({
    "farsi", "bangla", "deutsch", "castellano", "suomi", "magyar", "melayu", "nederlands", "norsk",
    "svenska", "turkce", "shqip", "eesti", "gaeilge", "cymraeg", "euskara", "galego", "kiswahili", "پارسی",
})


def _word_indexes() -> tuple[dict[str, str], dict[str, str]]:
    """Normalised single word -> code, split into strong and weak. Built once; the table above is the input."""
    strong: dict[str, str] = {}
    weak: dict[str, str] = {}
    for code, (native, aliases) in MARKERS.items():
        norm = _fold(native)
        if " " not in norm and norm != _fold(LANGUAGE_NAMES[code]):  # English, Tagalog, Afrikaans stay weak
            strong.setdefault(norm, code)
        for alias in aliases:
            (strong if _fold(alias) in STRONG_ALIASES else weak).setdefault(_fold(alias), code)
        weak.setdefault(_fold(LANGUAGE_NAMES[code]), code)
    return strong, weak


_STRONG_WORDS, _WEAK_WORDS = _word_indexes()

# Combining marks (vowel signs and the like) belong to the preceding letter; without them Indic words shatter.
_MARKS = ("\u0300-\u036f\u0483-\u0489\u0591-\u05bd\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed"
          "\u0900-\u0903\u093a-\u094f\u0951-\u0957\u0962-\u0963"
          "\u0981-\u0983\u09bc-\u09c4\u09c7-\u09c8\u09cb-\u09cd\u09d7\u09e2-\u09e3"
          "\u0a01-\u0a03\u0a3c\u0a3e-\u0a42\u0a47-\u0a48\u0a4b-\u0a4d\u0a51\u0a70-\u0a71\u0a75"
          "\u0a81-\u0a83\u0abc-\u0ac5\u0ac7-\u0ac9\u0acb-\u0acd\u0ae2-\u0ae3"
          "\u0b82-\u0b83\u0bbe-\u0bc2\u0bc6-\u0bc8\u0bca-\u0bcd\u0bd7"
          "\u0c00-\u0c04\u0c3e-\u0c44\u0c46-\u0c48\u0c4a-\u0c4d\u0c55-\u0c56\u0c62-\u0c63"
          "\u0c81-\u0c83\u0cbc-\u0cc4\u0cc6-\u0cc8\u0cca-\u0ccd\u0cd5-\u0cd6\u0ce2-\u0ce3"
          "\u0d00-\u0d03\u0d3b-\u0d3c\u0d3e-\u0d44\u0d46-\u0d48\u0d4a-\u0d4d\u0d57\u0d62-\u0d63"
          "\u0d82-\u0d83\u0dca-\u0ddf\u0df2-\u0df3"
          "\u0e31\u0e34-\u0e3a\u0e47-\u0e4e\u20d0-\u20ff\ufe20-\ufe2f")
_WORD_RE = re.compile(r"[^\W_](?:[^\W_]|[" + _MARKS + r"])*")
_BRACKET_CODE_RE = re.compile(r"[\[(]([a-z]{2})[\])]")
_HASH_CODE_RE = re.compile(r"(?<![^\W_])#([a-z]{2})(?![^\W_])")
_LANG_CODE_RE = re.compile(r"(?<![^\W_])(?:lang|language)\s*[:=\-]\s*([a-z]{2})(?![^\W_])")


def _explicit_codes(norm: str) -> set[str]:
    """Codes written as [xx], (xx), #xx, or lang:/language: xx. Bare two-letter codes never count."""
    found: set[str] = set()
    for pattern in (_BRACKET_CODE_RE, _HASH_CODE_RE, _LANG_CODE_RE):
        found.update(match for match in pattern.findall(norm) if match in MARKERS)
    return found


def strong_in(text: str) -> set[str]:
    """Strong marker codes named in one title or tag: explicit codes, native names other than a plain English name,
    and non-English aliases."""
    norm = _fold(text)
    found = {code for word in _WORD_RE.findall(norm) if (code := _STRONG_WORDS.get(word)) is not None}
    found.update(_explicit_codes(norm))
    for code, (native, _aliases) in MARKERS.items():
        norm_native = _fold(native)
        if " " in norm_native and re.search(
                r"(?<![^\W_])" + r"\s+".join(map(re.escape, norm_native.split())) + r"(?![^\W_])", norm):
            found.add(code)
    return found


def weak_in(text: str) -> set[str]:
    """Weak marker codes named in one title or tag: the English language names and English-word aliases."""
    return {code for word in _WORD_RE.findall(_fold(text)) if (code := _WEAK_WORDS.get(word)) is not None}


def markers_in(text: str) -> set[str]:
    """Marker codes of either class named in one title or tag. Whole words only; a bare code never counts."""
    return strong_in(text) | weak_in(text)


def detect_markers(title: str | None, tags: object = ()) -> set[str]:
    """Marker codes named across the title and tags together. Two or more distinct codes means ambiguous."""
    found = markers_in(title or "")
    for tag in tags or ():
        found.update(markers_in(str(tag)))
    return found


def _detect_split(title: str | None, tags: object) -> tuple[set[str], set[str], set[str], set[str]]:
    """(title strong, tag strong, title weak, tag weak) hits, so the source stays exact."""
    title_text = title or ""
    title_strong, title_weak = strong_in(title_text), weak_in(title_text)
    tag_strong, tag_weak = set(), set()
    for tag in tags or ():
        tag_strong.update(strong_in(str(tag)))
        tag_weak.update(weak_in(str(tag)))
    return title_strong, tag_strong, title_weak, tag_weak


def resolve_channel(channel_code: str | None, title: str | None = None,
                     tags: object = ()) -> tuple[str | None, str]:
    """(Name, source) for ``channel`` mode. A lone strong marker wins over ``broadcaster_language``; a weak
    marker counts only when the Twitch code is missing, ``other``, or unknown. Naming two distinct languages
    stays ambiguous and falls through. Only the name ever reaches the model, never title or tag text. The source
    is one of ``title``, ``tag``, ``twitch``, ``question``."""
    title_strong, tag_strong, title_weak, tag_weak = _detect_split(title, tags)
    strong = title_strong | tag_strong
    combined = strong | title_weak | tag_weak
    twitch_name = language_name(None if not channel_code or channel_code == "other" else channel_code)
    if len(combined) == 1:
        code = next(iter(combined))
        if (name := LANGUAGE_NAMES.get(code)) is not None and (code in strong or twitch_name is None):
            where = "title" if code in title_strong or code in title_weak else "tag"
            return name, where
    if twitch_name is not None:
        return twitch_name, "twitch"
    return None, "question"


def decision_source(channel_code: str | None, title: str | None = None, tags: object = ()) -> str:
    """Where ``channel`` mode would take the language from, without resolving the name."""
    return resolve_channel(channel_code, title, tags)[1]


def resolve_name(setting: str, channel_code: str | None, title: str | None = None,
                 tags: object = ()) -> str | None:
    """The English language name to reply in, or None to fall back to the question's language.

    ``channel`` tries a lone strong title/tag marker first, then the cached Helix code; a weak marker (an
    English language name) counts only when that code is missing, ``other``, or unknown. A fixed ISO code uses
    that name. ``viewer`` always falls back."""
    if setting == VIEWER:
        return None
    if setting == CHANNEL:
        return resolve_channel(channel_code, title, tags)[0]
    return language_name(setting)


def guidance(setting: str, channel_code: str | None, title: str | None = None,
             tags: object = ()) -> str:
    """Two short lines for ``channel_context``: which language to use, and the chat voice."""
    name = resolve_name(setting, channel_code, title, tags)
    if name is None:
        reply = "Reply in the language of this question to everyone."
    elif setting == CHANNEL:
        reply = f"Reply in {name} (the stream's language) to everyone, whatever language they write in."
    else:
        reply = f"Reply in {name} to everyone, whatever language they write in."
    return f"{reply}\n{_VOICE}"
