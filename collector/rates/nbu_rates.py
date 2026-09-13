"""The NBU statistics API -- one currency's rate history over a date range.

A different bank.gov.ua service than the numismatic-catalog one
countries/ua/nbu_client.py talks to: plain JSON, no HTML, no pagination,
one request per currency covers however wide a range is asked for. That
is also why coin_keeper's own "sync one date at a time" approach (see
docs/04-business-rules.md, "Известная дыра" in that repo) was the wrong
shape -- the range is free, so ask for the whole thing at once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import httpx

BASE_URL = "https://bank.gov.ua/NBUStatService/v1/statdirectory"
USER_AGENT = "coin-collector/0.1 (personal project)"
CURRENCIES = ("USD", "EUR")


@dataclass(frozen=True)
class RateRow:
    currency_code: str
    rate_uah: Decimal
    effective_date: date


def _parse_response(text: str, currency_code: str) -> list[RateRow]:
    """`rate` is parsed straight out of the JSON text as a Decimal, never
    via float -- json.loads's default float handling would round-trip
    through a binary float first, and exchange_rates.rate_uah is
    numeric(14,6): a rate that came in exact must stay exact."""
    records = json.loads(text, parse_float=Decimal)
    rows = []
    for record in records:
        if record.get("cc") != currency_code:
            # Defensive: valcode already scopes the request to one
            # currency; a mismatch here would mean the API changed shape.
            continue
        effective_date = datetime.strptime(record["exchangedate"], "%d.%m.%Y").date()
        rows.append(
            RateRow(
                currency_code=currency_code,
                rate_uah=Decimal(record["rate"]),
                effective_date=effective_date,
            )
        )
    return rows


def fetch_range(
    client: httpx.Client, currency_code: str, start: date, end: date
) -> list[RateRow]:
    """One currency's published rate for every banking day NBU has in
    [start, end] -- non-banking days simply have no row, same as the
    source. Raises httpx.HTTPError on a network or HTTP-status failure;
    the caller decides what a failed currency means for the run."""
    # Built by hand, not via params=: the documented query has a bare
    # "json" flag with no "=value" (bank.gov.ua/ua/open-data/api-dev), and
    # httpx's params= would render it as "json=" -- lenient APIs accept
    # that, but there is no reason to rely on leniency here.
    query = (
        f"json&start={start.strftime('%Y%m%d')}&end={end.strftime('%Y%m%d')}"
        f"&valcode={currency_code}&sort=exchangedate&order=desc"
    )
    response = client.get(f"{BASE_URL}/exchange?{query}")
    response.raise_for_status()
    return _parse_response(response.text, currency_code)
