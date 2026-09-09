"""CLI entry point.

    python -m collector ua --series "<name>" --step fetch|parse|all|match|fetch-photos|process-photos
    python -m collector ua --step series
    python -m collector ua --step load-series   # writes to coin_keeper's DB, needs DATABASE_URL
    python -m collector ua --series "<name>" --step fetch-prices
    python -m collector ua --series "<name>" --step load-cards
                                                # writes to coin_keeper's DB, needs DATABASE_URL
                                                # mirror media/out into the bucket FIRST --
                                                # see collector/countries/ua/load_cards.py
    python -m collector ua --step load-prices   # writes to coin_keeper's DB, needs DATABASE_URL
                                                # --series narrows it to one series
    python -m collector ua --step update-prices # nightly cron step: today's ua-coins quote
                                                # for every nbu:* coin already in the DB.
                                                # Scope comes from the DB, not from staging.
                                                # Exits 0 ok / 1 partial / 2 nothing done.

The four steps that write to production (load-series, load-cards,
load-prices, plus the bucket mirror that has to happen between the last
two) are not interchangeable and have preconditions. The full procedure
-- server access, the ssh tunnel postgres needs, what to check after
each step -- is docs/03_series_playbook.md.

update-prices stands apart from all of them: it is the one step meant to
run unattended, on the server, from deploy/crontab. It has no
preconditions beyond a loaded catalog, prints one greppable summary line,
and says everything else through its exit code -- see the "Суточные цены"
section of the playbook.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from collector.core.staging import DEFAULT_STAGING_ROOT
from collector.countries.ua.parser import ParserUkraine


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="collector")
    subparsers = parser.add_subparsers(dest="country", required=True)

    ua = subparsers.add_parser("ua", help="National Bank of Ukraine souvenir coins")
    ua.add_argument(
        "--series",
        default=None,
        help=(
            "Ukrainian series name, exact match (not needed for --step series/"
            "load-series/update-prices; optional for load-prices, which narrows to it "
            "when given)"
        ),
    )
    ua.add_argument(
        "--step",
        choices=[
            "fetch",
            "parse",
            "all",
            "series",
            "match",
            "load-series",
            "fetch-photos",
            "process-photos",
            "fetch-prices",
            "load-cards",
            "load-prices",
            "update-prices",
        ],
        default="all",
        help=(
            "which step to run ('all' = fetch, parse, match, fetch-photos, process-photos "
            "-- fetch-prices/load-cards/load-prices/update-prices stay out of 'all' on "
            "purpose: one hits the network hard, the others write to production, "
            "load-cards needs the media mirrored into the bucket first, and update-prices "
            "is the nightly cron step rather than part of collecting a series)"
        ),
    )
    ua.add_argument(
        "--staging-root",
        default=os.environ.get("COLLECTOR_STAGING_ROOT") or str(DEFAULT_STAGING_ROOT),
        help=(
            "where the staging tree lives (default: ./staging, or "
            "COLLECTOR_STAGING_ROOT). The deployed container mounts a volume "
            "elsewhere and sets that variable, so its working directory does not "
            "have to be writable"
        ),
    )
    ua.add_argument(
        "--refresh-ua-coins",
        action="store_true",
        help="refetch cached ua-coins.info yearly catalog pages instead of reusing staging/ua/_ua_coins/raw",
    )
    ua.add_argument(
        "--refresh-photos",
        action="store_true",
        help="redownload candidate photos instead of reusing staging/ua/<slug>/media/src",
    )
    ua.add_argument(
        "--drop-ucoin-prices",
        action="store_true",
        help=(
            "load-prices only: also DELETE the legacy uCoin price history of the coins "
            "this run just loaded a ua-coins history for (never the others -- a coin "
            "ua-coins does not quote keeps its uCoin rows, they are its only prices)"
        ),
    )
    ua.add_argument(
        "--refresh-prices",
        action="store_true",
        help=(
            "refetch price history instead of reusing staging/ua/_ua_coins/prices "
            "(the shared cache is keyed by ua-coins id, not by series)"
        ),
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.country != "ua":
        return 0

    staging_root = Path(args.staging_root)

    if args.step == "series":
        parser = ParserUkraine(staging_root=staging_root)
        summary = parser.collect_series()
        return 1 if summary.conflicts else 0

    if args.step == "load-series":
        parser = ParserUkraine(staging_root=staging_root)
        summary = parser.load_series()
        return 1 if summary.error else 0

    if args.step == "update-prices":
        # The cron step. Nothing about it is per-series (its scope is the
        # catalog itself), and its exit code is the whole report as far
        # as cron is concerned: 0 fine, 1 some years lost, 2 nothing done.
        return ParserUkraine(staging_root=staging_root).update_prices().exit_code

    if args.step == "load-prices":
        # Unlike the other per-series steps this one runs with or without
        # --series: the price cache is shared across series, so "load
        # everything staged" is just as meaningful as "load this series".
        parser = (
            ParserUkraine(series=args.series, staging_root=staging_root)
            if args.series
            else ParserUkraine(staging_root=staging_root)
        )
        summary = parser.load_prices(drop_ucoin=args.drop_ucoin_prices)
        return 1 if summary.error else 0

    if not args.series:
        print(
            "error: --series is required unless --step "
            "series/load-series/load-prices/update-prices",
            file=sys.stderr,
        )
        return 2

    parser = ParserUkraine(series=args.series, staging_root=staging_root)

    if args.step == "load-cards":
        summary = parser.load_cards()
        return 1 if summary.error else 0

    if args.step in ("fetch", "all"):
        parser.fetch()
    if args.step in ("parse", "all"):
        parser.parse()
    if args.step in ("match", "all"):
        parser.match_ua_coins(refresh=args.refresh_ua_coins)
    if args.step in ("fetch-photos", "all"):
        parser.fetch_photos(refresh=args.refresh_photos)
    if args.step in ("process-photos", "all"):
        parser.process_photos()
    if args.step == "fetch-prices":
        parser.fetch_prices(refresh=args.refresh_prices)

    return 0


if __name__ == "__main__":
    sys.exit(main())
