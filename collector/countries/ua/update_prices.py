"""One nightly ua-coins quote for every NBU coin already in the
production catalog. The first step in this repo built to run with nobody
watching (deploy/crontab), which changes several things about it.

What it does, once per night:

    1. scope comes from the DATABASE, not from staging -- every shared,
       active catalog_items row whose source_key is "nbu:*". The server
       has no staging tree worth trusting and cards.json says what one
       collection run happened to see, not what the catalog holds.
    2. the years those coins were issued in (plus/minus one, the same
       year_shift the matcher allows) are downloaded from ua-coins as
       whole catalog pages -- ONE request per year, not per coin. The
       yearly table already carries each coin's current price and the
       date it was taken.
    3. each coin finds its own row by ua-coins id, taken from the
       UA-Coins price_source_links row that --step match wrote. Nothing
       is re-matched here: matching is a decision, and decisions are not
       re-taken unattended at 03:15.
    4. the quotes go into market_price_snapshots through the very same
       batch that load-prices uses, so the two steps write identical
       rows and cannot fight each other. Reruns collapse on the
       NOT EXISTS in insert_from_tmp.

Nothing is cached between runs: the whole point is today's number, so a
cached table would be the one thing this step must never read. The
downloaded HTML is still kept, under
staging/ua/_ua_coins/daily/<run date>/<year>.html, for working out
afterwards what the site actually said on the night something looked
wrong -- and pruned after DAILY_KEEP_DAYS by this step itself, because
nobody is going to remember to do it.

Output is written for a log file and a grep, not for a person at a
terminal: a human-readable report, then exactly one summary line, and an
exit code that says what happened (0 fine / 1 partial / 2 nothing done).
There are no prompts and no "rerun with --flag" hints -- there is nobody
there to read them.
"""

from __future__ import annotations

import shutil
import time as time_module
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import psycopg

from collector.core.db import existing_columns, get_database_url
from collector.core.pacing import Pacer
from collector.core.staging import DEFAULT_STAGING_ROOT
from collector.countries.ua import ua_coins
from collector.countries.ua.load_prices import (
    REQUIRED_COLUMNS,
    TABLE,
    build_copy_rows,
    copy_rows,
    create_tmp_table,
    insert_from_tmp,
)
from collector.countries.ua.nbu_client import USER_AGENT
from collector.countries.ua.prices import MAX_PRICE, PricePoint

LINKS_TABLE = "price_source_links"
LINK_SOURCE_UA_COINS = "UA-Coins"
ITEMS_TABLE = "catalog_items"

# Kept for incident forensics, not as a cache -- see the module
# docstring. Two weeks is long enough to look into "why did the price
# jump last Tuesday" and short enough that a year-wide catalog's pages
# do not quietly fill the server's disk.
DAILY_KEEP_DAYS = 14

# A year page that fails for a reason 429-retrying does not cover (a
# timeout, a 5xx, a dropped connection) is retried this many times
# before the year is given up on. Deliberately small: the run is nightly,
# and a site that is down stays down for longer than a cron slot.
MAX_YEAR_ATTEMPTS = 3
YEAR_RETRY_SECONDS = 20.0

# ...and after this many years fail in a row, the rest are not attempted
# at all. Without a stop like this an unattended run has no bound: a full
# catalogue is ~30 years, each costing minutes of timeouts, and a cron
# job still grinding through a dead site at 07:00 helps nobody. The
# skipped years are reported as not downloaded, same as any other loss.
ABORT_AFTER_CONSECUTIVE_FAILURES = 5

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_NOTHING_DONE = 2


# ---------------------------------------------------------------------- #
# scope (database)
# ---------------------------------------------------------------------- #


@dataclass
class ScopeCoin:
    item_id: int
    source_key: str  # "nbu:88"
    issue_year: int | None
    page_url: str | None = None  # price_source_links UA-Coins external_id
    ua_coins_id: int | None = None


def fetch_scope(conn: psycopg.Connection) -> list[ScopeCoin]:
    """Every shared, active NBU coin in the production catalog.

    created_by IS NULL is not optional and is the same rule load_prices
    follows: source_key is unique per USER, so "nbu:88" legitimately
    exists again as some collector's private copy, and the central
    snapshots belong to the shared row only.
    """
    rows = conn.execute(
        f"""
        SELECT id, source_key, issue_year
        FROM {ITEMS_TABLE}
        WHERE source_key LIKE 'nbu:%%'
          AND created_by IS NULL
          AND status = 'active'
        ORDER BY id
        """
    ).fetchall()
    return [ScopeCoin(item_id=r[0], source_key=r[1], issue_year=r[2]) for r in rows]


def attach_links(conn: psycopg.Connection, scope: list[ScopeCoin]) -> None:
    """Fill in page_url/ua_coins_id from the UA-Coins price_source_links
    row --step match left behind. A coin without one keeps both as None
    and is counted as no_link: it was never matched to ua-coins, which is
    a fact about that coin, not a failure of tonight's run.
    """
    if not scope:
        return
    ids = [c.item_id for c in scope]
    rows = conn.execute(
        f"SELECT catalog_item_id, external_id FROM {LINKS_TABLE} "
        "WHERE source = %(source)s AND catalog_item_id = ANY(%(ids)s)",
        {"source": LINK_SOURCE_UA_COINS, "ids": ids},
    ).fetchall()
    by_item = {item_id: external_id for item_id, external_id in rows}
    for coin in scope:
        url = by_item.get(coin.item_id)
        if not url:
            continue
        coin.page_url = url
        coin.ua_coins_id = ua_coins.row_id_from_href(url)


def years_needed(scope: list[ScopeCoin]) -> list[int]:
    """Distinct issue years of the scope, each widened by one in both
    directions.

    The widening is the same year_shift the matcher allows: NBU's
    circulation year and the year ua-coins files a coin under differ by
    one often enough that a coin's row routinely sits in the neighbouring
    year's table. Cheap insurance -- a year is one request, and the
    widened set overlaps itself heavily.
    """
    years: set[int] = set()
    for coin in scope:
        if coin.issue_year is None:
            continue
        years.update((coin.issue_year - 1, coin.issue_year, coin.issue_year + 1))
    return sorted(years)


# ---------------------------------------------------------------------- #
# the night's tables (network)
# ---------------------------------------------------------------------- #


def daily_dir(staging_root: Path, day: date) -> Path:
    return staging_root / "ua" / "_ua_coins" / "daily" / day.isoformat()


def prune_daily(staging_root: Path, today: date, keep_days: int = DAILY_KEEP_DAYS) -> list[str]:
    """Delete kept HTML older than keep_days; returns what went.

    A directory whose name is not a date is left alone -- this deletes
    only what it recognises as its own.
    """
    root = staging_root / "ua" / "_ua_coins" / "daily"
    if not root.is_dir():
        return []
    cutoff = today - timedelta(days=keep_days)
    removed: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        try:
            day = date.fromisoformat(child.name)
        except ValueError:
            continue
        if day < cutoff:
            shutil.rmtree(child)
            removed.append(child.name)
    return removed


def download_year(
    client: httpx.Client, year: int, day_dir: Path, pacer: Pacer
) -> ua_coins.YearQuotes:
    """One year's table, fresh off the site and saved to day_dir.

    A 404 is a year ua-coins has no page for (the +1 of a current-year
    coin, most nights) and comes back as an empty page with no rows --
    the same reading fetch_year takes, and not a failure.
    """
    last_exc: Exception | None = None
    html: str | None = None
    for attempt in range(1, MAX_YEAR_ATTEMPTS + 1):
        try:
            resp = ua_coins.get_with_retry(
                client, ua_coins.CATALOG_PATH.format(year=year), pacer, log_label=str(year)
            )
            html = resp.text
            break
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                html = ""
                break
            last_exc = exc
            if exc.response.status_code == 429:
                # get_with_retry already backed off five times for this.
                # Asking again inside the same run is how a nightly job
                # turns into a morning one -- give the year up now.
                break
        except httpx.HTTPError as exc:
            last_exc = exc
        print(
            f"[update-prices]   year {year}: {type(last_exc).__name__}: {last_exc} "
            f"(attempt {attempt}/{MAX_YEAR_ATTEMPTS})"
        )
        if attempt < MAX_YEAR_ATTEMPTS:
            time_module.sleep(YEAR_RETRY_SECONDS)

    if html is None:
        raise RuntimeError(f"year {year} not downloaded: {type(last_exc).__name__}: {last_exc}")

    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / f"{year}.html").write_text(html, encoding="utf-8")
    return ua_coins.parse_year_quotes(html, year)


# ---------------------------------------------------------------------- #
# quote -> row (pure)
# ---------------------------------------------------------------------- #


@dataclass
class CoinQuoteReport:
    source_key: str
    item_id: int
    ua_coins_id: int | None = None
    # "quoted" | "no_link" | "no_quote:not_listed" | "no_quote:no_data"
    #          | "no_quote:unusable"
    status: str = "no_link"
    price: Decimal | None = None
    as_of: date | None = None
    table_year: int | None = None
    inserted: int = 0
    duplicates: int = 0
    note: str | None = None


def pick_quote(
    coin: ScopeCoin, tables: dict[int, ua_coins.YearQuotes]
) -> tuple[ua_coins.UaCoinsQuote, ua_coins.YearQuotes] | None:
    """The coin's row and the table it came from, or None if no table
    downloaded tonight lists it.

    A ua-coins id belongs to exactly one year's table, so the search is
    normally over in one step. When more than one table carries it (the
    site refiling a coin mid-run, say) the coin's own issue year wins and
    the earliest year is the tie-break -- an arbitrary rule, but a fixed
    one: two runs must not disagree about which row a coin has.
    """
    if coin.ua_coins_id is None:
        return None
    hits = [
        (table, table.quotes[coin.ua_coins_id])
        for _, table in sorted(tables.items())
        if coin.ua_coins_id in table.quotes
    ]
    if not hits:
        return None
    for table, quote in hits:
        if table.year == coin.issue_year:
            return quote, table
    table, quote = hits[0]
    return quote, table


def build_rows(
    scope: list[ScopeCoin], tables: dict[int, ua_coins.YearQuotes]
) -> tuple[list[tuple], list[CoinQuoteReport]]:
    """The COPY batch for tonight, plus one report line per scope coin.

    Every row goes through load_prices.build_copy_rows on a one-point
    PricePoint, which is what makes an update-prices row byte-identical
    to the load-prices row for the same coin and day -- same source, same
    ungraded NULL, same midnight-UTC observed_at, same coin-page URL. The
    two steps therefore share a history rather than each keeping their
    own, and a coin loaded by hand today and updated by cron tomorrow
    just gains a point.
    """
    rows: list[tuple] = []
    reports: list[CoinQuoteReport] = []

    for coin in scope:
        report = CoinQuoteReport(
            source_key=coin.source_key, item_id=coin.item_id, ua_coins_id=coin.ua_coins_id
        )
        if coin.page_url is None:
            report.status = "no_link"
            report.note = "no UA-Coins price_source_links row -- never matched"
            reports.append(report)
            continue
        if coin.ua_coins_id is None:
            report.status = "no_link"
            report.note = f"UA-Coins link {coin.page_url!r} has no coin id in its slug"
            reports.append(report)
            continue

        found = pick_quote(coin, tables)
        if found is None:
            report.status = "no_quote:not_listed"
            report.note = (
                f"ua-coins {coin.ua_coins_id} is in none of tonight's tables "
                f"(issue_year {coin.issue_year})"
            )
            reports.append(report)
            continue

        quote, table = found
        report.table_year = table.year
        report.as_of = table.as_of
        if quote.price is None:
            report.status = "no_quote:no_data"
            report.note = quote.raw_text or "empty price cell"
            reports.append(report)
            continue
        if quote.price >= MAX_PRICE:
            # numeric(14,2) would overflow and abort the whole insert.
            report.status = "no_quote:unusable"
            report.note = f"price {quote.price} does not fit numeric(14,2)"
            reports.append(report)
            continue

        report.status = "quoted"
        report.price = quote.price
        point = PricePoint(
            day=table.as_of,
            price=quote.price,
            raw={
                "date": table.as_of.isoformat(),
                "price": float(quote.price),
                "table_year": table.year,
            },
        )
        rows.extend(build_copy_rows(coin.item_id, [point], coin.page_url))
        reports.append(report)

    return rows, reports


# ---------------------------------------------------------------------- #
# reporting -- a log file and a grep, not a person
# ---------------------------------------------------------------------- #


@dataclass
class UpdatePricesSummary:
    run_date: date | None = None
    scope: int = 0
    years_ok: list[int] = field(default_factory=list)
    years_failed: list[int] = field(default_factory=list)
    coins: list[CoinQuoteReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    committed: bool = False
    error: str | None = None

    @property
    def matched(self) -> int:
        return sum(1 for c in self.coins if c.status == "quoted")

    @property
    def inserted(self) -> int:
        return sum(c.inserted for c in self.coins)

    @property
    def duplicates(self) -> int:
        return sum(c.duplicates for c in self.coins)

    @property
    def no_quote(self) -> int:
        return sum(1 for c in self.coins if c.status.startswith("no_quote"))

    @property
    def no_link(self) -> int:
        return sum(1 for c in self.coins if c.status == "no_link")

    @property
    def errors(self) -> int:
        """What the summary line reports as errors: years lost tonight,
        or 1 for a run that never got started at all."""
        return len(self.years_failed) if self.error is None else 1

    @property
    def exit_code(self) -> int:
        """0 fine, 1 partial, 2 nothing done.

        inserted == 0 is NOT a failure: ua-coins does not requote every
        coin every day, and a rerun on the same table is all duplicates
        by design. What separates 1 from 2 is whether anything was
        achieved -- some years lost is partial, no database or no site at
        all is nothing done.
        """
        if self.error is not None:
            return EXIT_NOTHING_DONE
        if self.years_failed and not self.years_ok:
            return EXIT_NOTHING_DONE
        if self.years_failed:
            return EXIT_PARTIAL
        return EXIT_OK

    def status_word(self) -> str:
        return {EXIT_OK: "ok", EXIT_PARTIAL: "partial", EXIT_NOTHING_DONE: "failed"}[
            self.exit_code
        ]

    def summary_line(self) -> str:
        """The one line a monitor greps. Same keys in the same order on
        every outcome, so a parser never has to branch on the status
        word. scope = no_link + no_quote + matched, and matched =
        inserted + dup."""
        return (
            f"update-prices {self.status_word()} "
            f"scope={self.scope} years={len(self.years_ok)} matched={self.matched} "
            f"inserted={self.inserted} dup={self.duplicates} "
            f"no_quote={self.no_quote} no_link={self.no_link} errors={self.errors}"
        )

    def print_report(self) -> None:
        stamp = self.run_date.isoformat() if self.run_date else "?"
        print(f"[update-prices] run {stamp} -- {TABLE} in coin_keeper")
        if self.error:
            print(f"[update-prices]   ERROR: {self.error}")

        if self.years_ok:
            print(
                f"[update-prices]   years downloaded ({len(self.years_ok)}): "
                + ", ".join(str(y) for y in self.years_ok)
            )
        if self.years_failed:
            print(
                f"[update-prices]   years NOT downloaded ({len(self.years_failed)}): "
                + ", ".join(str(y) for y in self.years_failed)
            )

        if self.coins:
            print(
                f"[update-prices]   {'source_key':<12} {'ua_coins':<9} {'status':<20} "
                f"{'price':>12} {'as of':<12} {'ins':>4} {'dup':>4}"
            )
        for c in self.coins:
            price = f"{c.price}" if c.price is not None else ""
            as_of = c.as_of.isoformat() if c.as_of else ""
            ua = str(c.ua_coins_id) if c.ua_coins_id is not None else ""
            print(
                f"[update-prices]   {c.source_key:<12} {ua:<9} {c.status:<20} "
                f"{price:>12} {as_of:<12} {c.inserted:>4} {c.duplicates:>4}"
            )
            if c.note and not c.status == "quoted":
                print(f"[update-prices]     {c.note}")

        for w in self.warnings:
            print(f"[update-prices]   WARNING: {w}")
        if self.pruned:
            print(
                f"[update-prices]   pruned kept HTML older than {DAILY_KEEP_DAYS} days: "
                + ", ".join(self.pruned)
            )

        print(
            f"[update-prices] coins: {self.scope} in scope, {self.matched} quoted, "
            f"{self.no_quote} without a quote, {self.no_link} without a ua-coins link"
        )
        print(
            f"[update-prices] points: inserted {self.inserted}, "
            f"already present {self.duplicates}"
        )
        print(self.summary_line())


# ---------------------------------------------------------------------- #
# entry point
# ---------------------------------------------------------------------- #


def _insert(
    conn: psycopg.Connection, rows: list[tuple], reports: list[CoinQuoteReport]
) -> None:
    cols = existing_columns(conn, TABLE)
    missing = [c for c in REQUIRED_COLUMNS if c not in cols]
    if missing:
        raise RuntimeError(f"{TABLE} is missing required column(s): {missing}")

    create_tmp_table(conn)
    copy_rows(conn, rows)
    inserted_by_item = insert_from_tmp(conn, "is_suspect" in cols)
    for report in reports:
        if report.status != "quoted":
            continue
        report.inserted = inserted_by_item.get(report.item_id, 0)
        report.duplicates = 1 - report.inserted


def update_prices(
    dsn: str | None = None,
    staging_root: Path = DEFAULT_STAGING_ROOT,
    today: date | None = None,
) -> UpdatePricesSummary:
    """One night's pass. Never raises: every failure lands in the summary
    and comes back out as an exit code, because the caller is cron."""
    run_date = today or datetime.now(timezone.utc).date()
    summary = UpdatePricesSummary(run_date=run_date)

    try:
        dsn = dsn or get_database_url()
    except RuntimeError as exc:
        summary.error = str(exc)
        summary.print_report()
        return summary

    # Scope first, on its own connection: the download that follows takes
    # minutes of paced requests, and an idle session held open across it
    # is a session an ssh tunnel gets to drop.
    try:
        with psycopg.connect(dsn) as conn:
            scope = fetch_scope(conn)
            attach_links(conn, scope)
    except Exception as exc:
        summary.error = f"reading the catalog: {type(exc).__name__}: {exc}"
        summary.print_report()
        return summary

    summary.scope = len(scope)
    print(f"[update-prices] {len(scope)} shared active nbu:* coin(s) in the catalog")

    no_year = [c.source_key for c in scope if c.issue_year is None]
    if no_year:
        summary.warnings.append(
            f"{len(no_year)} coin(s) have no issue_year, so no year page was fetched for "
            f"them: {', '.join(no_year[:10])}"
        )

    years = years_needed(scope)
    day_dir = daily_dir(staging_root, run_date)
    tables: dict[int, ua_coins.YearQuotes] = {}

    if years:
        print(f"[update-prices] downloading {len(years)} year table(s) into {day_dir}")
        pacer = Pacer(ua_coins.REQUEST_DELAY_RANGE)
        with httpx.Client(
            base_url=ua_coins.BASE_URL, headers={"User-Agent": USER_AGENT}, timeout=30.0
        ) as client:
            consecutive_failures = 0
            for i, year in enumerate(years, 1):
                if consecutive_failures >= ABORT_AFTER_CONSECUTIVE_FAILURES:
                    summary.years_failed.append(year)
                    continue
                try:
                    table = download_year(client, year, day_dir, pacer)
                except (RuntimeError, ua_coins.YearTableAnomaly) as exc:
                    summary.years_failed.append(year)
                    summary.warnings.append(str(exc))
                    consecutive_failures += 1
                    print(f"[update-prices]   year {year} ({i}/{len(years)}): FAILED -- {exc}")
                    if consecutive_failures >= ABORT_AFTER_CONSECUTIVE_FAILURES:
                        summary.warnings.append(
                            f"{consecutive_failures} years failed in a row -- giving up on "
                            "the rest of tonight's years"
                        )
                        print(
                            f"[update-prices]   {consecutive_failures} years failed in a row, "
                            "abandoning the remaining years"
                        )
                    continue
                consecutive_failures = 0
                tables[year] = table
                summary.years_ok.append(year)
                as_of = table.as_of.isoformat() if table.as_of else "no table"
                print(
                    f"[update-prices]   year {year} ({i}/{len(years)}): "
                    f"{len(table.quotes)} row(s), as of {as_of}"
                )

    try:
        summary.pruned = prune_daily(staging_root, run_date)
    except OSError as exc:
        summary.warnings.append(f"pruning old daily HTML: {type(exc).__name__}: {exc}")

    rows, reports = build_rows(scope, tables)
    summary.coins = reports

    if rows:
        try:
            with psycopg.connect(dsn) as conn:
                with conn.transaction():
                    _insert(conn, rows, reports)
            summary.committed = True
        except Exception as exc:
            for report in reports:
                report.inserted = report.duplicates = 0
            summary.error = f"writing snapshots: {type(exc).__name__}: {exc}"
            summary.print_report()
            return summary

    summary.print_report()
    return summary
