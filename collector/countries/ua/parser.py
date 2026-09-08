"""NBU (National Bank of Ukraine) souvenir-coin catalog parser.

Three independent steps:
  fetch()          -- network -> staging/ua/<slug>/raw/*.html
  parse()          -- staging/ua/<slug>/raw/*.html -> staging/ua/<slug>/parsed/cards.json (no network)
  collect_series()  -- network -> countries/ua/series.json (committed, not staging)

fetch()/parse() require a known series (see countries/ua/series.json,
built by collect_series()); collect_series() operates on the whole
catalog and does not need one.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
from selectolax.parser import HTMLParser

from collector.core.staging import DEFAULT_STAGING_ROOT, SeriesStaging
from collector.countries.ua.nbu_client import (
    BASE_URL,
    LOCALE_URL_SEGMENT,
    PER_PAGE,
    REQUEST_DELAY_RANGE,
    SEARCH_PATH,
    USER_AGENT,
)
from collector.countries.ua.parsing import CardAnomaly, build_canonical_card, parse_cards
from collector.countries.ua.series import collect_series, find_official_series, load_series_json

META_FILENAME = "_meta.json"

# NBU's `serie[]` filter does an exact string match, and NBU itself isn't
# consistent about which apostrophe-look-alike character it uses in a
# series name across its own database (seen: a grave accent "`" where a
# straight apostrophe "'" would be expected, e.g. "пам`ятки"). series.json
# (built by collect_series()) already holds the exact spelling NBU's
# filter actually wants, so this only matters as a last-resort safety net
# -- e.g. if someone calls fetch() with a hand-typed name that merely
# *resembles* a dictionary entry closely enough for find_official_series's
# normalize_match to accept it. If the query as given returns nothing,
# fetch() retries it with each of these substituted in turn before giving up.
_APOSTROPHE_VARIANTS = ["'", "`", "’", "ʼ"]


def _apostrophe_candidates(name: str) -> list[str]:
    """`name` plus variants with every apostrophe-look-alike character it
    contains swapped for each other known variant. `name` itself is
    always first. Returns just [name] if it has none of these chars."""
    present = [c for c in _APOSTROPHE_VARIANTS if c in name]
    if not present:
        return [name]
    candidates = [name]
    for repl in _APOSTROPHE_VARIANTS:
        candidate = name
        for c in present:
            candidate = candidate.replace(c, repl)
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


@dataclass
class FetchLocaleSummary:
    locale: str
    pages: int = 0
    cards_seen: int = 0
    bytes_downloaded: int = 0


@dataclass
class FetchSummary:
    series: str
    locales: dict[str, FetchLocaleSummary] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def print_report(self) -> None:
        print(f"[fetch] series: {self.series}")
        for locale, s in self.locales.items():
            print(
                f"[fetch]   {locale}: {s.pages} page(s), "
                f"{s.cards_seen} card(s) seen, {s.bytes_downloaded} bytes"
            )
        for w in self.warnings:
            print(f"[fetch]   WARNING: {w}")


@dataclass
class ParseSummary:
    series: str
    uk_count: int
    en_count: int
    matched_count: int
    warnings: list[str] = field(default_factory=list)
    anomalies: list[dict] = field(default_factory=list)
    cards: list[dict] = field(default_factory=list)

    def print_report(self) -> None:
        print(f"[parse] series: {self.series}")
        print(
            f"[parse]   uk cards: {self.uk_count}, en cards: {self.en_count}, "
            f"matched: {self.matched_count}, anomalies: {len(self.anomalies)}"
        )
        if self.cards:
            print(f"[parse]   {'source_id':<10} {'title':<30} {'denom':<10} material")
            for c in self.cards:
                title = c["titles"]["uk"] or "?"
                denom = c["denomination"]
                denom_str = (
                    f"{denom['value']} {denom['unit']}" if denom["value"] is not None else "?"
                )
                print(
                    f"[parse]   {c['source_id']:<10} {title:<30} {denom_str:<10} "
                    f"{c['material'] or '?'}"
                )
        for w in self.warnings:
            print(f"[parse]   WARNING: {w}")
        for a in self.anomalies:
            print(
                f"[parse]   ANOMALY: {a['source_id']} {a['field']}="
                f"{a['raw_value']!r} — {a['message']}"
            )


class ParserUkraine:
    """NBU souvenir-coin adapter.

    ORIGINAL_LANG is a fixed, adapter-level constant written into every
    cards.json this parser produces (as the root "original_lang" field).
    It tells a consumer which locale in titles/description is the
    authoritative source text -- for Ukraine that's always "uk", so it's
    a constant here rather than a per-card field. A future country with a
    different primary language (e.g. Poland) sets its own constant
    ("pl") in its own adapter; the shape of titles/description does not
    change, only which locale key counts as the original.

    `series` is required for fetch()/parse() but not for collect_series(),
    which operates on the whole catalog -- pass None (the default) when
    only calling collect_series().
    """

    ORIGINAL_LANG = "uk"

    def __init__(self, series: str | None = None, staging_root: Path = DEFAULT_STAGING_ROOT):
        self.series = series
        self.staging_root = staging_root
        self.staging = SeriesStaging("ua", series, root=staging_root) if series is not None else None

    # ------------------------------------------------------------------ #
    # fetch
    # ------------------------------------------------------------------ #

    def fetch(self) -> FetchSummary:
        if self.series is None or self.staging is None:
            raise RuntimeError("fetch() requires a series name (pass series=... to ParserUkraine)")

        series_dict = load_series_json()
        entry = find_official_series(series_dict, self.series)
        if entry is None:
            raise RuntimeError(
                f"unknown series {self.series!r} — not found in countries/ua/series.json. "
                "Run `python -m collector ua --step series` first to (re)build the dictionary."
            )

        summary = FetchSummary(series=self.series)

        query_by_locale = {"uk": entry["names"]["uk"]}
        en_name = entry["names"].get("en")
        if en_name is None:
            summary.warnings.append(
                f"series {self.series!r} has no English name recorded in series.json "
                "(NBU has no en-locale equivalent for it, or the dictionary is stale — "
                "rerun --step series if that seems wrong) — skipping en fetch"
            )
        else:
            query_by_locale["en"] = en_name

        self.staging.ensure_dirs()
        fetched_at = datetime.now(timezone.utc).isoformat()
        resolved_uk_series = entry["names"]["uk"]

        with httpx.Client(
            base_url=BASE_URL, headers={"User-Agent": USER_AGENT}, timeout=30.0
        ) as client:
            first_request = True

            def fetch_pages(locale: str, query_value: str) -> FetchLocaleSummary:
                nonlocal first_request
                loc_summary = FetchLocaleSummary(locale=locale)
                page = 1
                while True:
                    if not first_request:
                        time.sleep(random.uniform(*REQUEST_DELAY_RANGE))
                    first_request = False

                    path = SEARCH_PATH.format(locale_path=LOCALE_URL_SEGMENT[locale])
                    resp = client.post(
                        path,
                        data={"page": page, "perPage": PER_PAGE, "serie[]": query_value},
                    )
                    resp.raise_for_status()
                    html = resp.text

                    self.staging.write_raw(f"{locale}_p{page}.html", html)
                    loc_summary.pages += 1
                    loc_summary.bytes_downloaded += len(resp.content)

                    card_count = len(HTMLParser(html).css("div.search-result"))
                    loc_summary.cards_seen += card_count

                    if card_count < PER_PAGE:
                        break
                    page += 1

                return loc_summary

            for locale, query_value in query_by_locale.items():
                candidates = _apostrophe_candidates(query_value)
                loc_summary = fetch_pages(locale, candidates[0])
                used_value = candidates[0]

                for candidate in candidates[1:]:
                    if loc_summary.cards_seen > 0:
                        break
                    loc_summary = fetch_pages(locale, candidate)
                    used_value = candidate

                if used_value != query_value:
                    summary.warnings.append(
                        f"{locale}: series name {query_value!r} matched 0 cards; "
                        f"retried with apostrophe variant and used {used_value!r} instead"
                    )
                    if locale == "uk":
                        resolved_uk_series = used_value
                elif loc_summary.cards_seen == 0:
                    summary.warnings.append(
                        f"{locale}: series name {query_value!r} matched 0 cards, even "
                        "after trying apostrophe variants — check the exact spelling "
                        "against the \"Серія\"/\"Serie\" dropdown on bank.gov.ua"
                    )

                summary.locales[locale] = loc_summary

        nbu_card_count = entry.get("nbu_card_count")
        uk_locale_summary = summary.locales.get("uk")
        uk_seen = uk_locale_summary.cards_seen if uk_locale_summary is not None else None
        if nbu_card_count is not None and uk_seen is not None and uk_seen != nbu_card_count:
            summary.warnings.append(
                f"dictionary stale: series.json says nbu_card_count={nbu_card_count}, "
                f"but this fetch saw {uk_seen} — rerun --step series"
            )

        meta = {
            "fetched_at": fetched_at,
            "series": {"uk": resolved_uk_series, "en": en_name},
        }
        (self.staging.raw_dir / META_FILENAME).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        summary.print_report()
        return summary

    # ------------------------------------------------------------------ #
    # parse
    # ------------------------------------------------------------------ #

    def parse(self) -> ParseSummary:
        if self.series is None or self.staging is None:
            raise RuntimeError("parse() requires a series name (pass series=... to ParserUkraine)")

        uk_files = self.staging.raw_files("uk_p*.html")
        en_files = self.staging.raw_files("en_p*.html")
        if not uk_files and not en_files:
            raise RuntimeError(
                f"no raw data in {self.staging.raw_dir} — run fetch first"
            )

        uk_cards_raw = [
            card
            for f in uk_files
            for card in parse_cards(f.read_text(encoding="utf-8"), "uk")
        ]
        en_cards_raw = [
            card
            for f in en_files
            for card in parse_cards(f.read_text(encoding="utf-8"), "en")
        ]

        uk_by_id = {c["source_id"]: c for c in uk_cards_raw}
        en_by_id = {c["source_id"]: c for c in en_cards_raw}

        warnings: list[str] = []
        anomalies: list[dict] = []
        cards = []
        for source_id in sorted(set(uk_by_id) | set(en_by_id), key=int):
            uk = uk_by_id.get(source_id)
            en = en_by_id.get(source_id)
            if uk is None:
                warnings.append(f"nbu:{source_id}: has en card but no uk card, skipped")
                continue
            if en is None:
                warnings.append(f"nbu:{source_id}: no matching en card")
            try:
                cards.append(build_canonical_card(source_id, uk, en, warnings))
            except CardAnomaly as exc:
                anomalies.append(
                    {
                        "source_id": f"nbu:{source_id}",
                        "field": exc.field,
                        "raw_value": exc.raw_value,
                        "message": str(exc),
                    }
                )

        meta_path = self.staging.raw_dir / META_FILENAME
        series_dict = load_series_json()
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            fetched_at = meta.get("fetched_at")
            # fetch() may have resolved self.series to a different
            # apostrophe-variant spelling to match NBU's own (inconsistent)
            # serie[] value -- meta.json has the spelling that actually
            # worked, prefer it over the as-typed self.series.
            series_uk = meta.get("series", {}).get("uk") or self.series
            series_en = meta.get("series", {}).get("en")
        else:
            warnings.append("no fetch metadata found, using current time as fetched_at")
            fetched_at = datetime.now(timezone.utc).isoformat()
            series_uk = self.series
            entry = find_official_series(series_dict, self.series)
            series_en = entry["names"].get("en") if entry else None

        data = {
            "series": {"uk": series_uk, "en": series_en},
            "original_lang": self.ORIGINAL_LANG,
            "fetched_at": fetched_at,
            "cards": cards,
        }
        self.staging.write_parsed(data)
        self.staging.write_anomalies(anomalies)

        summary = ParseSummary(
            series=self.series,
            uk_count=len(uk_cards_raw),
            en_count=len(en_cards_raw),
            matched_count=len(cards),
            warnings=warnings,
            anomalies=anomalies,
            cards=cards,
        )
        summary.print_report()
        return summary

    # ------------------------------------------------------------------ #
    # series dictionary
    # ------------------------------------------------------------------ #

    def collect_series(self):
        return collect_series(staging_root=self.staging_root)
