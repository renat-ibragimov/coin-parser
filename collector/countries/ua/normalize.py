"""Text normalization helpers for NBU catalog data."""

from __future__ import annotations

import re

_QUOTE_CHARS = "«»\"'`‘’“”"
_METAL_SUFFIX_RE = re.compile(r"\s*\(([a-zA-Zа-яА-ЯіІїЇєЄ])\)\s*$")

# NBU sometimes renders the metal-suffix letter in Latin script instead of
# Cyrillic (observed: "срібло" suffixed as "(c)" with a Latin c instead of
# the Cyrillic "с"). The suffix is always one of these three letters, so any
# look-alike is unambiguous.
_SUFFIX_CANON = {
    "c": "с",  # Latin c -> Cyrillic с (срібло)
    "с": "с",
    "н": "н",  # нейзильбер
    "з": "з",  # золото
}

# Cyrillic/Latin homoglyph pairs that NBU text is known to mix up, mid-word
# ("рокiв" with a Latin i, "УHР" with a Latin H) or at a word's edge
# ("Христовe" with a trailing Latin e). Keyed by the Latin look-alike,
# mapped to its Cyrillic counterpart.
_HOMOGLYPHS = {
    "i": "і",
    "I": "І",
    "a": "а",
    "A": "А",
    "e": "е",
    "E": "Е",
    "o": "о",
    "O": "О",
    "p": "р",
    "P": "Р",
    "c": "с",
    "C": "С",
    "x": "х",
    "X": "Х",
    "y": "у",
    "H": "Н",
    "B": "В",
    "K": "К",
    "M": "М",
    "T": "Т",
}

_CYRILLIC_RE = re.compile(r"[а-яА-ЯіІїЇєЄґҐ]")
_LATIN_RE = re.compile(r"[a-zA-Z]")


def _is_word_boundary(ch: str) -> bool:
    """True for string edges, whitespace, punctuation, quotes, digits --
    anything that isn't itself a letter from either alphabet."""
    return not _CYRILLIC_RE.match(ch) and not _LATIN_RE.match(ch)


def fix_homoglyphs(s: str) -> str:
    """Replace a Latin look-alike letter with its Cyrillic counterpart.

    Replaces when at least one neighbor is Cyrillic and the other neighbor
    is Cyrillic OR a word boundary (start/end of string, whitespace,
    punctuation, quotes). This catches a stray Latin letter at the edge of
    an otherwise-Cyrillic word (e.g. NBU's "Христовe" with a trailing
    Latin e before a space) while still leaving whole Latin words alone
    ("Sea Baby" has no Cyrillic neighbor anywhere, so nothing fires).
    """
    if not s:
        return s
    chars = list(s)
    for i, ch in enumerate(chars):
        replacement = _HOMOGLYPHS.get(ch)
        if replacement is None:
            continue
        prev_ch = chars[i - 1] if i > 0 else ""
        next_ch = chars[i + 1] if i + 1 < len(chars) else ""
        prev_is_cyrillic = bool(_CYRILLIC_RE.match(prev_ch))
        next_is_cyrillic = bool(_CYRILLIC_RE.match(next_ch))
        prev_ok = prev_is_cyrillic or _is_word_boundary(prev_ch)
        next_ok = next_is_cyrillic or _is_word_boundary(next_ch)
        if prev_ok and next_ok and (prev_is_cyrillic or next_is_cyrillic):
            chars[i] = replacement
    return "".join(chars)


def strip_quotes(s: str) -> str:
    """Strip a matching pair of wrapping quote characters -- only when
    BOTH the first and last character are quote marks. `str.strip(chars)`
    would happily eat a trailing quote with no matching leading one,
    which is wrong when a quote pair sits mid-string wrapping just an
    embedded name rather than the whole title (e.g. NBU's "Криголам
    `Капітан Бєлоусов`" -- the leading backtick isn't at the string's
    edge, so a plain .strip() only ate the trailing one, leaving a
    dangling unmatched backtick). Leaving both untouched when they don't
    symmetrically wrap the whole string is the correct, non-mangling
    behavior; converting them to real typographic quotes is a separate
    decision, not this function's job.
    """
    if len(s) >= 2 and s[0] in _QUOTE_CHARS and s[-1] in _QUOTE_CHARS:
        return s[1:-1].strip()
    return s


def fix_stray_apostrophe(s: str) -> str:
    """Replace a backtick standing in for an apostrophe inside a word
    (e.g. NBU's en title "Catherine`s Glory Ship of the Line") with a
    proper apostrophe (’).

    Only fires when both neighbors are letters. A backtick used as an
    ad-hoc quote mark around an embedded name ("Icebreaker `Captain
    Belousov`") always has a non-letter neighbor (a space, or the string
    edge) on at least one side, so those are left untouched here --
    see strip_quotes for why that pair itself isn't collapsed either.
    """
    if "`" not in s:
        return s
    chars = list(s)
    for i, ch in enumerate(chars):
        if ch != "`":
            continue
        prev_ch = chars[i - 1] if i > 0 else ""
        next_ch = chars[i + 1] if i + 1 < len(chars) else ""
        if prev_ch.isalpha() and next_ch.isalpha():
            chars[i] = "’"
    return "".join(chars)


def normalize_title(s: str) -> tuple[str, str | None]:
    """Strip wrapping quotes and a trailing metal-suffix "(н|с|з)".

    Returns (clean_title, metal_suffix) where metal_suffix is the
    canonical Cyrillic letter (н/с/з) or None if there wasn't one.
    """
    text = s.strip()

    suffix = None
    match = _METAL_SUFFIX_RE.search(text)
    if match:
        raw_letter = match.group(1)
        suffix = _SUFFIX_CANON.get(raw_letter, raw_letter)
        text = text[: match.start()].rstrip()

    text = strip_quotes(text)
    text = fix_stray_apostrophe(text)
    text = re.sub(r"\s+", " ", text).strip()

    return text, suffix


# NBU isn't consistent about which apostrophe-look-alike character it uses
# in a series name across its own database (seen: a grave accent "`"
# where a straight apostrophe would be expected -- "Античні пам`ятки
# України" -- next to other series using a proper right single quote
# "Пам'ятки архітектури України" (’)). All four collapse to one
# canonical character for comparison purposes only.
_APOSTROPHE_CHARS = "'`’ʼ"
_APOSTROPHE_CANON = "’"


def normalize_match(s: str) -> str:
    """Normalize a string (series name) for equality comparison only --
    NOT for display. Applies fix_homoglyphs, folds every apostrophe
    look-alike to one canonical character, and collapses whitespace.
    Two strings that differ only in which apostrophe character or which
    script a look-alike letter is in compare equal after this.
    """
    text = fix_homoglyphs(s)
    for ch in _APOSTROPHE_CHARS:
        text = text.replace(ch, _APOSTROPHE_CANON)
    return re.sub(r"\s+", " ", text).strip()
