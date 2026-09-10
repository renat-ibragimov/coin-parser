"""Text normalization helpers for NBU catalog data."""

from __future__ import annotations

import re

_QUOTE_CHARS = "«»\"'`‘’“”"

# One letter for every material but bimetallic (н нейзильбер, с/c срібло, з
# золото, м мельхіор) -- NBU spells that one out as "бн" instead, apparently
# because a bare "б" would be one edit away from "в" and "н" is already
# taken. "бн" is matched as its own literal alternative rather than folded
# into a generic 1-2-letter rule: a closed list of known codes can't
# accidentally eat a real short parenthetical (nbu:1520's title is "Рік
# Кота (Кролика)" -- an alternate name, not a material tag, and "Кролика"
# only escapes this by being longer than two letters).
_METAL_SUFFIX_RE = re.compile(r"\s*\((бн|[a-zA-Zа-яА-ЯіІїЇєЄ])\)\s*$")

# NBU sometimes renders the metal-suffix letter in Latin script instead of
# Cyrillic (observed: "срібло" suffixed as "(c)" with a Latin c instead of
# the Cyrillic "с"). The suffix is always one of these known codes, so any
# look-alike is unambiguous.
_SUFFIX_CANON = {
    "c": "с",  # Latin c -> Cyrillic с (срібло)
    "с": "с",
    "н": "н",  # нейзильбер
    "з": "з",  # золото
    "м": "м",  # мельхіор
    "бн": "бн",  # bimetallic ("біметалеві із недорогоцінних металів")
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


# Both NBU and ua-coins mark the separately-catalogued packaged variant
# of a coin by appending a packaging phrase to the coin's own title --
# that tail is the ONLY thing telling "Захисниці" apart from "Захисниці
# у сувенірній упаковці", which are two different catalog entries with
# the same year and denomination. Both sites drift between "упаковці"
# and "пакованні" for the same packaging (NBU's nbu:1718 says
# "пакованні" where its neighbours in the same series say "упаковці"),
# so the noun is folded away and the adjective ("сувенірн-" /
# "подарунков-") is what identifies WHICH packaging it is.
#
# Cases and caskets ("у футлярі", "у дерев`яному футлярі") are
# deliberately not part of this family: nothing observed so far
# distinguishes two catalog entries by a футляр tail alone, and the tail
# is part of the item's real name there ("Набір із двох срібних монет
# ... у футлярі" is a set, not a packaged single coin).
_PACKAGING_RE = re.compile(
    r"\s+у\s+(сувенірн|подарунков)\w*\s+(?:упаковці|пакованні)\s*$",
    re.IGNORECASE,
)


def split_packaging(s: str) -> tuple[str, str | None, str | None]:
    """Split a trailing packaging phrase off a title.

    Returns (base, tail, key): the title without the tail, the tail
    verbatim (so a caller can re-attach it for display) or None, and the
    canonical packaging key -- the lowercased adjective stem, which
    compares equal across the "упаковці"/"пакованні" spelling drift --
    or None when there is no packaging tail.
    """
    match = _PACKAGING_RE.search(s)
    if match is None:
        return s, None, None
    return s[: match.start()].rstrip(), match.group(0).strip(), match.group(1).lower()


def normalize_title(s: str) -> tuple[str, str | None]:
    """Strip wrapping quotes and a trailing metal-suffix "(н|с|з)".

    Returns (clean_title, metal_suffix) where metal_suffix is the
    canonical Cyrillic letter (н/с/з) or None if there wasn't one.

    A packaging tail (see split_packaging) is peeled off first and
    re-attached at the end, so the quote-stripping and metal-suffix rules
    still see the coin's own name at the string's edge when NBU appends
    one. That is what nbu:1718's title needs -- NBU writes it as
    `"Країна супергероїв. Дякуємо зброярам!" (н) у сувенірному
    пакованні`, where both the closing quote and the suffix sit
    mid-string. The two tails come in either order, so they are peeled in
    whatever order they appear, once each.
    """
    text = s.strip()

    suffix = None
    packaging_tail = None
    while True:
        match = _METAL_SUFFIX_RE.search(text)
        if match and suffix is None:
            raw_letter = match.group(1)
            suffix = _SUFFIX_CANON.get(raw_letter, raw_letter)
            text = text[: match.start()].rstrip()
            continue
        base, tail, _ = split_packaging(text)
        if tail is not None and packaging_tail is None:
            packaging_tail = tail
            text = base
            continue
        break

    text = strip_quotes(text)
    text = fix_stray_apostrophe(text)
    if packaging_tail:
        text = f"{text} {packaging_tail}"
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

# Quote marks are noise for comparison: NBU quotes the coin's own name in
# some titles and not in others, and ua-coins copies whichever form NBU
# used at the time -- ua-coins row 2659 keeps NBU's straight quotes in
# `"Країна супергероїв. Дякуємо зброярам!" у сувенірному пакованні`,
# while our own parsed title has them stripped. Dropped from both sides
# here rather than "fixed" on either.
#
# The apostrophe look-alikes are not in this list because inside a word
# they carry a letter's worth of meaning ("Пам’ятки"). But ua-coins also
# uses ’ as an outer quote mark, around a name that already contains
# straight quotes: `’Українські народні казки. "Кирило Кожум’яка"’ у
# сувенірному пакованні` against NBU's unwrapped form of the same title.
# 33 rows of the cached catalogue are written that way. What separates
# the two uses is position, not character: a Ukrainian apostrophe always
# stands between two letters, and a quote mark never does -- which is
# the rule _fold_apostrophes applies.
_MATCH_DROP_CHARS = '«»"“”'


def _fold_apostrophes(text: str) -> str:
    """Canonicalize the apostrophes and drop the quote marks, telling them
    apart by where they stand.

    Between two letters it is an apostrophe -- Кожум’яка, пам`ятки,
    CHILDREN’S -- and folds to one canonical character. Anywhere else it
    is being used as a quote mark and goes, the same as « " “ do.

    Judged on the text as it arrives, before the other quote marks are
    dropped: removing those first would close a gap like `"’` and leave
    an outer quote looking letter-flanked.
    """
    out: list[str] = []
    for i, ch in enumerate(text):
        if ch not in _APOSTROPHE_CHARS:
            out.append(ch)
            continue
        prev = text[i - 1] if i else ""
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if prev.isalpha() and nxt.isalpha():
            out.append(_APOSTROPHE_CANON)
    return "".join(out)


def normalize_match(s: str) -> str:
    """Normalize a string (series name, coin title) for equality
    comparison only -- NOT for display. Applies fix_homoglyphs, folds
    every apostrophe look-alike inside a word to one canonical character,
    drops quote marks -- an apostrophe look-alike used as one included --
    and collapses whitespace. Two strings that differ only in which
    apostrophe character, which script a look-alike letter is in, or
    whether a name is quoted compare equal after this.
    """
    text = _fold_apostrophes(fix_homoglyphs(s))
    for ch in _MATCH_DROP_CHARS:
        text = text.replace(ch, "")
    return re.sub(r"\s+", " ", text).strip()
