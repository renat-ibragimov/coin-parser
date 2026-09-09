"""CLI entry point.

    python -m collector ua --series "<name>" --step fetch|parse|all|match|fetch-photos|process-photos
    python -m collector ua --step series
    python -m collector ua --step load-series   # writes to coin_keeper's DB, needs DATABASE_URL
    python -m collector ua --series "<name>" --step fetch-prices
    python -m collector ua --step load-prices   # writes to coin_keeper's DB, needs DATABASE_URL
                                                # --series narrows it to one series
"""

from __future__ import annotations

import argparse
import sys

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
            "load-series; optional for load-prices, which narrows to it when given)"
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
            "load-prices",
        ],
        default="all",
        help=(
            "which step to run ('all' = fetch, parse, match, fetch-photos, process-photos "
            "-- fetch-prices/load-prices stay out of 'all' on purpose: one hits the "
            "network hard, the other writes to production)"
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

    if args.step == "series":
        parser = ParserUkraine()
        summary = parser.collect_series()
        return 1 if summary.conflicts else 0

    if args.step == "load-series":
        parser = ParserUkraine()
        summary = parser.load_series()
        return 1 if summary.error else 0

    if args.step == "load-prices":
        # Unlike the other per-series steps this one runs with or without
        # --series: the price cache is shared across series, so "load
        # everything staged" is just as meaningful as "load this series".
        parser = ParserUkraine(series=args.series) if args.series else ParserUkraine()
        summary = parser.load_prices()
        return 1 if summary.error else 0

    if not args.series:
        print(
            "error: --series is required unless --step series/load-series/load-prices",
            file=sys.stderr,
        )
        return 2

    parser = ParserUkraine(series=args.series)
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
