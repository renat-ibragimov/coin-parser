"""Keeps coin_keeper's exchange_rates table current -- the write side of
docs/business-rules.md, BR-6, in that repository (the read side,
purchase_rate_uah and the historical-rate lookups, is already there and
untouched by this module).

Two runs of the exact same code (docs/00_spec.md, principle 1):

    - the nightly cron step: a short rolling window, so one missed run
      heals on the next without anyone noticing (see DEFAULT_WINDOW_DAYS);
    - the one-time backfill: an explicit --start at the earliest date NBU
      has, run once by hand after this step is deployed.

ON CONFLICT DO UPDATE always wins with the freshest NBU answer, including
over coin_keeper's migrated legacy rows -- those were themselves a onetime
cache of this same API, not an independent source, so there is no reason
to treat them as more trustworthy than asking again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx
import psycopg

from collector.core.db import get_database_url
from collector.rates.nbu_rates import CURRENCIES, USER_AGENT, RateRow, fetch_range

TABLE = "exchange_rates"
SOURCE = "NBU"

# Wide enough that a cron run skipped by a dead server (an outage, a bad
# deploy) is made whole by the next run, with nothing to notice or fix by
# hand. Cheap regardless of width: the request is a range, not one call
# per day in the window.
DEFAULT_WINDOW_DAYS = 14

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_NOTHING_DONE = 2


@dataclass
class CurrencyReport:
    currency_code: str
    status: str = "ok"  # "ok" | "error"
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    error: str | None = None


@dataclass
class UpdateRatesSummary:
    run_date: date | None = None
    start: date | None = None
    end: date | None = None
    currencies: list[CurrencyReport] = field(default_factory=list)
    # Set only for a run that never got started at all (no DATABASE_URL).
    error: str | None = None

    @property
    def inserted(self) -> int:
        return sum(c.inserted for c in self.currencies)

    @property
    def updated(self) -> int:
        return sum(c.updated for c in self.currencies)

    @property
    def unchanged(self) -> int:
        return sum(c.unchanged for c in self.currencies)

    @property
    def failed(self) -> list[str]:
        return [c.currency_code for c in self.currencies if c.status == "error"]

    @property
    def exit_code(self) -> int:
        """0 fine, 1 partial, 2 nothing done -- same three-way split as
        update-prices, meaning the same thing here: some currencies
        lost is partial, no database or every currency lost is nothing
        done. A window with zero new or changed rows is NOT a failure --
        most nights repeat a date the previous run already wrote."""
        if self.error is not None:
            return EXIT_NOTHING_DONE
        if self.currencies and len(self.failed) == len(self.currencies):
            return EXIT_NOTHING_DONE
        if self.failed:
            return EXIT_PARTIAL
        return EXIT_OK

    def status_word(self) -> str:
        return {EXIT_OK: "ok", EXIT_PARTIAL: "partial", EXIT_NOTHING_DONE: "failed"}[
            self.exit_code
        ]

    def summary_line(self) -> str:
        """One line a monitor greps, same keys in the same order on every
        outcome."""
        return (
            f"update-rates {self.status_word()} "
            f"currencies={len(self.currencies)} "
            f"inserted={self.inserted} updated={self.updated} unchanged={self.unchanged} "
            f"failed={len(self.failed)}"
        )

    def report_payload(self) -> dict:
        """The run as coin_keeper's admin section stores it."""
        return {
            "status": self.status_word(),
            "runDate": self.run_date.isoformat() if self.run_date else None,
            "summary": self.summary_line(),
            "stats": {
                "start": self.start.isoformat() if self.start else None,
                "end": self.end.isoformat() if self.end else None,
                "currencies": len(self.currencies),
                "inserted": self.inserted,
                "updated": self.updated,
                "unchanged": self.unchanged,
                "failed": len(self.failed),
            },
            "details": self.details_text(),
            "exitCode": self.exit_code,
        }

    def details_text(self) -> str | None:
        if self.exit_code == EXIT_OK:
            return None
        parts: list[str] = []
        if self.error:
            parts.append(self.error)
        for currency in self.currencies:
            if currency.status == "error":
                parts.append(f"{currency.currency_code}: {currency.error}")
        return "\n".join(parts) or None

    def print_report(self) -> None:
        stamp = self.run_date.isoformat() if self.run_date else "?"
        window = f"{self.start.isoformat()}..{self.end.isoformat()}" if self.start else "?"
        print(f"[update-rates] run {stamp} -- {TABLE} in coin_keeper, window {window}")
        if self.error:
            print(f"[update-rates]   ERROR: {self.error}")
        for currency in self.currencies:
            if currency.status == "error":
                print(f"[update-rates]   {currency.currency_code}: FAILED -- {currency.error}")
            else:
                print(
                    f"[update-rates]   {currency.currency_code}: fetched {currency.fetched}, "
                    f"inserted {currency.inserted}, updated {currency.updated}, "
                    f"unchanged {currency.unchanged}"
                )
        print(self.summary_line())


def _upsert(conn: psycopg.Connection, rows: list[RateRow]) -> tuple[int, int, int]:
    """One row at a time -- volume here is a currency's rate for a
    handful of days on a cron night, or at most some thousands on the
    one-time backfill, not the hundreds of thousands of points
    load_prices.py exists to batch through COPY.

    The WHERE on the DO UPDATE is what makes "unchanged" observable:
    when it is false, postgres neither inserts nor updates the
    conflicting row, and RETURNING yields no row at all for that
    input -- fetchone() comes back None. `xmax = 0` is postgres' own
    tell for "this row was just inserted" otherwise.
    """
    inserted = updated = unchanged = 0
    fetched_at = datetime.now(timezone.utc)
    for row in rows:
        result = conn.execute(
            f"""
            INSERT INTO {TABLE} (currency_code, rate_uah, effective_date, fetched_at, source)
            VALUES (%(currency_code)s, %(rate_uah)s, %(effective_date)s, %(fetched_at)s, %(source)s)
            ON CONFLICT (currency_code, effective_date, source) DO UPDATE
                SET rate_uah = EXCLUDED.rate_uah, fetched_at = EXCLUDED.fetched_at
                WHERE {TABLE}.rate_uah IS DISTINCT FROM EXCLUDED.rate_uah
            RETURNING (xmax = 0) AS inserted
            """,
            {
                "currency_code": row.currency_code,
                "rate_uah": row.rate_uah,
                "effective_date": row.effective_date,
                "fetched_at": fetched_at,
                "source": SOURCE,
            },
        ).fetchone()
        if result is None:
            unchanged += 1
        elif result[0]:
            inserted += 1
        else:
            updated += 1
    return inserted, updated, unchanged


def update_rates(
    dsn: str | None = None,
    start: date | None = None,
    end: date | None = None,
    today: date | None = None,
) -> UpdateRatesSummary:
    """One run: fetch every currency in CURRENCIES over [start, end] and
    upsert it. Never raises -- every failure lands in the summary and
    comes back out as an exit code, because the caller is cron.

    With no start/end: the last DEFAULT_WINDOW_DAYS days, for the nightly
    step. An explicit start (the earliest date NBU has) is the one-time
    backfill -- same code, wider window.
    """
    run_date = today or datetime.now(timezone.utc).date()
    end = end or run_date
    start = start or (end - timedelta(days=DEFAULT_WINDOW_DAYS))
    summary = UpdateRatesSummary(run_date=run_date, start=start, end=end)

    try:
        dsn = dsn or get_database_url()
    except RuntimeError as exc:
        summary.error = str(exc)
        summary.print_report()
        return summary

    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0) as client:
        for code in CURRENCIES:
            report = CurrencyReport(currency_code=code)
            try:
                rows = fetch_range(client, code, start, end)
            except httpx.HTTPError as exc:
                report.status = "error"
                report.error = f"{type(exc).__name__}: {exc}"
                summary.currencies.append(report)
                continue

            report.fetched = len(rows)
            try:
                with psycopg.connect(dsn) as conn, conn.transaction():
                    inserted, updated, unchanged = _upsert(conn, rows)
            except Exception as exc:  # noqa: BLE001 - reported, run continues
                report.status = "error"
                report.error = f"writing to the database: {type(exc).__name__}: {exc}"
                summary.currencies.append(report)
                continue

            report.inserted = inserted
            report.updated = updated
            report.unchanged = unchanged
            summary.currencies.append(report)

    summary.print_report()
    return summary
