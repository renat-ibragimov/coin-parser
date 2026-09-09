"""Pure tests for the nightly update-prices step -- no network, no database.

The two halves that need a live system are deliberately not faked: the
paced download of the year pages and the INSERT (which is load_prices'
own, already covered there). What IS covered is everything that decides
what a night writes and what cron is told about it -- reading the price
and its date out of a yearly table, finding the coin's row by the id in
its stored link, "немає даних", the COPY batch, and the one summary line
a monitor greps.
"""

from datetime import date
from decimal import Decimal

import pytest

from collector.countries.ua import update_prices as up
from collector.countries.ua.load_prices import CURRENCY, GRADE, SOURCE
from collector.countries.ua.ua_coins import (
    YearTableAnomaly,
    parse_price_cell,
    parse_year_quotes,
    row_id_from_href,
)

PAGE_URL = "https://www.ua-coins.info/ua/list/1999-rizdvo-khrystove"


def _table_html(rows_html: str, header: str = "Вартість 08.09.2026") -> str:
    return f"""
    <table class="col-md-12 table-bordered table-striped table-sm cf coin-list">
      <thead class="cf"><tr class="tr_head">
        <td class="text-center">Дата</td><td class="text-center">Номінал</td>
        <td class="text-center">Тираж тис.</td><td>Назва</td>
        <td class="text-center">{header}</td>
      </tr></thead>
      <tr><td colspan="5" class="ua-table-year-cell">
        <a href="/ua/catalog/all/1999">1999</a>
      </td></tr>
      {rows_html}
    </table>
    """


def _row_html(href: str, title: str, price_html: str, denom: str = "10 грн.") -> str:
    return f"""
    <tr>
      <td data-title="Дата" class="date text-center"><span class="desktop">29.12.1999</span></td>
      <td class="text-center" data-title="Номінал">{denom}</td>
      <td class="text-center" data-title="Тираж тис.">10</td>
      <td class="ua-table-name-cell" data-title="Назва">
        <span class="nbu-store-title-line">
          <a href="{href}" class="nbu-store-title-text" title="{title}">{title}</a>
        </span>
      </td>
      <td class="ua-table-price-cell" data-title="Вартість 08.09.2026">
        <a href="{href}" class="list_price">{price_html}</a>
      </td>
    </tr>
    """


# ---------------------------------------------------------------------- #
# the price and its date, out of the yearly table
# ---------------------------------------------------------------------- #


def test_parse_year_quotes_reads_price_and_snapshot_date():
    html = _table_html(
        _row_html("/ua/list/1999-rizdvo-khrystove", "Різдво Христове", "7 568")
    )
    table = parse_year_quotes(html, 1999)
    assert table.as_of == date(2026, 9, 8)
    assert table.quotes[1999].price == Decimal("7568")
    assert table.quotes[1999].url == PAGE_URL


def test_snapshot_date_is_the_headers_not_today():
    # The whole point: a run on the 9th reading a table computed on the
    # 8th must write the 8th, or it invents a price nobody quoted.
    html = _table_html(
        _row_html("/ua/list/43-rizdvo-khrystove", "Різдво Христове", "568"),
        header="Вартість 07.09.2026",
    )
    assert parse_year_quotes(html, 1999).as_of == date(2026, 9, 7)


def test_price_keeps_the_thousands_separator_out_and_the_arrow_too():
    html = _table_html(
        _row_html(
            "/ua/list/2000-sonya-sadova",
            "Соня садова",
            '10 058 <span class="ua-price-delta ua-price-delta--up">↑</span>',
        )
    )
    assert parse_year_quotes(html, 2000).quotes[2000].price == Decimal("10058")


def test_nemaie_danykh_is_no_price_not_zero():
    html = _table_html(
        _row_html("/ua/list/1998-rizdvo-khrystove", "Різдво Христовe", "немає даних")
    )
    quote = parse_year_quotes(html, 1999).quotes[1998]
    assert quote.price is None
    assert quote.raw_text == "немає даних"


def test_empty_price_cell_is_no_price():
    html = _table_html(_row_html("/ua/list/55-shchedryk", "Щедрик", ""))
    assert parse_year_quotes(html, 1999).quotes[55].price is None


def test_a_year_with_no_table_at_all_is_not_an_anomaly():
    # ua-coins 404s the +1 of a current-year coin; fetch caches that as an
    # empty document. No rows, no date, no error.
    table = parse_year_quotes("", 2027)
    assert table.quotes == {} and table.as_of is None


def test_a_table_without_the_dated_header_is_an_anomaly():
    html = _table_html(
        _row_html("/ua/list/43-rizdvo-khrystove", "Різдво Христове", "568"),
        header="Вартість",
    ).replace('data-title="Вартість 08.09.2026"', 'data-title="Вартість"')
    with pytest.raises(YearTableAnomaly):
        parse_year_quotes(html, 1999)


def test_parse_price_cell_rejects_a_non_positive_price():
    assert parse_price_cell("0") is None
    assert parse_price_cell("немає даних") is None
    assert parse_price_cell("1 234,50") == Decimal("1234.50")


# ---------------------------------------------------------------------- #
# ua_coins_id out of the stored link
# ---------------------------------------------------------------------- #


def test_row_id_from_href_takes_the_leading_number_of_the_slug():
    assert row_id_from_href("/ua/list/43-rizdvo-khrystove") == 43
    assert row_id_from_href(PAGE_URL) == 1999  # an id that looks like a year
    assert row_id_from_href("https://www.ua-coins.info/ua/list/2449-zakhysnytsi/") == 2449


def test_row_id_from_href_of_a_slug_that_starts_with_no_number():
    assert row_id_from_href("/ua/list/rizdvo-khrystove") is None
    assert row_id_from_href("") is None


# ---------------------------------------------------------------------- #
# scope -> years
# ---------------------------------------------------------------------- #


def _coin(item_id, key, year, url=PAGE_URL):
    coin = up.ScopeCoin(item_id=item_id, source_key=key, issue_year=year)
    if url is not None:
        coin.page_url = url
        coin.ua_coins_id = row_id_from_href(url)
    return coin


def test_years_needed_widens_each_issue_year_by_one():
    scope = [_coin(1, "nbu:88", 1999), _coin(2, "nbu:90", 2000)]
    assert up.years_needed(scope) == [1998, 1999, 2000, 2001]


def test_years_needed_ignores_a_coin_with_no_issue_year():
    assert up.years_needed([_coin(1, "nbu:88", None)]) == []


# ---------------------------------------------------------------------- #
# the batch
# ---------------------------------------------------------------------- #


def _tables(*specs):
    """(year, as_of, {id: price_html}) -> parsed YearQuotes by year."""
    out = {}
    for year, as_of, rows in specs:
        html = _table_html(
            "".join(
                _row_html(f"/ua/list/{coin_id}-x", f"coin {coin_id}", price)
                for coin_id, price in rows.items()
            ),
            header=f"Вартість {as_of}",
        )
        out[year] = parse_year_quotes(html, year)
    return out


def test_build_rows_makes_one_snapshot_per_quoted_coin():
    scope = [_coin(77, "nbu:161", 1999, "https://www.ua-coins.info/ua/list/43-rizdvo")]
    tables = _tables((1999, "08.09.2026", {43: "568"}))

    rows, reports = up.build_rows(scope, tables)

    assert len(rows) == 1
    item_id, source, grade, price, currency, observed_at, url, raw = rows[0]
    assert (item_id, source, grade, currency) == (77, SOURCE, GRADE, CURRENCY)
    assert price == Decimal("568")
    # Midnight UTC on the HEADER's day, not the run's -- same pinning
    # load-prices uses, which is what lets the two share a history.
    assert observed_at.isoformat() == "2026-09-08T00:00:00+00:00"
    assert url == "https://www.ua-coins.info/ua/list/43-rizdvo"
    assert raw == {"date": "2026-09-08", "price": 568.0, "table_year": 1999}
    assert reports[0].status == "quoted"


def test_build_rows_counts_a_no_data_row_as_no_quote_and_writes_nothing():
    # nbu:88's own case: ua-coins has quoted nothing for it since 2024.
    scope = [_coin(88, "nbu:88", 1999, "https://www.ua-coins.info/ua/list/1998-rizdvo")]
    tables = _tables((1999, "08.09.2026", {1998: "немає даних"}))

    rows, reports = up.build_rows(scope, tables)

    assert rows == []
    assert reports[0].status == "no_quote:no_data"


def test_build_rows_counts_a_coin_with_no_link_as_no_link():
    scope = [_coin(5, "nbu:5", 1999, url=None)]
    rows, reports = up.build_rows(scope, _tables((1999, "08.09.2026", {43: "568"})))
    assert rows == []
    assert reports[0].status == "no_link"


def test_build_rows_counts_a_coin_missing_from_tonights_tables():
    scope = [_coin(9, "nbu:9", 1999, "https://www.ua-coins.info/ua/list/999-x")]
    rows, reports = up.build_rows(scope, _tables((1999, "08.09.2026", {43: "568"})))
    assert rows == []
    assert reports[0].status == "no_quote:not_listed"


def test_build_rows_takes_the_year_shifted_table_when_that_is_where_the_row_is():
    scope = [_coin(77, "nbu:161", 2000, "https://www.ua-coins.info/ua/list/43-rizdvo")]
    tables = _tables((1999, "08.09.2026", {43: "568"}), (2000, "08.09.2026", {}))

    rows, reports = up.build_rows(scope, tables)

    assert len(rows) == 1
    assert reports[0].table_year == 1999


def test_build_rows_prefers_the_coins_own_year_when_two_tables_list_it():
    scope = [_coin(77, "nbu:161", 2000, "https://www.ua-coins.info/ua/list/43-rizdvo")]
    tables = _tables(
        (1999, "08.09.2026", {43: "568"}),
        (2000, "08.09.2026", {43: "590"}),
    )

    rows, reports = up.build_rows(scope, tables)

    assert rows[0][3] == Decimal("590")
    assert reports[0].table_year == 2000


def test_build_rows_refuses_a_price_the_column_cannot_hold():
    scope = [_coin(77, "nbu:161", 1999, "https://www.ua-coins.info/ua/list/43-rizdvo")]
    tables = _tables((1999, "08.09.2026", {43: "9 999 999 999 999"}))

    rows, reports = up.build_rows(scope, tables)

    assert rows == []
    assert reports[0].status == "no_quote:unusable"


# ---------------------------------------------------------------------- #
# the summary line and the exit code -- the whole report, for cron
# ---------------------------------------------------------------------- #


def _summary(**kwargs):
    summary = up.UpdatePricesSummary(run_date=date(2026, 9, 9), **kwargs)
    return summary


def _report(status, inserted=0, duplicates=0):
    return up.CoinQuoteReport(
        source_key="nbu:1", item_id=1, status=status, inserted=inserted, duplicates=duplicates
    )


def test_summary_line_counts_every_bucket():
    summary = _summary(
        scope=6,
        years_ok=[1998, 1999, 2000],
        coins=[
            _report("quoted", inserted=1),
            _report("quoted", inserted=1),
            _report("quoted", duplicates=1),
            _report("no_quote:no_data"),
            _report("no_quote:not_listed"),
            _report("no_link"),
        ],
    )
    assert summary.summary_line() == (
        "update-prices ok scope=6 years=3 matched=3 inserted=2 dup=1 "
        "no_quote=2 no_link=1 errors=0"
    )
    assert summary.exit_code == up.EXIT_OK


def test_scope_is_the_sum_of_the_three_buckets_and_matched_is_ins_plus_dup():
    summary = _summary(
        scope=3,
        years_ok=[1999],
        coins=[_report("quoted", inserted=1), _report("no_quote:no_data"), _report("no_link")],
    )
    assert summary.no_link + summary.no_quote + summary.matched == summary.scope
    assert summary.inserted + summary.duplicates == summary.matched


def test_a_night_with_nothing_new_is_still_ok():
    # ua-coins does not requote every coin every day, and a rerun reads
    # the same table -- inserted=0 is the normal shape of both.
    summary = _summary(scope=1, years_ok=[1999], coins=[_report("quoted", duplicates=1)])
    assert summary.exit_code == up.EXIT_OK
    assert "ok" in summary.summary_line()


def test_some_years_lost_is_partial():
    summary = _summary(
        scope=1, years_ok=[1999], years_failed=[2000], coins=[_report("quoted", inserted=1)]
    )
    assert summary.exit_code == up.EXIT_PARTIAL
    assert summary.summary_line().startswith("update-prices partial ")
    assert "errors=1" in summary.summary_line()


def test_every_year_lost_is_nothing_done():
    summary = _summary(scope=1, years_failed=[1999, 2000], coins=[_report("no_quote:not_listed")])
    assert summary.exit_code == up.EXIT_NOTHING_DONE
    assert summary.summary_line().startswith("update-prices failed ")


def test_no_database_is_nothing_done_and_still_prints_the_line():
    summary = _summary(error="DATABASE_URL is not set")
    assert summary.exit_code == up.EXIT_NOTHING_DONE
    assert summary.summary_line() == (
        "update-prices failed scope=0 years=0 matched=0 inserted=0 dup=0 "
        "no_quote=0 no_link=0 errors=1"
    )


def test_the_report_ends_with_exactly_one_summary_line(capsys):
    summary = _summary(scope=1, years_ok=[1999], coins=[_report("quoted", inserted=1)])
    summary.print_report()
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == summary.summary_line()
    assert sum(1 for line in out if line.startswith("update-prices ")) == 1


# ---------------------------------------------------------------------- #
# keeping the night's HTML, and not forever
# ---------------------------------------------------------------------- #


def test_prune_daily_removes_only_dated_dirs_past_the_window(tmp_path):
    root = tmp_path / "ua" / "_ua_coins" / "daily"
    # 14 days back from the 9th is the 26th: the 27th stays, the 25th goes.
    for name in ("2026-09-09", "2026-08-27", "2026-08-25", "notes"):
        (root / name).mkdir(parents=True)

    removed = up.prune_daily(tmp_path, date(2026, 9, 9), keep_days=14)

    assert removed == ["2026-08-25"]
    assert (root / "2026-08-27").exists()
    assert (root / "notes").exists()


def test_prune_daily_on_a_server_that_has_never_run_it(tmp_path):
    assert up.prune_daily(tmp_path, date(2026, 9, 9)) == []
