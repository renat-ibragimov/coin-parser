"""The NBU exchange rate archive -- one currency's rate history over a
date range, one request regardless of how wide the range is.

Not bank.gov.ua/NBUStatService/v1/statdirectory/exchange, despite that
being the one documented at bank.gov.ua/ua/open-data/api-dev and the one
docs/integrations.md (coin_keeper) names: that endpoint only accepts
a single `date`, and silently ignores `start`/`end`/`valcode` -- every
combination tried against it returned one row, today's, regardless of
the range asked for (verified live, 2026-09-13). The endpoint that
actually serves a range is undocumented but is what bank.gov.ua's own
exchange-rate archive page calls: NBU_Exchange/exchange_site.

Its `rate` field is also not what it is on the documented endpoint: for
old records it is "rate per `units` units", not "rate per unit" --
e.g. 2002-01-01 USD carries rate=529.85, units=100 (the early-hryvnia
scaling), and `rate_per_unit` is the 5.2985 that actually belongs in
exchange_rates.rate_uah. `units` is 1 for every modern date, where the
two fields agree, so this is only visible on the older history a
backfill actually needs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import httpx

BASE_URL = "https://bank.gov.ua/NBU_Exchange"
USER_AGENT = "coin-collector/0.1 (personal project)"
CURRENCIES = ("USD", "EUR")


@dataclass(frozen=True)
class RateRow:
    currency_code: str
    rate_uah: Decimal
    effective_date: date


def _parse_response(text: str, currency_code: str) -> list[RateRow]:
    """`rate_per_unit` is parsed straight out of the JSON text as a
    Decimal, never via float -- json.loads's default float handling
    would round-trip through a binary float first, and
    exchange_rates.rate_uah is numeric(14,6): a rate that came in exact
    must stay exact."""
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
                rate_uah=Decimal(record["rate_per_unit"]),
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
    query = (
        f"start={start.strftime('%Y%m%d')}&end={end.strftime('%Y%m%d')}"
        f"&valcode={currency_code}&sort=exchangedate&order=asc&json"
    )
    response = client.get(f"{BASE_URL}/exchange_site?{query}")
    response.raise_for_status()
    return _parse_response(response.text, currency_code)
