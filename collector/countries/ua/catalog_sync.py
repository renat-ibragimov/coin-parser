"""Daily, shallow NBU catalogue sync: recent cards -> review drafts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import psycopg
from selectolax.parser import HTMLParser

from collector.core.db import get_database_url
from collector.core.object_storage import upload_catalog_media
from collector.core.staging import DEFAULT_STAGING_ROOT
from collector.countries.ua.nbu_client import (
    BASE_URL,
    LOCALE_URL_SEGMENT,
    SEARCH_PATH,
    USER_AGENT,
    extract_card_id,
)
from collector.countries.ua.normalize import normalize_match
from collector.countries.ua.parsing import parse_cards
from collector.countries.ua.series import find_official_series, load_series_json

RECENT_LIMIT = 25


@dataclass
class CatalogSyncSummary:
    scanned: int = 0
    known: int = 0
    drafted: int = 0
    uploaded: int = 0
    new_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def exit_code(self) -> int:
        return 2 if self.error else (1 if self.warnings else 0)

    def report_payload(self) -> dict:
        status = "failed" if self.error else ("partial" if self.warnings else "ok")
        details = "\n".join(([self.error] if self.error else []) + self.warnings) or None
        return {
            "status": status,
            "summary": (
                f"nbu-catalog-sync scanned={self.scanned} new={len(self.new_ids)} "
                f"drafted={self.drafted}"
            ),
            "details": details,
            "stats": {
                "scanned": self.scanned,
                "known": self.known,
                "new": len(self.new_ids),
                "drafted": self.drafted,
                "uploaded": self.uploaded,
                "warnings": len(self.warnings),
            },
            "exitCode": self.exit_code,
        }

    def print_report(self) -> None:
        print(self.report_payload()["summary"])
        for warning in self.warnings:
            print(f"[update-catalog] WARNING: {warning}")
        if self.error:
            print(f"[update-catalog] ERROR: {self.error}")


def _series_by_id(html: str) -> tuple[dict[str, str], list[str]]:
    result: dict[str, str] = {}
    idless: list[str] = []
    for node in HTMLParser(html).css("div.search-result"):
        title_node = node.css_first("div.title")
        title = " ".join(title_node.text().split()) if title_node else "?"
        card_id = extract_card_id(node)
        if card_id is None:
            idless.append(title)
            continue
        tag = node.css_first("div.tag")
        result[card_id] = " ".join(tag.text().split()) if tag else ""
    return result, idless


def _known_ids(conn: psycopg.Connection, source_ids: list[str]) -> set[str]:
    if not source_ids:
        return set()
    rows = conn.execute(
        "SELECT source_key FROM catalog_items WHERE created_by IS NULL AND source_key = ANY(%s)",
        (source_ids,),
    ).fetchall()
    return {row[0] for row in rows}


def _unknown_idless_titles(conn: psycopg.Connection, titles: list[str]) -> list[str]:
    if not titles:
        return []
    rows = conn.execute(
        "SELECT title_original, title_uk FROM catalog_items WHERE created_by IS NULL"
    ).fetchall()
    known = {
        normalize_match(title)
        for row in rows
        for title in row
        if isinstance(title, str) and title
    }
    return [title for title in titles if normalize_match(title) not in known]


def update_catalog(
    *, dsn: str | None = None, staging_root: Path = DEFAULT_STAGING_ROOT
) -> CatalogSyncSummary:
    # Imports stay lazy: only this container mode needs the heavy photo stack.
    from collector.countries.ua.parser import ParserUkraine

    summary = CatalogSyncSummary()
    try:
        pages: dict[str, str] = {}
        with httpx.Client(
            base_url=BASE_URL, headers={"User-Agent": USER_AGENT}, timeout=30.0
        ) as client:
            for locale in ("uk",):
                response = client.post(
                    SEARCH_PATH.format(locale_path=LOCALE_URL_SEGMENT[locale]),
                    data={"page": 1, "perPage": RECENT_LIMIT, "category[]": "Coin"},
                )
                response.raise_for_status()
                pages[locale] = response.text

        series_by_id, idless = _series_by_id(pages["uk"])
        summary.scanned = len(HTMLParser(pages["uk"]).css("div.search-result"))
        uk = {c["source_id"]: c for c in parse_cards(pages["uk"], "uk")}
        source_ids = [f"nbu:{card_id}" for card_id in uk]
        with psycopg.connect(dsn or get_database_url()) as conn:
            known = _known_ids(conn, source_ids)
            unknown_idless = _unknown_idless_titles(conn, idless)
        summary.warnings.extend(
            f"card has no NBU id and no title match; skipped: {title}"
            for title in unknown_idless
        )
        missing = [source_id for source_id in source_ids if source_id not in known]
        summary.known = len(known)
        summary.new_ids = sorted(missing, key=lambda value: int(value.split(":")[1]))
        if not missing:
            summary.print_report()
            return summary

        dictionary = load_series_json()
        grouped: dict[str, set[str]] = {}
        for source_id in missing:
            card_id = source_id.split(":", 1)[1]
            series_name = series_by_id.get(card_id, "")
            entry = find_official_series(dictionary, series_name)
            if entry is None:
                summary.warnings.append(f"{source_id}: unknown series {series_name!r}")
                continue
            grouped.setdefault(entry["names"]["uk"], set()).add(source_id)

        for series_name, wanted_ids in grouped.items():
            parser = ParserUkraine(series=series_name, staging_root=staging_root)
            assert parser.staging is not None
            parser.fetch()
            parsed = parser.parse()
            cards = [card for card in parsed.cards if card["source_id"] in wanted_ids]
            missing_after_parse = wanted_ids - {card["source_id"] for card in cards}
            summary.warnings.extend(
                f"{source_id}: missing after full-series parse"
                for source_id in sorted(missing_after_parse)
            )
            if not cards:
                continue
            data = parser.staging.read_parsed()
            data["cards"] = cards
            parser.staging.write_parsed(data)
            parser.match_ua_coins(refresh=True)
            parser.fetch_prices(refresh=True)
            parser.fetch_photos(refresh=True)
            parser.process_photos()

            photos = json.loads((parser.staging.parsed_dir / "photos.json").read_text())
            ready: set[str] = set()
            for photo in photos.get("cards", []):
                roles = photo.get("roles") or {}
                has_both = all(
                    roles.get(role, {}).get("winner")
                    for role in ("obverse", "reverse")
                )
                if photo.get("anomalies") or not has_both:
                    summary.warnings.append(f"{photo.get('source_id')}: photo review failed")
                else:
                    ready.add(photo["source_id"])
            if not ready:
                continue
            summary.uploaded += upload_catalog_media(parser.staging.dir, ready)
            loaded = parser.load_cards(insert_status="draft", only_source_ids=ready)
            if loaded.error:
                raise RuntimeError(loaded.error)
            summary.drafted += sum(card.action == "insert" for card in loaded.cards)
            prices = parser.load_prices()
            if prices.error:
                summary.warnings.append(f"{series_name}: prices: {prices.error}")
    except Exception as exc:
        summary.error = f"{type(exc).__name__}: {exc}"
    summary.print_report()
    return summary
