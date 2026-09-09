"""Matches NBU cards to their catalog page on ua-coins.info.

Deterministic only -- (normalize_match(title), year, denomination) as the
key, with year_shift as the one explicit fallback (NBU's own circulation
year and the coin's face year can differ by one). Never matches by name
similarity/fuzzy scoring; ambiguity (more than one candidate on either
side) is reported as a conflict, not silently resolved.

Prices, price history, and photos are explicitly out of scope here --
this step only records the URL/id of the matching ua-coins.info page.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser

from collector.core.pacing import Pacer
from collector.countries.ua.nbu_client import USER_AGENT
from collector.countries.ua.normalize import normalize_match, split_packaging
from collector.countries.ua.parsing import _parse_float

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

_ID_RE = re.compile(r"^(\d+)")


def staging_dir(staging_root: Path) -> Path:
    return staging_root / "ua" / "_ua_coins" / "raw"


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
    (respecting a Retry-After header if the server sends one). Shared by
    every ua-coins.info caller in this adapter (catalog pages, coin
    detail pages, image downloads, the signed price-chart endpoint) --
    one rate-limit policy for the one host, so nothing accidentally
    paces or retries differently.

    `headers` are merged over the client's own for this one request; the
    price-chart endpoint needs a Referer naming the coin page whose
    render minted its signature (see prices.py).
    """
    for attempt in range(1, MAX_429_RETRIES + 1):
        pacer.wait()
        resp = client.get(path, headers=headers)
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
        slug_part = href.rstrip("/").rsplit("/", 1)[-1]
        id_match = _ID_RE.match(slug_part)
        if id_match is None:
            continue
        row_id = int(id_match.group(1))

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


def match_cards(
    cards: list[dict], rows_by_year: dict[int, list[UaCoinsRow]], matched_at: str
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
    # though each card individually looked like a unique match.
    claims: dict[int, list[str]] = {}
    for source_id, result in per_card.items():
        if result["status"] == "match":
            claims.setdefault(result["row"].id, []).append(source_id)
    contested = {row_id for row_id, holders in claims.items() if len(holders) > 1}

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
                "matched_by": result["matched_by"],
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
