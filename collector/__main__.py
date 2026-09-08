"""CLI entry point.

    python -m collector ua --series "<name>" --step fetch|parse|all|match
    python -m collector ua --step series
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
        help="Ukrainian series name, exact match (not needed for --step series)",
    )
    ua.add_argument(
        "--step",
        choices=["fetch", "parse", "all", "series", "match"],
        default="all",
        help="which step to run ('all' = fetch, parse, match)",
    )
    ua.add_argument(
        "--refresh-ua-coins",
        action="store_true",
        help="refetch cached ua-coins.info yearly catalog pages instead of reusing staging/ua/_ua_coins/raw",
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

    if not args.series:
        print("error: --series is required unless --step series", file=sys.stderr)
        return 2

    parser = ParserUkraine(series=args.series)
    if args.step in ("fetch", "all"):
        parser.fetch()
    if args.step in ("parse", "all"):
        parser.parse()
    if args.step in ("match", "all"):
        parser.match_ua_coins(refresh=args.refresh_ua_coins)

    return 0


if __name__ == "__main__":
    sys.exit(main())
