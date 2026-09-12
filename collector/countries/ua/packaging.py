"""Detects genuine bare<->packaged pairs within one series' parsed cards.

NBU appends a packaging phrase to a coin's own title on a subset of its
cards ("... у сувенірній упаковці" / "... у сувенірному пакованні") --
see normalize.split_packaging(). That phrase alone does NOT mean two
cards are the same coin, one loose and one boxed: this catalogue was
found (docs/01_findings.md, "packaging pairs" investigation) to also
reuse the exact same title text for two unrelated cases that must NOT
be linked as a pair:

  * the same subject reissued years apart (e.g. "Соломія Крушельницька"
    minted in both 1997 and 2022 -- 25 years apart, sharing a title,
    sharing nothing else);
  * a parallel heavier/bigger silver (or gold) edition of the same
    theme and year, catalogued under its own NBU card number (16.54g/
    35mm nickel silver next to a 31.1g/38.6mm silver "premium" piece) --
    a genuinely different product, not a box.

What actually tells "the same coin, sold loose or boxed" apart from
both of those is that NBU's own weight_grams and diameter_mm -- the
physical coin, independent of which retail channel it was counted
under -- match exactly. mintage_announced is deliberately NOT part of
the comparison: NBU counts a loose run and a boxed run as two separate
totals for the same coin, so requiring them to match would silently
drop genuine pairs (a die-identical coin sold 30 000 loose + 20 000
boxed is still one coin).

find_packaging_pairs() operates on a single series' `cards` list
(parser.py's own in-memory `cards`, or an equivalent list of dicts from
cards.json) because every genuine pair observed so far sits inside one
NBU series -- both the bare and the packaged card are pulled by the
same `serie[]` fetch, so there is no need (and no evidence to support)
matching across series boundaries.
"""

from __future__ import annotations

from collector.countries.ua.normalize import split_packaging


def find_packaging_pairs(cards: list[dict]) -> dict[str, str]:
    """Return {packaged_card_source_id: bare_card_source_id}.

    A packaged card with no matching bare twin in `cards` is left out of
    the mapping entirely -- for that card, the packaging IS the only
    form NBU ever released, and nothing should be linked.
    """
    groups: dict[tuple[str, object], list[tuple[dict, str | None]]] = {}
    for card in cards:
        title = card["titles"]["uk"]
        base, tail, _key = split_packaging(title)
        groups.setdefault((base, card.get("year")), []).append((card, tail))

    pairs: dict[str, str] = {}
    for members in groups.values():
        bare = [c for c, tail in members if tail is None]
        packaged = [c for c, tail in members if tail is not None]
        for p in packaged:
            p_weight = p.get("weight_grams")
            p_diameter = p.get("diameter_mm")
            if p_weight is None or p_diameter is None:
                continue
            twins = [
                b
                for b in bare
                if b.get("weight_grams") == p_weight and b.get("diameter_mm") == p_diameter
            ]
            if twins:
                pairs[p["source_id"]] = twins[0]["source_id"]
    return pairs
