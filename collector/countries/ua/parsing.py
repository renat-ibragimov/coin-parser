"""Pure, network-free parsing: raw NBU listing HTML -> raw card dicts ->
canonical card dict. No IO, no orchestration -- shared by parser.py
(per-series fetch/parse pipeline) and series.py (series-dictionary
builder), both of which need to read cards out of a search-result page.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from collector.countries.ua import vocab
from collector.countries.ua.nbu_client import BASE_URL, extract_card_id
from collector.countries.ua.normalize import fix_homoglyphs, normalize_title

# span.mark label text -> canonical field, per locale.
LABELS: dict[str, dict[str, str]] = {
    "uk": {
        "nominal": "Номінал:",
        "circulation_date": "Дата введення в обіг:",
        "material": "Матеріал:",
        "designers": "Художник:",
        "sculptors": "Скульптор:",
        "mintage": "Тираж (оголошений/фактичний), шт.:",
        "fine_weight": "Маса дорогоцінного металу в чистоті, г:",
        "weight": "Маса, г:",
        "diameter": "Діаметр, мм:",
        "quality": "Категорія якості карбування:",
        "edge": "Гурт:",
    },
    "en": {
        "nominal": "Nominal:",
        "circulation_date": "Circulation start date:",
        "material": "Material:",
        "designers": "Painter:",
        "sculptors": "Sculptor:",
        "mintage": "Coins in circulation (planned/actual):",
        "fine_weight": "Weight of pure precious metals, gramme:",
        "weight": "Mass, g:",
        "diameter": "Diameter, mm.:",
        "quality": "Quality:",
        "edge": "Coin edges:",
    },
}


class CardAnomaly(Exception):
    """Raised while building a canonical card when a raw value that must
    map through a closed vocabulary (vocab.py) has no matching entry.
    Caught by the caller -- the card is excluded from cards.json and
    recorded in anomalies.json instead, never written with a guessed or
    raw fallback code."""

    def __init__(self, field: str, raw_value: str, message: str):
        self.field = field
        self.raw_value = raw_value
        super().__init__(message)


# ---------------------------------------------------------------------- #
# raw HTML -> raw card dicts
# ---------------------------------------------------------------------- #


def _collapse_ws(s: str) -> str:
    return " ".join(s.split())


def parse_cards(html: str, locale: str) -> list[dict]:
    tree = HTMLParser(html)
    cards = []
    for node in tree.css("div.search-result"):
        card = _parse_card_node(node, locale)
        if card is not None:
            cards.append(card)
    return cards


def _parse_card_node(node, locale: str) -> dict | None:
    source_id = extract_card_id(node)
    if source_id is None:
        return None

    imgs = node.css("div.img-container img")

    title_node = node.css_first("div.title")
    title_raw = _collapse_ws(title_node.text()) if title_node else ""

    marks: dict[str, str] = {}
    for mark in node.css("span.mark"):
        label = _collapse_ws(mark.text())
        parent = mark.parent
        value_node = parent.css_first("span.mark-text") if parent else None
        marks[label] = _collapse_ws(value_node.text()) if value_node else ""

    paragraphs = [_collapse_ws(d.text()) for d in node.css("div.description__text")]
    general_desc, obverse_desc, reverse_desc = _split_description(paragraphs, locale)
    general_desc = fix_homoglyphs(general_desc) if general_desc else general_desc
    obverse_desc = fix_homoglyphs(obverse_desc) if obverse_desc else obverse_desc
    reverse_desc = fix_homoglyphs(reverse_desc) if reverse_desc else reverse_desc

    obverse_url = urljoin(BASE_URL, imgs[0].attributes.get("src") or "") if imgs else None
    reverse_url = urljoin(BASE_URL, imgs[1].attributes.get("src") or "") if len(imgs) > 1 else None

    # The thumbnail in the listing is 200x200, but NBU often wraps it in a
    # lightbox link to the full-resolution original (nbu:482's is 1120x1120,
    # the largest that exists for that coin anywhere). Read per thumbnail
    # rather than by collecting all the links, so an obverse that has one
    # and a reverse that does not stay correctly paired -- both cases occur,
    # sometimes on the same page. The link is not always live either
    # (nbu:438's 404s), so downstream must treat it as one more candidate
    # that may fail, not as a promise.
    obverse_big_url = _big_image_url(imgs[0]) if imgs else None
    reverse_big_url = _big_image_url(imgs[1]) if len(imgs) > 1 else None

    return {
        "source_id": source_id,
        "title_raw": title_raw,
        "marks": marks,
        "general_desc": general_desc,
        "obverse_desc": obverse_desc,
        "reverse_desc": reverse_desc,
        "obverse_img": obverse_url,
        "reverse_img": reverse_url,
        "obverse_big_img": obverse_big_url,
        "reverse_big_img": reverse_big_url,
    }


def _big_image_url(img_node) -> str | None:
    """The full-resolution original behind a listing thumbnail, if the page
    links one: `<a class="big-image" href="..."><img src="...thumb..."></a>`.
    """
    node = img_node.parent
    while node is not None and node.tag != "div":
        if node.tag == "a" and "big-image" in (node.attributes.get("class") or ""):
            href = node.attributes.get("href")
            return urljoin(BASE_URL, href) if href else None
        node = node.parent
    return None


def _split_description(
    paragraphs: list[str], locale: str
) -> tuple[str | None, str | None, str | None]:
    """NBU descriptions come as N free paragraphs: a general historical
    intro, then one obverse-specific and one reverse-specific paragraph.
    Classify by keyword; anything unclassified is treated as the general
    paragraph (there is normally exactly one).
    """
    obverse_kw = "аверс" if locale == "uk" else "obverse"
    reverse_kw = "реверс" if locale == "uk" else "reverse"

    general_parts, obverse_parts, reverse_parts = [], [], []
    for p in paragraphs:
        low = p.lower()
        if reverse_kw in low:
            reverse_parts.append(p)
        elif obverse_kw in low:
            obverse_parts.append(p)
        else:
            general_parts.append(p)

    general = " ".join(general_parts).strip() or None
    obverse = " ".join(obverse_parts).strip() or None
    reverse = " ".join(reverse_parts).strip() or None
    return general, obverse, reverse


# ---------------------------------------------------------------------- #
# raw card pair -> canonical card
# ---------------------------------------------------------------------- #


def _get_mark(uk_marks: dict, en_marks: dict, field_key: str) -> str | None:
    value = uk_marks.get(LABELS["uk"][field_key])
    if value:
        return value
    return en_marks.get(LABELS["en"][field_key]) or None


def _parse_int(s: str | None) -> int | None:
    if not s:
        return None
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None


def to_decimal(raw: str) -> Decimal | None:
    """"7 568" / "1\u00a0234,50" -> Decimal, or None if it is not a number.

    Ukrainian sites group thousands with a space (plain, non-breaking or
    narrow) and write the decimal separator as a comma; float() and
    Decimal() choke on both. Shared by every reader of a price out of
    ua-coins HTML or JSON -- one spelling of "what counts as a number"
    for the whole adapter.
    """
    cleaned = raw.replace(" ", "").replace("\u00a0", "").replace("\u202f", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _parse_float(s: str | None) -> float | None:
    if not s:
        return None
    m = re.search(r"[\d.,]+", s)
    if not m:
        return None
    return float(m.group().replace(",", "."))


def _parse_denomination(raw: str | None, warnings: list[str], source_id: str) -> dict:
    if not raw:
        warnings.append(f"nbu:{source_id}: missing denomination")
        return {"value": None, "unit": None}
    parts = raw.split(None, 1)
    value = _parse_float(parts[0])
    if value is not None and value.is_integer():
        value = int(value)
    unit = parts[1] if len(parts) > 1 else None
    if value is None:
        warnings.append(f"nbu:{source_id}: cannot parse denomination {raw!r}")
    return {"value": value, "unit": unit}


def _parse_circulation_date(
    raw: str | None, warnings: list[str], source_id: str
) -> tuple[str | None, int | None]:
    if not raw:
        warnings.append(f"nbu:{source_id}: missing circulation date")
        return None, None
    try:
        d = datetime.strptime(raw, "%d.%m.%Y").date()
    except ValueError:
        warnings.append(f"nbu:{source_id}: cannot parse circulation date {raw!r}")
        return None, None
    return d.isoformat(), d.year


def _parse_mintage(raw: str | None) -> dict:
    if not raw:
        return {"announced": None, "actual": None}
    if "/" in raw:
        left, right = raw.split("/", 1)
        return {"announced": _parse_int(left), "actual": _parse_int(right)}
    return {"announced": _parse_int(raw), "actual": None}


def _split_names(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [fix_homoglyphs(p.strip()) for p in re.split(r"[,;]", raw) if p.strip()]


def _pair_names(uk_raw: str | None, en_raw: str | None) -> list[dict]:
    """Pair uk/en artist names by position in their respective
    comma/semicolon-separated lists (there's no id to match individual
    names by -- same convention as obverse/reverse image order). Uneven
    counts just leave the missing side as None for that position."""
    uk_names = _split_names(uk_raw)
    en_names = _split_names(en_raw)
    count = max(len(uk_names), len(en_names))
    return [
        {
            "uk": uk_names[i] if i < len(uk_names) else None,
            "en": en_names[i] if i < len(en_names) else None,
        }
        for i in range(count)
    ]


def _lookup_or_raise(
    vocab_dict: dict[str, list[str]], raw: str | None, field: str, source_id: str
) -> str | None:
    """Look up `raw` in a closed vocabulary. None if raw is empty/missing
    (a separate, non-fatal "missing X" warning already covers that case
    elsewhere). Raises CardAnomaly if raw is present but unrecognized --
    never falls back to writing the raw string as if it were a code.
    """
    if not raw:
        return None
    code = vocab.lookup(vocab_dict, raw)
    if code is None:
        raise CardAnomaly(field, raw, f"nbu:{source_id}: unknown {field}: {raw!r}")
    return code


def build_canonical_card(
    source_id: str, uk: dict, en: dict | None, warnings: list[str]
) -> dict:
    uk_title_clean, metal_suffix = normalize_title(fix_homoglyphs(uk["title_raw"]))
    en_title_clean = None
    if en:
        en_title_clean, _ = normalize_title(fix_homoglyphs(en["title_raw"]))

    uk_marks = uk["marks"]
    en_marks = en["marks"] if en else {}

    def get(field_key: str) -> str | None:
        return _get_mark(uk_marks, en_marks, field_key)

    denomination = _parse_denomination(get("nominal"), warnings, source_id)
    unit_raw = denomination["unit"]
    denomination["unit"] = _lookup_or_raise(vocab.UNIT, unit_raw, "denomination.unit", source_id)

    circulation_date, year = _parse_circulation_date(
        get("circulation_date"), warnings, source_id
    )

    material_raw = get("material")
    material = _lookup_or_raise(vocab.MATERIALS, material_raw, "material", source_id)

    quality_raw = get("quality")
    quality = _lookup_or_raise(vocab.QUALITY, quality_raw, "quality", source_id)

    edge_raw = get("edge")
    edge = _lookup_or_raise(vocab.EDGE, edge_raw, "edge", source_id)

    # Artist names need both locales side by side, not the uk-preferred
    # fallback `get()` uses elsewhere -- so read uk_marks/en_marks
    # directly instead of going through _get_mark().
    designers = _pair_names(
        uk_marks.get(LABELS["uk"]["designers"]),
        en_marks.get(LABELS["en"]["designers"]),
    )
    sculptors = _pair_names(
        uk_marks.get(LABELS["uk"]["sculptors"]),
        en_marks.get(LABELS["en"]["sculptors"]),
    )

    return {
        "source_id": f"nbu:{source_id}",
        "titles": {"uk": uk_title_clean, "en": en_title_clean},
        "title_raw": {"uk": uk["title_raw"], "en": en["title_raw"] if en else None},
        "metal_suffix": metal_suffix,
        "denomination": denomination,
        "circulation_date": circulation_date,
        "year": year,
        "material": material,
        "material_raw": material_raw,
        "mintage": _parse_mintage(get("mintage")),
        "weight_grams": _parse_float(get("weight")),
        "fine_weight_grams": _parse_float(get("fine_weight")),
        "diameter_mm": _parse_float(get("diameter")),
        "quality": quality,
        "quality_raw": quality_raw,
        "edge": edge,
        "edge_raw": edge_raw,
        "description": {
            "uk": {
                "general": uk["general_desc"],
                "obverse": uk["obverse_desc"],
                "reverse": uk["reverse_desc"],
            },
            "en": {
                "general": en["general_desc"] if en else None,
                "obverse": en["obverse_desc"] if en else None,
                "reverse": en["reverse_desc"] if en else None,
            },
        },
        "artists": {
            "designers": designers,
            "sculptors": sculptors,
        },
        "images": {
            "obverse_url": uk["obverse_img"],
            "reverse_url": uk["reverse_img"],
            "obverse_big_url": uk.get("obverse_big_img"),
            "reverse_big_url": uk.get("reverse_big_img"),
        },
    }
