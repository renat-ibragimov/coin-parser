"""Closed vocabularies mapping NBU's raw Ukrainian labels to canonical
codes for material / quality / edge / denomination unit.

Each dict is keyed by the canonical code, with a list of every raw NBU
label known to mean that code -- so when an anomaly turns up a new raw
spelling of an existing concept (e.g. a plural variant of an edge
description), fixing it is just appending one string to the right list,
not deciding between adding a new key or reusing an old one.

Lookup is case- and whitespace-insensitive, and runs the raw label through
fix_homoglyphs() first -- so a stray Latin look-alike letter in the source
text (see normalize.py) can't cause a spurious "unknown value" anomaly by
itself. Everything else is a hard match: a raw label matching none of the
lists below is NOT guessed at, the caller (parser.py) turns that into a
CardAnomaly.

These are literal, official NBU strings, not something we control -- the
lists below cover what's been seen in the pilot series plus a few known
values from the wider catalog. Extend them as new series turn up new
labels. Note: "звичайна" (lit. "ordinary") and "анциркулейтед"
(transliteration of "uncirculated") are two different raw strings NBU uses
for the same non-proof finish tier depending on the coin/era -- both are
listed under "uncirculated" on purpose, not a typo.
"""

from __future__ import annotations

from collector.countries.ua.normalize import fix_homoglyphs

MATERIALS: dict[str, list[str]] = {
    "gold": ["золото"],
    "silver": ["срібло"],
    "nickel_silver": ["нейзильбер"],
    # Its own code, NOT a variant of nickel_silver, however close they
    # look on a coin: мельхіор is copper-nickel, нейзильбер is
    # copper-nickel-ZINC, and NBU uses both words as different materials
    # in the same series ("Видатні особистості України" has 143 of one
    # and 5 of the other). Folding them together would be exactly the
    # guess this module exists to refuse.
    "cupronickel": ["мельхіор"],
    # NBU spells this out in full on some series ("Відродження
    # української державності", nbu:236) instead of the short "біметал"
    # seen elsewhere -- same material, "made of base metals" being the
    # only bimetallic option NBU uses (never precious-metal bimetallic).
    "bimetallic": ["біметал", "біметалеві із недорогоцінних металів"],
}

QUALITY: dict[str, list[str]] = {
    "proof": ["пруф"],
    # Not a spelling of "пруф": a proof-like strike is polished dies
    # without the full proof treatment, and NBU grades both in the same
    # series ("Видатні особистості України": 26 пруф against 5
    # пруф-лайк, on different coins). Merging them would upgrade five
    # coins to a quality their issuer never claimed.
    "proof_like": ["пруф-лайк"],
    "uncirculated": ["звичайна", "анциркулейтед"],
    "special_uncirculated": ["спеціальний анциркулейтед"],
}

EDGE: dict[str, list[str]] = {
    "plain": ["гладкий"],
    "reeded": ["рифлений"],
    "plain_incuse_lettering": [
        "гладкий із заглибленим написом",
        "гладкий із заглибленими написами",
    ],
    "sector_reeded": ["секторальне рифлення"],
}

UNIT: dict[str, list[str]] = {
    "hryvnia": ["грн", "грн."],
    # NBU abbreviates the karbovanets both ways in its own catalog --
    # "крб" on some cards, "карб" on others, sometimes within one series.
    # Same currency, same canonical code; the DB already knows it as UAK.
    "karbovanets": ["крб", "крб.", "карб", "карб."],
}


def _normalize_key(raw: str) -> str:
    return fix_homoglyphs(raw).strip().lower()


def lookup(vocab: dict[str, list[str]], raw: str | None) -> str | None:
    """Look up a raw NBU label across every code's variant list; None if
    raw is empty/None, or if a non-empty raw label matches no variant."""
    if not raw:
        return None
    key = _normalize_key(raw)
    for code, raw_variants in vocab.items():
        if any(_normalize_key(variant) == key for variant in raw_variants):
            return code
    return None
