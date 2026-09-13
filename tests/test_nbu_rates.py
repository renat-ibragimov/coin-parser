"""Pure tests for the NBU exchange rate archive client -- request shape
and response parsing, no real network (httpx.MockTransport stands in
for bank.gov.ua)."""

from datetime import date
from decimal import Decimal

import httpx
import pytest

from collector.rates import nbu_rates as nr


def _transport(body: str, capture: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["url"] = str(request.url)
        return httpx.Response(200, text=body)

    return httpx.MockTransport(handler)


def test_fetch_range_builds_the_working_range_query():
    capture: dict = {}
    client = httpx.Client(transport=_transport("[]", capture))

    nr.fetch_range(client, "USD", date(2026, 1, 1), date(2026, 1, 31))

    url = capture["url"]
    assert url.startswith(f"{nr.BASE_URL}/exchange_site?")
    assert "start=20260101" in url
    assert "end=20260131" in url
    assert "valcode=USD" in url
    assert "sort=exchangedate" in url
    assert "order=asc" in url
    # A bare "json" flag at the end, matching bank.gov.ua's own archive page.
    assert url.endswith("&json")


def test_fetch_range_parses_rate_per_unit_and_date():
    body = (
        '[{"exchangedate":"13.09.2026","r030":840,"cc":"USD","rate":44.5526,'
        '"units":1,"rate_per_unit":44.5526,"group":"1"}]'
    )
    client = httpx.Client(transport=_transport(body))

    rows = nr.fetch_range(client, "USD", date(2026, 9, 13), date(2026, 9, 13))

    assert rows == [nr.RateRow("USD", Decimal("44.5526"), date(2026, 9, 13))]


def test_a_scaled_historical_rate_uses_rate_per_unit_not_rate():
    # 2002-01-01 USD, verified live: rate=529.85 is per 100 units (early-
    # hryvnia scaling), not per 1 -- storing `rate` here would be 100x off.
    body = (
        '[{"exchangedate":"01.01.2002","cc":"USD","rate":529.85,'
        '"units":100,"rate_per_unit":5.2985}]'
    )
    client = httpx.Client(transport=_transport(body))

    rows = nr.fetch_range(client, "USD", date(2002, 1, 1), date(2002, 1, 1))

    assert rows[0].rate_uah == Decimal("5.2985")


def test_rate_keeps_full_precision_no_float_roundtrip():
    # A value float() cannot represent exactly -- proof parse_float=Decimal
    # is actually taking the literal apart, not the binary float of it.
    body = '[{"rate_per_unit":27.123456,"cc":"USD","exchangedate":"01.01.2026"}]'
    client = httpx.Client(transport=_transport(body))

    rows = nr.fetch_range(client, "USD", date(2026, 1, 1), date(2026, 1, 1))

    assert rows[0].rate_uah == Decimal("27.123456")
    assert str(rows[0].rate_uah) == "27.123456"


def test_a_row_for_the_wrong_currency_is_dropped():
    # Defensive: valcode already scopes the request; a stray EUR row
    # inside a USD response should never happen, but must not be kept
    # silently if it does.
    body = (
        '[{"rate_per_unit":41.85,"cc":"USD","exchangedate":"13.09.2026"},'
        '{"rate_per_unit":48.10,"cc":"EUR","exchangedate":"13.09.2026"}]'
    )
    client = httpx.Client(transport=_transport(body))

    rows = nr.fetch_range(client, "USD", date(2026, 9, 13), date(2026, 9, 13))

    assert len(rows) == 1
    assert rows[0].currency_code == "USD"


def test_an_empty_range_is_an_empty_list_not_an_error():
    client = httpx.Client(transport=_transport("[]"))

    rows = nr.fetch_range(client, "USD", date(2026, 1, 1), date(2026, 1, 1))

    assert rows == []


def test_a_bad_status_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(httpx.HTTPStatusError):
        nr.fetch_range(client, "USD", date(2026, 1, 1), date(2026, 1, 1))
