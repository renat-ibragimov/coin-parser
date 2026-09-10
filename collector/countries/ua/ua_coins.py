"""Matches NBU cards to their catalog page on ua-coins.info.

Deterministic only -- (normalize_match(title), year, denomination) as the
key, with year_shift as the one explicit fallback (NBU's own circulation
year and the coin's face year can differ by one). Never matches by name
similarity/fuzzy scoring; ambiguity (more than one candidate on either
side) is reported as a conflict, not silently resolved.

Photos and price HISTORY are out of scope here: matching only records
the URL/id of the coin's ua-coins.info page. The one price this module
does read is the single current quote the yearly catalog table already
carries in its "Вартість дд.мм.гггг" column -- the nightly update-prices
step lives off it, and it comes from the very page the matcher fetches.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser

from collector.core.pacing import Pacer
from collector.countries.ua.normalize import normalize_match, split_packaging
from collector.countries.ua.parsing import _parse_float, to_decimal

BASE_URL = "https://www.ua-coins.info"
CATALOG_PATH = "/ua/catalog/all/{year}"

# ua-coins.info turns out to rate-limit harder than bank.gov.ua: a series
# whose cards span many different years needs one request per distinct
# year (year-1/year/year+1 for each), which can be a few dozen requests
# in a row for a wide-year-range series -- NBU's 1-2s pacing got a 429
# partway through such a run (see docs/01_findings.md). Slower and
# dedicated to this site rather than reusing NBU's constant.
REQUEST_DELAY_RANGE = (2.5, 4.5)
MAX_429_RETRIES = 5
DEFAULT_429_BACKOFF_SECONDS = 30.0

# Separate from the 429 handling above: transport-level failures (a
# connection reset mid-response, a dropped socket) that ua-coins.info's
# server produces now and then even outside rate-limiting -- e.g.
# httpx.RemoteProtocolError "Server disconnected without sending a
# response". These have nothing to do with pacing, so they get their own
# short retry budget rather than consuming a 429 attempt.
MAX_TRANSPORT_RETRIES = 3
TRANSPORT_RETRY_BACKOFF_SECONDS = 3.0

_ID_RE = re.compile(r"^(\d+)")


def staging_dir(staging_root: Path) -> Path:
    return staging_root / "ua" / "_ua_coins" / "raw"


def row_id_from_href(href: str) -> int | None:
    """"/ua/list/1999-rizdvo-khrystove" -> 1999, the ua-coins id.

    It looks like a year and is not one: ua-coins' ids run from 1 and its
    early entries happen to land in the 1990s (see docs/01_findings.md).
    The leading number of the slug is the id, full stop.
    """
    slug_part = href.rstrip("/").rsplit("/", 1)[-1]
    m = _ID_RE.match(slug_part)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------- #
# fetch (network, cached in a shared staging dir -- not per-series)
# ---------------------------------------------------------------------- #


def get_with_retry(
    client: httpx.Client,
    path: str,
    pacer: Pacer,
    *,
    log_label: str = "",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """GET `path` off ua-coins.info, paced, with 429 backoff-retry
    (respecting a Retry-After header if the server sends one) and a
    short separate retry for transport-level failures (connection reset,
    dropped socket -- see MAX_TRANSPORT_RETRIES). Shared by every
    ua-coins.info caller in this adapter (catalog pages, coin detail
    pages, image downloads, the signed price-chart endpoint) -- one
    rate-limit policy for the one host, so nothing accidentally paces or
    retries differently.

    `headers` are merged over the client's own for this one request; the
    price-chart endpoint needs a Referer naming the coin page whose
    render minted its signature (see prices.py).
    """
    for attempt in range(1, MAX_429_RETRIES + 1):
        pacer.wait()
        for transport_attempt in range(1, MAX_TRANSPORT_RETRIES + 1):
            try:
                resp = client.get(path, headers=headers)
                break
            except httpx.TransportError as exc:
                if transport_attempt == MAX_TRANSPORT_RETRIES:
                    raise
                print(
                    f"[ua_coins]   connection error for {log_label or path} "
                    f"(attempt {transport_attempt}/{MAX_TRANSPORT_RETRIES}): {exc}, "
                    f"retrying in {TRANSPORT_RETRY_BACKOFF_SECONDS:.0f}s..."
                )
                time.sleep(TRANSPORT_RETRY_BACKOFF_SECONDS)
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            delay = (
                float(retry_after)
                if retry_after and retry_after.strip().isdigit()
                else DEFAULT_429_BACKOFF_SECONDS
            )
            print(
                f"[ua_coins]   429 from ua-coins.info for {log_label or path} "
                f"(attempt {attempt}/{MAX_429_RETRIES}), waiting {delay:.0f}s..."
            )
            time.sleep(delay)
            continue
        resp.raise_for_status()
        return resp

    resp.raise_for_status()  # exhausted retries -- surface the last 429 as an error
    raise AssertionError("unreachable")  # raise_for_status() above always raises here


def fetch_year(
    client: httpx.Client, year: int, dir_: Path, pacer: Pacer, refresh: bool
) -> tuple[str, bool]:
    """HTML for one year's catalog page. Returns (html, was_fetched) --
    was_fetched is False when it was read from the staging cache without
    touching the network.

    A year with no catalog page at all (ua-coins 404s it -- seen for
    year+1 of a card whose circulation year is the current year, e.g.
    querying 2027 while it's still 2026) is cached as an empty page
    rather than raised as an error: it's a legitimate "no coins here",
    not a fetch failure, and re-fetching it every run would be wasted
    requests for a year that isn't going to appear later.
    """
    path = dir_ / f"{year}.html"
    if path.exists() and not refresh:
        return path.read_text(encoding="utf-8"), False

    try:
        resp = get_with_retry(client, CATALOG_PATH.format(year=year), pacer, log_label=str(year))
        html = resp.text
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            html = ""
        else:
            raise
    dir_.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return html, True


# ---------------------------------------------------------------------- #
# parse (pure, no network)
# ---------------------------------------------------------------------- #


@dataclass
class UaCoinsRow:
    id: int
    url: str
    title: str
    title_key: str
    year: int
    denomination: float


def parse_year(html: str, year: int) -> list[UaCoinsRow]:
    tree = HTMLParser(html)
    table = tree.css_first("table.coin-list")
    if table is None:
        return []

    rows: list[UaCoinsRow] = []
    for tr in table.css("tr"):
        name_cell = tr.css_first('td[data-title="Назва"]')
        if name_cell is None:
            continue  # header row or the "year" separator row
        link = name_cell.css_first("a")
        if link is None:
            continue

        href = (link.attributes.get("href") or "").strip()
        row_id = row_id_from_href(href)
        if row_id is None:
            continue

        title = (link.attributes.get("title") or link.text()).strip()

        denom_cell = tr.css_first('td[data-title="Номінал"]')
        denom_value = _parse_float(denom_cell.text()) if denom_cell else None
        if denom_value is None:
            continue

        rows.append(
            UaCoinsRow(
                id=row_id,
                url=urljoin(BASE_URL, href),
                title=title,
                title_key=normalize_match(title),
                year=year,
                denomination=denom_value,
            )
        )
    return rows


# ---------------------------------------------------------------------- #
# the price column of the yearly table (pure, no network)
# ---------------------------------------------------------------------- #

# The same yearly page the matcher reads also carries ONE current price
# per coin, in a column whose header names the day it was taken:
# "Вартість 08.09.2026". That header is the only date this adapter will
# put on such a quote. ua-coins recomputes the column on its own
# schedule, so a morning run can still see yesterday's number -- and
# stamping it with today's date would invent a price point that nobody
# quoted. See update_prices.py, which is the whole reason this exists:
# one cheap page per year against one signed request per coin.
_QUOTE_DATE_RE = re.compile(r"Вартість\s+(\d{2})\.(\d{2})\.(\d{4})")
_QUOTE_HEADER_PREFIX = "Вартість"
# A price cell holds "7 568" plus an arrow span, or the site's own
# "немає даних" for a coin it has no quote for.
_QUOTE_NUMBER_RE = re.compile(r"\d[\d\s\u00a0\u202f.,]*")


class YearTableAnomaly(ValueError):
    """The year page has a coin table, but not in the shape prices are
    read out of. Takes the WHOLE year out of a run rather than letting a
    misread page write dated numbers into the production history."""


@dataclass
class UaCoinsQuote:
    id: int
    url: str
    title: str
    price: Decimal | None  # None == the row says "немає даних"
    raw_text: str


@dataclass
class YearQuotes:
    year: int
    as_of: date | None  # None only when the year has no table at all
    quotes: dict[int, UaCoinsQuote] = field(default_factory=dict)


def _quote_cell(tr) -> object | None:
    """The row's price cell. Located by class first and by the column
    header echoed into data-title second, so a class rename costs the
    prices only if the header wording changes too."""
    cell = tr.css_first("td.ua-table-price-cell")
    if cell is not None:
        return cell
    for td in tr.css("td"):
        if (td.attributes.get("data-title") or "").strip().startswith(_QUOTE_HEADER_PREFIX):
            return td
    return None


def parse_quote_date(table) -> date | None:
    """The snapshot date out of the "Вартість дд.мм.гггг" column header.

    Looked for in the header row's text and then in the data-title
    attributes the site copies that same header into for its mobile
    layout -- two spellings of one fact, so losing either does not lose
    the date.
    """
    texts = [td.text() or "" for td in table.css("thead td")]
    texts += [td.text() or "" for td in table.css("thead th")]
    texts += [
        (td.attributes.get("data-title") or "")
        for td in table.css("td")
        if (td.attributes.get("data-title") or "").strip().startswith(_QUOTE_HEADER_PREFIX)
    ]
    for text in texts:
        m = _QUOTE_DATE_RE.search(text)
        if m is None:
            continue
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            raise YearTableAnomaly(f"price column header has an impossible date: {text.strip()!r}")
    return None


def parse_price_cell(text: str) -> Decimal | None:
    """The number in a price cell, or None when there is not one.

    "немає даних" is not an error and not a zero: ua-coins simply has no
    quote for that coin (nbu:88 has been in that state since 2024). It
    reads as None here and the coin is skipped by the caller.
    """
    m = _QUOTE_NUMBER_RE.search(text)
    if m is None:
        return None
    price = to_decimal(m.group().strip())
    if price is None or price <= 0:
        return None
    return price


def parse_year_quotes(html: str, year: int) -> YearQuotes:
    """One year's catalog page -> {ua_coins id: its current quote}.

    Keyed by the ua-coins id from the row's own href, never by title:
    the id is already stored per coin (price_source_links' external_id),
    so this side does no matching at all -- the fuzzy question was
    settled once, at --step match, and is not reopened nightly.
    """
    tree = HTMLParser(html)
    table = tree.css_first("table.coin-list")
    if table is None:
        # No table at all -- a year ua-coins does not have a page for
        # (fetch_year caches those as an empty document). Not an anomaly.
        return YearQuotes(year=year, as_of=None)

    as_of = parse_quote_date(table)
    if as_of is None:
        raise YearTableAnomaly(
            f"year {year}: no \"{_QUOTE_HEADER_PREFIX} дд.мм.гггг\" column header -- "
            "without it there is no date to put on these prices"
        )

    quotes: dict[int, UaCoinsQuote] = {}
    for tr in table.css("tr"):
        name_cell = tr.css_first('td[data-title="Назва"]')
        if name_cell is None:
            continue  # header row or the "year" separator row
        link = name_cell.css_first("a")
        if link is None:
            continue

        href = (link.attributes.get("href") or "").strip()
        row_id = row_id_from_href(href)
        if row_id is None:
            continue

        cell = _quote_cell(tr)
        raw_text = " ".join((cell.text() if cell is not None else "").split())
        quotes[row_id] = UaCoinsQuote(
            id=row_id,
            url=urljoin(BASE_URL, href),
            title=(link.attributes.get("title") or link.text()).strip(),
            price=parse_price_cell(raw_text) if cell is not None else None,
            raw_text=raw_text,
        )

    return YearQuotes(year=year, as_of=as_of, quotes=quotes)


# ---------------------------------------------------------------------- #
# matching (pure, no network)
# ---------------------------------------------------------------------- #


def _titles_match(a: str, b: str) -> bool:
    """a, b are already normalize_match()-ed. Equal, or one is the other
    plus a trailing clarification ("Назва уточнення") -- ua-coins
    sometimes adds detail NBU's own title doesn't have.

    A packaging tail is the one exception to that leniency: "Захисниці"
    (ua-coins 2449) and "Захисниці у сувенірній упаковці" (2441) are two
    different catalog entries at the same year and denomination, so
    reading the tail as a clarification made BOTH of them candidates for
    either NBU card and turned every such pair into a conflict. Both
    sides must therefore carry the same packaging -- compared by
    split_packaging's key, which is blind to the "упаковці"/"пакованні"
    spelling drift, so a card NBU spells one way still matches a
    ua-coins row spelling it the other.
    """
    a_base, _, a_packaging = split_packaging(a)
    b_base, _, b_packaging = split_packaging(b)
    if a_packaging != b_packaging:
        return False
    if a_base == b_base:
        return True
    return a_base.startswith(b_base + " ") or b_base.startswith(a_base + " ")


def _find_candidates(rows: list[UaCoinsRow], title_key: str, denom: float) -> list[UaCoinsRow]:
    return [r for r in rows if r.denomination == denom and _titles_match(title_key, r.title_key)]


def _prefer_exact_title(candidates: list[UaCoinsRow], title_key: str) -> list[UaCoinsRow]:
    """When several candidates survive, an EXACTLY equal title beats one
    that only matched by the trailing-clarification leniency.

    Real case: NBU's "Лев Ландау" (2 UAH, 2008) drew two candidates --
    ua-coins 196 "Лев Ландау" and ua-coins 1872 "Лев", a different coin
    of the same year and denomination that happens to be a word-prefix
    of the physicist's name. The leniency exists because ua-coins
    sometimes appends detail NBU's own title lacks; it was never meant to
    let a shorter, unrelated name compete with the real thing.

    Deterministic and narrowing only: it never invents a candidate, and
    it steps aside unless exactly one is an exact match, so a genuine
    ambiguity is still reported as a conflict rather than resolved by
    preference.
    """
    exact = [c for c in candidates if c.title_key == title_key]
    return exact if len(exact) == 1 else candidates


# ua-coins sometimes lists the same coin twice under one title with a
# trailing "(quality)" clarification distinguishing two real, different
# catalog entries -- e.g. "100 років Київському політехнічному
# інституту (звичайна)" vs "... (анциркулейтед)", id 29 vs id 28 (same
# year, same denomination). NBU's own title never carries this, so both
# rows pass the primary title match and _find_candidates reports a
# conflict. This only fires as a rescue AFTER that conflict happens, and
# only narrows using the card's own quality_raw text -- it never
# invents a match no candidate already qualified for, so it can't
# misfire on titles whose "(...)" is part of the real name instead of a
# quality marker (e.g. our own "Скіфське золото (богиня Апі)"): those
# only ever produce ONE candidate in the first place, so this never runs.
_TRAILING_PARENS_RE = re.compile(r"\s*\(([^()]+)\)\s*$")


def _disambiguate_by_quality(
    candidates: list[UaCoinsRow], card_quality_raw: str | None
) -> list[UaCoinsRow]:
    if len(candidates) <= 1 or not card_quality_raw:
        return candidates

    split = [_TRAILING_PARENS_RE.search(c.title) for c in candidates]
    if any(m is None for m in split):
        return candidates  # not every candidate has a "(...)" -- not this pattern
    bases = {c.title[: m.start()].strip() for c, m in zip(candidates, split)}
    if len(bases) != 1:
        return candidates  # different base names -- a real conflict, not a quality split

    target = normalize_match(card_quality_raw)
    narrowed = [
        c for c, m in zip(candidates, split) if normalize_match(m.group(1)) == target
    ]
    return narrowed if len(narrowed) == 1 else candidates


SHARED_LISTINGS_FILE = Path(__file__).with_name("shared_listings.json")


def shared_listings(path: Path | None = None) -> dict[int, frozenset[str]]:
    """{ua-coins row id: the exact set of NBU cards allowed to share it}.

    NBU sometimes catalogues a set one card per coin while ua-coins sells
    it as a single position -- «Пектораль» is four cards against row 2294.
    Left alone the many-to-one guard below drops all of them, which is the
    right default: a row claimed by several cards is far more often a
    matching mistake than a set. So the sets are declared by hand, in
    shared_listings.json, after somebody has looked at the case.

    The set is exact, not a minimum. A fifth card arriving on 2294 makes
    the claim stop matching this declaration and the guard fires again --
    which is the point of writing the members down rather than a bare
    "this row may be shared".
    """
    raw = json.loads((path or SHARED_LISTINGS_FILE).read_text(encoding="utf-8"))
    return {
        int(entry["ua_coins_id"]): frozenset(entry["source_ids"])
        for entry in raw.get("shared", [])
    }


def match_cards(
    cards: list[dict],
    rows_by_year: dict[int, list[UaCoinsRow]],
    matched_at: str,
    shared: dict[int, frozenset[str]] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Sets card["ua_coins"] on every card (a dict, or None) and returns
    (cards, unmatched_entries). Never mutates rows_by_year."""
    per_card: dict[str, dict] = {}

    for card in cards:
        source_id = card["source_id"]
        year = card.get("year")
        denom = card.get("denomination", {}).get("value")
        title = card.get("titles", {}).get("uk")

        if year is None or denom is None or not title:
            per_card[source_id] = {
                "status": "unmatched",
                "reason": "card missing year, denomination, or uk title",
                "candidates_seen": [],
            }
            continue

        title_key = normalize_match(title)
        quality_raw = card.get("quality_raw")

        exact = _find_candidates(rows_by_year.get(year, []), title_key, denom)
        if len(exact) > 1:
            exact = _prefer_exact_title(exact, title_key)
        if len(exact) > 1:
            exact = _disambiguate_by_quality(exact, quality_raw)
        if len(exact) == 1:
            per_card[source_id] = {"status": "match", "matched_by": "exact", "row": exact[0], "year_used": year}
            continue
        if len(exact) > 1:
            per_card[source_id] = {"status": "conflict", "candidates": exact}
            continue

        shifted: list[UaCoinsRow] = []
        for y in (year - 1, year + 1):
            shifted.extend(_find_candidates(rows_by_year.get(y, []), title_key, denom))
        if len(shifted) > 1:
            shifted = _prefer_exact_title(shifted, title_key)
        if len(shifted) > 1:
            shifted = _disambiguate_by_quality(shifted, quality_raw)
        if len(shifted) == 1:
            row = shifted[0]
            per_card[source_id] = {
                "status": "match",
                "matched_by": "year_shift",
                "row": row,
                "year_used": row.year,
            }
            continue
        if len(shifted) > 1:
            per_card[source_id] = {"status": "conflict", "candidates": shifted}
            continue

        seen_titles = []
        for y in (year - 1, year, year + 1):
            for row in rows_by_year.get(y, []):
                if row.denomination == denom:
                    seen_titles.append(row.title)
        per_card[source_id] = {
            "status": "unmatched",
            "reason": "no candidate on ua-coins for this title/year(±1)/denomination",
            "candidates_seen": seen_titles,
        }

    # A ua-coins row claimed by more than one card is a conflict too, even
    # though each card individually looked like a unique match. Unless the
    # sharing is a declared set -- and then only if the cards claiming it
    # are exactly the ones declared.
    declared = shared_listings() if shared is None else shared
    claims: dict[int, list[str]] = {}
    for source_id, result in per_card.items():
        if result["status"] == "match":
            claims.setdefault(result["row"].id, []).append(source_id)
    contested = {
        row_id
        for row_id, holders in claims.items()
        if len(holders) > 1 and declared.get(row_id) != frozenset(holders)
    }
    is_shared = {
        row_id for row_id, holders in claims.items() if len(holders) > 1
    } - contested

    unmatched_entries: list[dict] = []
    for card in cards:
        source_id = card["source_id"]
        result = per_card[source_id]
        base = {
            "source_id": source_id,
            "title": card.get("titles", {}).get("uk"),
            "year": card.get("year"),
            "denomination": card.get("denomination", {}).get("value"),
        }

        if result["status"] == "match" and result["row"].id not in contested:
            row = result["row"]
            card["ua_coins"] = {
                "id": row.id,
                "url": row.url,
                "matched_by": (
                    "shared_listing" if row.id in is_shared else result["matched_by"]
                ),
                "year_used": result["year_used"],
                "matched_at": matched_at,
            }
            continue

        card["ua_coins"] = None

        if result["status"] == "match" and result["row"].id in contested:
            other = [sid for sid in claims[result["row"].id] if sid != source_id]
            unmatched_entries.append(
                {
                    **base,
                    "reason": "conflict: matched ua-coins row also claimed by another card",
                    "conflict_with": other,
                    "ua_coins_id": result["row"].id,
                }
            )
        elif result["status"] == "conflict":
            unmatched_entries.append(
                {
                    **base,
                    "reason": "conflict: more than one ua-coins candidate",
                    "candidates": [
                        {"id": r.id, "url": r.url, "title": r.title, "year": r.year}
                        for r in result["candidates"]
                    ],
                }
            )
        else:
            unmatched_entries.append(
                {
                    **base,
                    "reason": result.get("reason", "unmatched"),
                    "candidates_seen": result.get("candidates_seen", []),
                }
            )

    return cards, unmatched_entries
