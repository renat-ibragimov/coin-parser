"""Loads the staged ua-coins price history into coin_keeper's
market_price_snapshots. The second step in this repo that writes to the
production database (after load_series).

Scope boundary, on purpose: prices are only loaded for coins the writer
has ALREADY put in catalog_items. A coin whose source_key is not in the
database is reported as skipped:not_in_db and is not an error -- price
history is an attribute of a catalogued coin, and inventing the coin
from a price file would be the wrong step doing it.

The legacy uCoin history of a coin is dropped here too, but only with
--drop-ucoin-prices and only for the coins this very run just loaded a
ua-coins history for. Scoped that way it cannot leave a coin priceless,
and it is per series by design: a catalogue-wide sweep would strike
coins ua-coins does not quote at all (the US ones, mostly), whose only
prices are those very rows.

Volume is why nothing here inserts row by row: a country's full history
runs to hundreds of thousands of points, and a per-row INSERT over an
ssh tunnel is not a viable shape for that. Everything goes through one
COPY into a temp table and one INSERT ... SELECT out of it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from collector.core.db import existing_columns, get_database_url
from collector.core.staging import DEFAULT_STAGING_ROOT
from collector.countries.ua.prices import (
    PricePoint,
    PriceSeriesAnomaly,
    load_price_file,
    prices_path,
)

TABLE = "market_price_snapshots"
SOURCE = "UA-Coins"
# The legacy importer's label. Its numbers are unusable on precious metal
# (seen: 8.13 UAH against ua-coins' 100600 for the same gold coin) and
# they are dated later than some ua-coins histories, so they win the
# storefront's "latest snapshot" pick and show as the coin's price. They
# are dropped per series, never catalogue-wide -- see _drop_ucoin.
UCOIN_SOURCE = "uCoin"
CURRENCY = "UAH"
GRADE = None  # ua-coins quotes one price per coin, ungraded

# ua-coins does not settle a day's quote when the day starts: it
# recomputes during the day, and it revises days already closed (seen:
# nbu:418 on 2026-09-04, 299 when we read it, 295 a week later). One
# snapshot per date with no way back would keep whichever number we
# happened to read first, so the last REFRESH_WINDOW_DAYS days stay
# open to correction -- a row inside the window whose price no longer
# matches the source is updated in place, keeping its id and its date.
# Older rows are never touched: past that horizon a difference is far
# more likely to be a bad scrape than a revision, and a silent rewrite
# of ten years of history is not something a nightly job should be able
# to do.
REFRESH_WINDOW_DAYS = 7

REQUIRED_COLUMNS = [
    "catalog_item_id",
    "source",
    "grade",
    "price",
    "currency_code",
    "observed_at",
    "source_url",
    "raw_payload",
    "created_by",
]
# Added by coin_keeper migration 0002. Only ever written as false here --
# this step does no suspect-marking at all (the market is thin, spikes
# are real trades), so its absence costs nothing.
OPTIONAL_COLUMNS = ["is_suspect"]

TMP_TABLE = "tmp_ua_coins_price_snapshots"
COPY_COLUMNS = [
    "catalog_item_id",
    "source",
    "grade",
    "price",
    "currency_code",
    "observed_at",
    "source_url",
    "raw_payload",
]
COPY_TYPES = ["int4", "text", "text", "numeric", "text", "timestamptz", "text", "jsonb"]


# ---------------------------------------------------------------------- #
# staging -> which coin is which (pure)
# ---------------------------------------------------------------------- #


@dataclass
class CardRef:
    ua_coins_id: int
    source_id: str  # "nbu:88"
    page_url: str
    series_slug: str


def _cards_json_paths(staging_root: Path, series_slug: str | None) -> list[Path]:
    country_dir = staging_root / "ua"
    if series_slug is not None:
        return [country_dir / series_slug / "parsed" / "cards.json"]
    # "_ua_coins" is the shared cache dir, not a series.
    return sorted(
        p
        for p in country_dir.glob("*/parsed/cards.json")
        if not p.parent.parent.name.startswith("_")
    )


def build_card_index(
    staging_root: Path, series_slug: str | None = None
) -> tuple[dict[int, CardRef], list[str]]:
    """{ua_coins_id: CardRef} across the staged series, plus warnings.

    The price cache is shared between series, so the ua-coins id is the
    only key that links a price file back to an NBU card. Two series
    claiming the same ua-coins id for DIFFERENT cards would make that
    link ambiguous, so such an id is dropped with a warning rather than
    resolved by guessing which series is right.
    """
    index: dict[int, CardRef] = {}
    warnings: list[str] = []
    contested: set[int] = set()

    for path in _cards_json_paths(staging_root, series_slug):
        if not path.exists():
            warnings.append(f"no parsed cards at {path} -- run --step parse/match for that series")
            continue
        slug = path.parent.parent.name
        data = json.loads(path.read_text(encoding="utf-8"))
        for card in data.get("cards", []):
            ua = card.get("ua_coins")
            if not ua:
                continue
            coin_id = int(ua["id"])
            ref = CardRef(
                ua_coins_id=coin_id,
                source_id=card["source_id"],
                page_url=ua["url"],
                series_slug=slug,
            )
            existing = index.get(coin_id)
            if existing is not None and existing.source_id != ref.source_id:
                contested.add(coin_id)
                warnings.append(
                    f"ua-coins {coin_id} is claimed by {existing.source_id} "
                    f"({existing.series_slug}) and {ref.source_id} ({slug}) -- skipped"
                )
                continue
            if existing is None:
                index[coin_id] = ref

    for coin_id in contested:
        index.pop(coin_id, None)
    return index, warnings


# ---------------------------------------------------------------------- #
# rows to COPY (pure)
# ---------------------------------------------------------------------- #


def build_copy_rows(
    catalog_item_id: int, points: list[PricePoint], source_url: str
) -> list[tuple]:
    """One tuple per price point, in COPY_COLUMNS order.

    observed_at is the point's date at midnight UTC: ua-coins quotes one
    price per calendar day, and the unique constraint keys on the exact
    timestamp, so pinning it to a fixed instant is what makes a rerun a
    no-op instead of a second row a few hours along.

    source_url is the COIN PAGE, never the signed chart URL -- that one
    expires in about two days and would be a dead link in the history
    within the week.
    """
    return [
        (
            catalog_item_id,
            SOURCE,
            GRADE,
            p.price,
            CURRENCY,
            datetime.combine(p.day, time.min, tzinfo=timezone.utc),
            source_url,
            p.raw,
        )
        for p in points
    ]


# ---------------------------------------------------------------------- #
# reporting
# ---------------------------------------------------------------------- #


@dataclass
class PriceCoinReport:
    ua_coins_id: int
    source_id: str
    status: str  # "loaded" | "skipped:not_in_db" | "skipped:no_prices_file" | "anomaly"
    points: int = 0
    date_min: str | None = None
    date_max: str | None = None
    inserted: int = 0
    updated: int = 0
    duplicates: int = 0
    ucoin_present: int = 0
    ucoin_dropped: int = 0
    note: str | None = None


@dataclass
class LoadPricesSummary:
    series: str | None = None
    drop_ucoin: bool = False
    coins: list[PriceCoinReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rows_before: int | None = None
    rows_after: int | None = None
    committed: bool = False
    error: str | None = None

    def print_report(self) -> None:
        scope = self.series or "all staged series"
        print(f"[load-prices] {TABLE} in coin_keeper -- {scope}")
        if self.error:
            print(f"[load-prices]   ERROR: {self.error}")
            print(
                "[load-prices]   transaction rolled back, nothing written"
                if self.rows_before is not None
                else "[load-prices]   nothing written"
            )
            return

        if self.coins:
            print(
                f"[load-prices]   {'source_id':<10} {'ua_coins':<9} {'status':<24} "
                f"{'points':>7} {'date range':<25} {'ins':>7} {'upd':>7} {'dup':>7} "
                f"{'uCoin':>7}"
            )
        for c in self.coins:
            span = f"{c.date_min} .. {c.date_max}" if c.date_min else ""
            if c.ucoin_dropped:
                ucoin = f"-{c.ucoin_dropped}"
            elif c.ucoin_present:
                ucoin = f"{c.ucoin_present} left"
            else:
                ucoin = ""
            print(
                f"[load-prices]   {c.source_id:<10} {c.ua_coins_id:<9} {c.status:<24} "
                f"{c.points:>7} {span:<25} {c.inserted:>7} {c.updated:>7} "
                f"{c.duplicates:>7} {ucoin:>7}"
            )
            if c.note:
                print(f"[load-prices]     {c.note}")

        for w in self.warnings:
            print(f"[load-prices]   WARNING: {w}")

        loaded = sum(1 for c in self.coins if c.status == "loaded")
        not_in_db = sum(1 for c in self.coins if c.status == "skipped:not_in_db")
        no_file = sum(1 for c in self.coins if c.status == "skipped:no_prices_file")
        anomalies = sum(1 for c in self.coins if c.status == "anomaly")
        inserted = sum(c.inserted for c in self.coins)
        updated = sum(c.updated for c in self.coins)
        duplicates = sum(c.duplicates for c in self.coins)
        print(
            f"[load-prices] coins: loaded {loaded}, skipped:not_in_db {not_in_db}, "
            f"skipped:no_prices_file {no_file}, anomalies {anomalies}"
        )
        print(
            f"[load-prices] points: inserted {inserted}, corrected {updated}, "
            f"already present {duplicates}"
        )

        ucoin_dropped = sum(c.ucoin_dropped for c in self.coins)
        ucoin_present = sum(c.ucoin_present for c in self.coins)
        if self.drop_ucoin:
            print(
                f"[load-prices] legacy uCoin snapshots deleted: {ucoin_dropped} "
                "(only on coins that just got a ua-coins history)"
            )
        elif ucoin_present:
            print(
                f"[load-prices] legacy uCoin snapshots still on these coins: {ucoin_present} "
                "-- they are dated later than some ua-coins histories, so the storefront "
                "may show one as the price. Rerun with --drop-ucoin-prices to remove them."
            )
        if self.rows_before is not None:
            untouched = (
                "(hand-entered snapshots untouched)"
                if self.drop_ucoin
                else "(uCoin and hand-entered snapshots untouched)"
            )
            print(
                f"[load-prices] {TABLE} row count: {self.rows_before} -> {self.rows_after} "
                f"{untouched}"
            )
        if self.committed:
            print(
                "[load-prices] committed. Rerun with no new fetch -- it should report "
                "inserted 0, corrected 0, already present "
                f"{inserted + updated + duplicates}."
            )


# ---------------------------------------------------------------------- #
# SQL
# ---------------------------------------------------------------------- #


def _resolve_catalog_items(conn: psycopg.Connection, source_keys: list[str]) -> dict[str, int]:
    """{source_key: catalog_items.id} for the SHARED catalog only.

    created_by IS NULL is not optional: source_key is unique per user, so
    the same "nbu:88" can legitimately exist again as some collector's
    private copy. Central price snapshots (created_by NULL) belong to the
    shared item, and without this filter one private copy would make the
    lookup ambiguous.
    """
    if not source_keys:
        return {}
    rows = conn.execute(
        "SELECT source_key, id FROM catalog_items "
        "WHERE source_key = ANY(%(keys)s) AND created_by IS NULL",
        {"keys": source_keys},
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def _count_ucoin(conn: psycopg.Connection, item_ids: list[int]) -> dict[int, int]:
    """{catalog_item_id: how many shared uCoin snapshots it still has}."""
    if not item_ids:
        return {}
    rows = conn.execute(
        f"SELECT catalog_item_id, COUNT(*) FROM {TABLE} "
        "WHERE catalog_item_id = ANY(%(ids)s) AND source = %(source)s AND created_by IS NULL "
        "GROUP BY catalog_item_id",
        {"ids": item_ids, "source": UCOIN_SOURCE},
    ).fetchall()
    return {item_id: count for item_id, count in rows}


def _drop_ucoin(conn: psycopg.Connection, item_ids: list[int]) -> dict[int, int]:
    """Delete the legacy uCoin history of these coins; returns what went.

    `item_ids` is only ever the coins whose ua-coins history this very run
    loaded. That is the whole safety argument: a coin cannot be left
    without a price by this, because it just got one. A coin with no
    ua-coins match never reaches here, so its uCoin rows -- the only
    prices it has -- stay untouched.

    created_by IS NULL keeps it to the central importer's rows. A
    collector's own uCoin snapshot is their record, not legacy of ours.
    """
    if not item_ids:
        return {}
    rows = conn.execute(
        f"DELETE FROM {TABLE} "
        "WHERE catalog_item_id = ANY(%(ids)s) AND source = %(source)s AND created_by IS NULL "
        "RETURNING catalog_item_id",
        {"ids": item_ids, "source": UCOIN_SOURCE},
    ).fetchall()
    dropped: dict[int, int] = {}
    for (item_id,) in rows:
        dropped[item_id] = dropped.get(item_id, 0) + 1
    return dropped


def create_tmp_table(conn: psycopg.Connection) -> None:
    conn.execute(
        f"""
        CREATE TEMP TABLE {TMP_TABLE} (
            catalog_item_id integer NOT NULL,
            source          text    NOT NULL,
            grade           text,
            price           numeric(14,2) NOT NULL,
            currency_code   text    NOT NULL,
            observed_at     timestamptz NOT NULL,
            source_url      text,
            raw_payload     jsonb
        ) ON COMMIT DROP
        """
    )


def copy_rows(conn: psycopg.Connection, rows: list[tuple]) -> None:
    col_clause = ", ".join(COPY_COLUMNS)
    with conn.cursor() as cur:
        with cur.copy(f"COPY {TMP_TABLE} ({col_clause}) FROM STDIN") as copy:
            copy.set_types(COPY_TYPES)
            for row in rows:
                # build_copy_rows keeps raw_payload a plain dict so it can
                # be asserted on in tests; jsonb needs the wrapper only here.
                *head, raw_payload = row
                copy.write_row((*head, Jsonb(raw_payload)))


def refresh_window_start(today: date | None = None) -> datetime:
    """Midnight UTC of the oldest day still open to correction.

    REFRESH_WINDOW_DAYS counts calendar days including today, so a window
    of 7 run on the 10th reaches back to the 4th.
    """
    day = today or datetime.now(timezone.utc).date()
    return datetime.combine(
        day - timedelta(days=REFRESH_WINDOW_DAYS - 1), time.min, tzinfo=timezone.utc
    )


def refresh_recent(
    conn: psycopg.Connection, window_start: datetime
) -> dict[int, int]:
    """Correct the rows inside the window whose price the source has since
    changed; returns {catalog_item_id: rows updated}.

    Only our own rows move: created_by IS NULL keeps a collector's
    hand-entered snapshot theirs, the same boundary _drop_ucoin draws.
    The row keeps its id and observed_at, so nothing downstream that
    points at a snapshot loses its target -- only the number it carries
    and the payload it came from are rewritten.

    Matching is on the same four columns the insert de-duplicates on, so
    a row this updates is exactly a row the insert would have skipped.
    """
    rows = conn.execute(
        f"""
        UPDATE {TABLE} m
        SET price = t.price,
            source_url = t.source_url,
            raw_payload = t.raw_payload
        FROM (
            SELECT DISTINCT ON (catalog_item_id, source, grade, observed_at) *
            FROM {TMP_TABLE}
            ORDER BY catalog_item_id, source, grade, observed_at
        ) t
        WHERE m.catalog_item_id = t.catalog_item_id
          AND m.source = t.source
          AND m.grade IS NOT DISTINCT FROM t.grade
          AND m.observed_at = t.observed_at
          AND m.observed_at >= %(window_start)s
          AND m.created_by IS NULL
          AND m.price IS DISTINCT FROM t.price
        RETURNING m.catalog_item_id
        """,
        {"window_start": window_start},
    ).fetchall()
    updated: dict[int, int] = {}
    for (item_id,) in rows:
        updated[item_id] = updated.get(item_id, 0) + 1
    return updated


def insert_from_tmp(conn: psycopg.Connection, has_is_suspect: bool) -> dict[int, int]:
    """Move the staged rows into the real table; returns
    {catalog_item_id: rows actually inserted}.

    The NOT EXISTS is what makes a rerun a no-op, and it is not
    belt-and-braces around the unique constraint -- it is the only thing
    doing the job. uq_market_price_snapshots_item_source_grade_observed
    includes `grade`, every row we write has grade NULL, and Postgres'
    default NULLS DISTINCT means two such rows never collide: ON CONFLICT
    alone would happily insert the entire history a second time. Changing
    the constraint is coin_keeper's call, not this repo's, so the
    de-duplication is done here instead, with IS NOT DISTINCT FROM giving
    NULL the "equal to NULL" meaning the constraint does not.

    The ON CONFLICT clause stays as the backstop for the case the
    constraint DOES cover (a non-NULL grade arriving from some future
    source), and DISTINCT ON collapses duplicates inside the batch
    itself, which NOT EXISTS cannot see.

    A skipped row is not necessarily an unchanged one: the price is not
    part of the key. refresh_recent() is what revisits the recent ones,
    and it must run against the same staged batch.
    """
    extra_cols = ", is_suspect" if has_is_suspect else ""
    extra_vals = ", false" if has_is_suspect else ""
    rows = conn.execute(
        f"""
        INSERT INTO {TABLE} (
            catalog_item_id, source, grade, price, currency_code,
            observed_at, source_url, raw_payload, created_by{extra_cols}
        )
        SELECT t.catalog_item_id, t.source, t.grade, t.price, t.currency_code,
               t.observed_at, t.source_url, t.raw_payload, NULL{extra_vals}
        FROM (
            SELECT DISTINCT ON (catalog_item_id, source, grade, observed_at) *
            FROM {TMP_TABLE}
            ORDER BY catalog_item_id, source, grade, observed_at
        ) t
        WHERE NOT EXISTS (
            SELECT 1 FROM {TABLE} m
            WHERE m.catalog_item_id = t.catalog_item_id
              AND m.source = t.source
              AND m.grade IS NOT DISTINCT FROM t.grade
              AND m.observed_at = t.observed_at
        )
        ON CONFLICT ON CONSTRAINT uq_market_price_snapshots_item_source_grade_observed
        DO NOTHING
        RETURNING catalog_item_id
        """
    ).fetchall()

    inserted: dict[int, int] = {}
    for (item_id,) in rows:
        inserted[item_id] = inserted.get(item_id, 0) + 1
    return inserted


# ---------------------------------------------------------------------- #
# entry point
# ---------------------------------------------------------------------- #


def _collect(
    staging_root: Path, series_slug: str | None, summary: LoadPricesSummary
) -> tuple[list[tuple[CardRef, list[PricePoint], PriceCoinReport]], list[PriceCoinReport]]:
    """Read and validate everything on disk before opening a connection.
    A bad file should never leave a half-open transaction behind."""
    index, warnings = build_card_index(staging_root, series_slug)
    summary.warnings.extend(warnings)
    print(f"[load-prices] {len(index)} matched card(s) in staging")

    ready: list[tuple[CardRef, list[PricePoint], PriceCoinReport]] = []
    early: list[PriceCoinReport] = []

    for coin_id in sorted(index):
        ref = index[coin_id]
        report = PriceCoinReport(
            ua_coins_id=coin_id, source_id=ref.source_id, status="skipped:no_prices_file"
        )
        path = prices_path(staging_root, ref.source_id)
        if not path.exists():
            report.note = f"no {path.name} -- run --step fetch-prices"
            early.append(report)
            print(f"[load-prices]   {ref.source_id} (ua-coins {coin_id}): no price file, skipped")
            continue

        try:
            points = load_price_file(path)
        except PriceSeriesAnomaly as exc:
            report.status = "anomaly"
            report.note = str(exc)
            early.append(report)
            print(f"[load-prices]   {ref.source_id} (ua-coins {coin_id}): ANOMALY -- {exc}")
            continue

        report.points = len(points)
        if points:
            report.date_min = points[0].day.isoformat()
            report.date_max = points[-1].day.isoformat()
        print(
            f"[load-prices]   {ref.source_id} (ua-coins {coin_id}): {len(points)} point(s) "
            f"{report.date_min}..{report.date_max}"
        )
        ready.append((ref, points, report))

    return ready, early


def _run_transaction(
    conn: psycopg.Connection,
    ready: list[tuple[CardRef, list[PricePoint], PriceCoinReport]],
    summary: LoadPricesSummary,
) -> None:
    cols = existing_columns(conn, TABLE)
    missing = [c for c in REQUIRED_COLUMNS if c not in cols]
    if missing:
        raise RuntimeError(f"{TABLE} is missing required column(s): {missing}")
    has_is_suspect = "is_suspect" in cols
    if not has_is_suspect:
        summary.warnings.append("column is_suspect missing -- run migration 0002 in coin_keeper")

    if conn.execute(
        "SELECT 1 FROM currencies WHERE code = %(code)s", {"code": CURRENCY}
    ).fetchone() is None:
        raise RuntimeError(
            f"currency {CURRENCY!r} is not in the currencies table -- "
            f"{TABLE}.currency_code references it, so nothing can be written"
        )

    summary.rows_before = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]

    resolved = _resolve_catalog_items(conn, [ref.source_id for ref, _, _ in ready])
    print(f"[load-prices] {len(resolved)}/{len(ready)} coin(s) found in catalog_items")

    loadable: list[tuple[PriceCoinReport, int]] = []
    all_rows: list[tuple] = []
    for ref, points, report in ready:
        item_id = resolved.get(ref.source_id)
        if item_id is None:
            report.status = "skipped:not_in_db"
            report.note = (
                f"no catalog_items row with source_key={ref.source_id!r} "
                "in the shared catalog -- load the coin first"
            )
            summary.coins.append(report)
            continue
        report.status = "loaded"
        loadable.append((report, item_id))
        all_rows.extend(build_copy_rows(item_id, points, ref.page_url))
        summary.coins.append(report)

    if not all_rows:
        summary.rows_after = summary.rows_before
        print("[load-prices] nothing to insert")
        return

    print(f"[load-prices] COPY {len(all_rows)} row(s) into {TMP_TABLE}...")
    create_tmp_table(conn)
    copy_rows(conn, all_rows)
    inserted_by_item = insert_from_tmp(conn, has_is_suspect)
    print(f"[load-prices] inserted {sum(inserted_by_item.values())} new row(s)")

    window_start = refresh_window_start()
    updated_by_item = refresh_recent(conn, window_start)
    print(
        f"[load-prices] corrected {sum(updated_by_item.values())} row(s) "
        f"inside the {REFRESH_WINDOW_DAYS}-day window "
        f"(from {window_start.date()}) where ua-coins has changed its quote"
    )

    for report, item_id in loadable:
        report.inserted = inserted_by_item.get(item_id, 0)
        report.updated = updated_by_item.get(item_id, 0)
        report.duplicates = report.points - report.inserted - report.updated

    _handle_ucoin(conn, loadable, summary)

    summary.rows_after = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]


def _handle_ucoin(
    conn: psycopg.Connection,
    loadable: list[tuple[PriceCoinReport, int]],
    summary: LoadPricesSummary,
) -> None:
    """Count -- and, when asked, delete -- the legacy uCoin history of the
    coins this run just gave a ua-coins history to.

    Counting always happens, because a uCoin row that is left behind is
    not harmless: the storefront shows the LATEST snapshot whatever its
    source, and these rows are dated later than some ua-coins histories.
    So a run that does not drop them says how many are sitting there and
    what to type to remove them.
    """
    item_ids = [item_id for _, item_id in loadable]
    present = _count_ucoin(conn, item_ids)
    for report, item_id in loadable:
        report.ucoin_present = present.get(item_id, 0)

    if not summary.drop_ucoin:
        return

    dropped = _drop_ucoin(conn, item_ids)
    for report, item_id in loadable:
        report.ucoin_dropped = dropped.get(item_id, 0)


def load_prices(
    series_slug: str | None = None,
    dsn: str | None = None,
    staging_root: Path = DEFAULT_STAGING_ROOT,
    drop_ucoin: bool = False,
) -> LoadPricesSummary:
    summary = LoadPricesSummary(series=series_slug, drop_ucoin=drop_ucoin)

    ready, early = _collect(staging_root, series_slug, summary)

    try:
        dsn = dsn or get_database_url()
    except RuntimeError as exc:
        summary.coins.extend(early)
        summary.error = str(exc)
        summary.print_report()
        return summary

    try:
        with psycopg.connect(dsn) as conn:
            with conn.transaction():
                _run_transaction(conn, ready, summary)
            # conn.transaction() exited without raising -> committed.
    except Exception as exc:
        summary.coins.extend(early)
        summary.error = f"{type(exc).__name__}: {exc}"
        summary.print_report()
        return summary

    summary.coins.extend(early)
    summary.coins.sort(key=lambda c: c.ua_coins_id)
    summary.committed = True
    summary.print_report()
    return summary
