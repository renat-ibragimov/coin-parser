"""Builds and maintains countries/ua/series.json -- the closed dictionary
of NBU souvenir-coin series (uk/en name pairs, card counts, year ranges).
Committed to the repo, like vocab.py -- not staging.

Why this exists: NBU's own en-locale series name is NOT a translation of
the uk name in any derivable way (different alphabetical sort order in
each locale's dropdown, different counts -- see docs/01_findings.md), so
there is no way to query the en locale for "the same series" other than
knowing the answer already. This module discovers the uk<->en pairing the
only reliable way available: by which card ids show up under each name,
in each locale, on the live site -- never by comparing name text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
from selectolax.parser import HTMLParser

from collector.core.pacing import Pacer
from collector.core.staging import DEFAULT_STAGING_ROOT, slugify
from collector.countries.ua.nbu_client import (
    BASE_URL,
    LISTING_PATH,
    LOCALE_URL_SEGMENT,
    PER_PAGE,
    REQUEST_DELAY_RANGE,
    SEARCH_PATH,
    USER_AGENT,
)
from collector.countries.ua.normalize import normalize_match
from collector.countries.ua.parsing import LABELS, _parse_circulation_date, parse_cards

SERIES_JSON_PATH = Path(__file__).parent / "series.json"

# The "Any" placeholder option in the Серія/Serie dropdown -- observed as
# an empty value="" in both locales' <select>; the literal phrases are
# kept too as a defensive fallback in case NBU ever renders it as text.
_PLACEHOLDER_VALUES = {"", "не вказується", "not specified", "any"}


@dataclass
class SeriesCollectSummary:
    entries: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    written: bool = False

    def print_report(self) -> None:
        print("[series] NBU series dictionary scan")
        if self.entries:
            print(
                f"[series]   {'slug':<45} {'uk':<30} {'en':<30} {'off':<5} "
                f"{'cards':<6} years"
            )
            for e in self.entries:
                names = e.get("names", {})
                uk_name = (names.get("uk") or "?")[:30]
                en_name = (names.get("en") or "-")[:30]
                official = "yes" if e.get("is_official") else "no"
                cards = e.get("nbu_card_count")
                cards_str = str(cards) if cards is not None else "-"
                yr = e.get("year_range")
                yr_str = f"{yr[0]}-{yr[1]}" if yr else "-"
                flag = " (missing_from_nbu)" if e.get("missing_from_nbu") else ""
                print(
                    f"[series]   {e.get('slug', '?'):<45} {uk_name:<30} {en_name:<30} "
                    f"{official:<5} {cards_str:<6} {yr_str}{flag}"
                )
        for w in self.warnings:
            print(f"[series]   WARNING: {w}")
        for c in self.conflicts:
            print(f"[series]   CONFLICT: {c}")
        official_n = sum(1 for e in self.entries if e.get("is_official"))
        curated_n = sum(1 for e in self.entries if not e.get("is_official"))
        print(f"[series] official {official_n}, curated {curated_n}, warnings {len(self.warnings)}")
        if self.written:
            print("[series] review: git diff countries/ua/series.json")
        elif self.conflicts:
            print("[series] series.json NOT written -- resolve conflicts above first")


# ---------------------------------------------------------------------- #
# scraping
# ---------------------------------------------------------------------- #


def _fetch_series_options(
    client: httpx.Client, locale: str, staging_dir: Path, pacer: Pacer
) -> list[str]:
    pacer.wait()
    path = LISTING_PATH.format(locale_path=LOCALE_URL_SEGMENT[locale])
    resp = client.get(path)
    resp.raise_for_status()
    html = resp.text
    (staging_dir / f"{locale}_listing.html").write_text(html, encoding="utf-8")

    tree = HTMLParser(html)
    select = tree.css_first("#serie")
    if select is None:
        raise RuntimeError(
            f"{locale}: could not find the series <select id=\"serie\"> on the listing page "
            "-- NBU may have changed the page layout"
        )
    names: list[str] = []
    seen = set()
    for option in select.css("option"):
        # NBU's own serie[] filter is an exact-string match, and at least
        # two of its own option values carry a trailing space that's part
        # of the value it expects back (verified live: querying "My
        # Immortal Ukraine" -> 0 cards, "My Immortal Ukraine " -> 16).
        # Only use a stripped copy to detect the empty/placeholder
        # option -- the value we keep must stay byte-exact.
        value = option.attributes.get("value") or ""
        stripped = value.strip()
        if not stripped or stripped.lower() in _PLACEHOLDER_VALUES:
            continue
        if value not in seen:
            seen.add(value)
            names.append(value)
    return names


def _fetch_series_cards(
    client: httpx.Client,
    locale: str,
    name: str,
    staging_dir: Path,
    warnings: list[str],
    pacer: Pacer,
) -> tuple[set[str], list[int]]:
    """All card ids under `name` in `locale`, plus (uk only) the years
    parsed from each card's circulation date."""
    ids: set[str] = set()
    years: list[int] = []
    page = 1
    slug_part = slugify(name)
    while True:
        pacer.wait()
        path = SEARCH_PATH.format(locale_path=LOCALE_URL_SEGMENT[locale])
        resp = client.post(
            path, data={"page": page, "perPage": PER_PAGE, "serie[]": name}
        )
        resp.raise_for_status()
        html = resp.text
        (staging_dir / f"{locale}__{slug_part}_p{page}.html").write_text(
            html, encoding="utf-8"
        )

        # Pagination MUST be decided from the raw block count, not from
        # `cards` (parse_cards() silently drops a block it can't extract
        # an id from -- e.g. a brand-new NBU listing with no photo
        # uploaded yet, see docs/01_findings.md). Using len(cards) here
        # made a page with even one such unparseable block look short
        # and stopped pagination early, truncating the whole series scan
        # (found live: "Інші монети" undercounted 99 vs the real 149
        # because page 1 had exactly one photo-less card).
        raw_block_count = len(HTMLParser(html).css("div.search-result"))
        cards = parse_cards(html, locale)
        if len(cards) != raw_block_count:
            warnings.append(
                f"{name} ({locale}) page {page}: {raw_block_count - len(cards)} card(s) "
                "on this page failed to parse (likely missing photo) and were skipped"
            )
        for card in cards:
            ids.add(card["source_id"])
            if locale == "uk":
                raw_date = card["marks"].get(LABELS["uk"]["circulation_date"])
                _, year = _parse_circulation_date(raw_date, warnings, card["source_id"])
                if year is not None:
                    years.append(year)

        if raw_block_count < PER_PAGE:
            break
        page += 1

    return ids, years


def _pair_series(
    uk_id_sets: dict[str, set[str]],
    en_id_sets: dict[str, set[str]],
    warnings: list[str],
) -> dict[str, str | None]:
    """Pair each uk series name with the en series name whose card-id set
    overlaps it most -- ids only, never name text. Greedy by descending
    overlap size so no en name gets claimed by two different uk names.
    """
    scored = []
    for uk_name, uk_ids in uk_id_sets.items():
        if not uk_ids:
            continue
        for en_name, en_ids in en_id_sets.items():
            overlap = len(uk_ids & en_ids)
            if overlap > 0:
                scored.append((overlap, uk_name, en_name))
    scored.sort(key=lambda t: -t[0])

    uk_assigned: dict[str, str] = {}
    en_used: set[str] = set()
    for overlap, uk_name, en_name in scored:
        if uk_name in uk_assigned or en_name in en_used:
            continue
        uk_assigned[uk_name] = en_name
        en_used.add(en_name)
        uk_ids = uk_id_sets[uk_name]
        en_ids = en_id_sets[en_name]
        if uk_ids != en_ids:
            warnings.append(
                f"series pairing incomplete: uk={uk_name!r} ({len(uk_ids)} ids) <-> "
                f"en={en_name!r} ({len(en_ids)} ids), overlap={overlap}"
            )

    for uk_name, uk_ids in uk_id_sets.items():
        if uk_ids and uk_name not in uk_assigned:
            warnings.append(f"series {uk_name!r} has no en pair (no id overlap with any en series)")

    for en_name, en_ids in en_id_sets.items():
        if en_ids and en_name not in en_used:
            warnings.append(f"en series {en_name!r} has no uk pair (no id overlap with any uk series)")

    return uk_assigned


# ---------------------------------------------------------------------- #
# validation
# ---------------------------------------------------------------------- #


def _validate_entry(entry: dict) -> list[str]:
    errors = []
    slug = entry.get("slug") or "?"
    is_official = entry.get("is_official")
    if is_official is True:
        if "membership" in entry:
            errors.append(f"{slug}: is_official=true must not have 'membership'")
    elif is_official is False:
        membership = entry.get("membership")
        if not membership:
            errors.append(f"{slug}: is_official=false requires 'membership'")
        elif not (
            isinstance(membership.get("title_prefix"), str)
            or isinstance(membership.get("card_ids"), list)
        ):
            errors.append(
                f"{slug}: 'membership' must have a string 'title_prefix' or a list 'card_ids'"
            )
    else:
        errors.append(f"{slug}: 'is_official' must be true or false, got {is_official!r}")
    if not entry.get("names", {}).get("uk"):
        errors.append(f"{slug}: missing names.uk")
    if not entry.get("slug"):
        errors.append(f"entry missing 'slug': {entry!r}")
    return errors


def _load_existing(path: Path) -> tuple[list[dict], list[str]]:
    if not path.exists():
        return [], []
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("series", [])
    errors = []
    for entry in entries:
        errors.extend(_validate_entry(entry))
    return entries, errors


# ---------------------------------------------------------------------- #
# merge
# ---------------------------------------------------------------------- #


def _merge(
    fresh_official: list[dict], existing_entries: list[dict], warnings: list[str]
) -> tuple[list[dict] | None, list[str]]:
    """Returns (merged_entries, conflicts). merged_entries is None if
    conflicts is non-empty -- caller must not write the file then."""
    existing_official = [e for e in existing_entries if e.get("is_official") is True]
    existing_curated = [e for e in existing_entries if e.get("is_official") is False]

    conflicts: list[str] = []
    used_slugs = {e["slug"] for e in existing_entries if e.get("slug")}
    seen_existing_slugs: set[str] = set()

    merged_official: list[dict] = []
    for fresh in fresh_official:
        fresh_uk = fresh["names"]["uk"]

        match = next(
            (
                e
                for e in existing_official
                if normalize_match(e["names"]["uk"]) == normalize_match(fresh_uk)
            ),
            None,
        )
        if match is not None:
            fresh["slug"] = match["slug"]
            seen_existing_slugs.add(match["slug"])
            merged_official.append(fresh)
            continue

        curated_match = next(
            (
                e
                for e in existing_curated
                if normalize_match(e["names"]["uk"]) == normalize_match(fresh_uk)
            ),
            None,
        )
        if curated_match is not None:
            conflicts.append(
                f"official series {fresh_uk!r} from NBU now matches curated entry "
                f"{curated_match['slug']!r} (names.uk={curated_match['names']['uk']!r}) "
                "-- was this series legitimized by NBU? Resolve by hand."
            )
            continue

        slug = slugify(fresh_uk)
        base_slug, n = slug, 2
        while slug in used_slugs:
            slug = f"{base_slug}-{n}"
            n += 1
        used_slugs.add(slug)
        fresh["slug"] = slug
        merged_official.append(fresh)

    if conflicts:
        return None, conflicts

    for existing in existing_official:
        if existing["slug"] not in seen_existing_slugs:
            carried = dict(existing)
            carried["missing_from_nbu"] = True
            warnings.append(
                f"official series {existing['names']['uk']!r} (slug={existing['slug']!r}) "
                "no longer in NBU's series filter -- kept, marked missing_from_nbu"
            )
            merged_official.append(carried)

    merged = merged_official + [dict(e) for e in existing_curated]
    merged.sort(key=lambda e: e["slug"])
    return merged, []


# ---------------------------------------------------------------------- #
# entry point
# ---------------------------------------------------------------------- #


def collect_series(staging_root: Path = DEFAULT_STAGING_ROOT) -> SeriesCollectSummary:
    warnings: list[str] = []
    staging_dir = staging_root / "ua" / "_series" / "raw"
    staging_dir.mkdir(parents=True, exist_ok=True)

    existing_entries, validation_errors = _load_existing(SERIES_JSON_PATH)
    if validation_errors:
        summary = SeriesCollectSummary(
            warnings=validation_errors,
            conflicts=[f"existing series.json failed validation: {e}" for e in validation_errors],
        )
        summary.print_report()
        return summary

    pacer = Pacer(REQUEST_DELAY_RANGE)
    with httpx.Client(
        base_url=BASE_URL, headers={"User-Agent": USER_AGENT}, timeout=30.0
    ) as client:
        uk_names = _fetch_series_options(client, "uk", staging_dir, pacer)
        en_names = _fetch_series_options(client, "en", staging_dir, pacer)

        uk_id_sets: dict[str, set[str]] = {}
        uk_years: dict[str, list[int]] = {}
        for i, name in enumerate(uk_names, 1):
            print(f"[series]   scanning uk {i}/{len(uk_names)}: {name!r}")
            ids, years = _fetch_series_cards(client, "uk", name, staging_dir, warnings, pacer)
            uk_id_sets[name] = ids
            uk_years[name] = years

        en_id_sets: dict[str, set[str]] = {}
        for i, name in enumerate(en_names, 1):
            print(f"[series]   scanning en {i}/{len(en_names)}: {name!r}")
            ids, _years = _fetch_series_cards(client, "en", name, staging_dir, warnings, pacer)
            en_id_sets[name] = ids

    pairing = _pair_series(uk_id_sets, en_id_sets, warnings)

    fresh_official = []
    for uk_name, ids in uk_id_sets.items():
        years = uk_years[uk_name]
        fresh_official.append(
            {
                "is_official": True,
                "names": {"uk": uk_name, "en": pairing.get(uk_name)},
                "nbu_card_count": len(ids),
                "year_range": [min(years), max(years)] if years else None,
            }
        )

    merged, conflicts = _merge(fresh_official, existing_entries, warnings)

    if conflicts:
        summary = SeriesCollectSummary(
            entries=fresh_official, warnings=warnings, conflicts=conflicts, written=False
        )
        summary.print_report()
        return summary

    data = {
        "country": "ua",
        "original_lang": "uk",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "series": merged,
    }
    SERIES_JSON_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    summary = SeriesCollectSummary(entries=merged, warnings=warnings, conflicts=[], written=True)
    summary.print_report()
    return summary


# ---------------------------------------------------------------------- #
# lookup helpers (used by parser.py's fetch())
# ---------------------------------------------------------------------- #


def load_series_json(path: Path = SERIES_JSON_PATH) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def find_official_series(data: dict | None, uk_name: str) -> dict | None:
    """Look up a series entry -- official (NBU-catalogued) or curated
    (is_official=false, membership-based, see the schema note in
    _validate_entry) -- by names.uk, tolerant of apostrophe-variant/
    homoglyph differences (normalize_match). The name is kept despite
    also matching curated entries: every caller uses it to resolve
    `--series "<name>"` into its series.json entry regardless of which
    kind it turns out to be, and a rename would touch call sites for no
    behavioural gain.
    """
    if not data:
        return None
    target = normalize_match(uk_name)
    for entry in data.get("series", []):
        if normalize_match(entry.get("names", {}).get("uk", "")) == target:
            return entry
    return None
