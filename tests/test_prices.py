"""Pure tests for the price-history steps -- no network, no database.

The two halves that need a live system are deliberately not faked here:
fetch_prices' HTTP conversation and load_prices' transaction. What IS
covered is everything that decides what ends up in the production
history -- pulling the signed chart URL out of a page, validating the
series, and the exact tuples that get COPYed.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

import collector.countries.ua.load_prices as load_prices_mod
from collector.countries.ua.load_prices import (
    CURRENCY,
    GRADE,
    SOURCE,
    build_card_index,
    build_copy_rows,
)
from collector.countries.ua.prices import (
    PriceSeriesAnomaly,
    cached_ua_coins_id,
    file_stem,
    meta_path,
    page_path,
    prices_path,
    check_control_point,
    extract_current_price,
    extract_prices_url,
    parse_price_series,
)


# ---------------------------------------------------------------------- #
# extract_prices_url
# ---------------------------------------------------------------------- #

SIGNED = "/coin/prices/2449?_hash=6f1c2a&expires=1764547200"


def test_extract_prices_url_from_plain_href():
    html = f'<div id="chart"><a href="{SIGNED}">графік</a></div>'
    ep = extract_prices_url(html, 2449)
    assert ep.path == SIGNED
    assert ep.coin_id == 2449
    assert ep.signed


def test_extract_prices_url_from_html_escaped_attribute():
    # An attribute always carries &amp; rather than a bare ampersand.
    html = '<div data-url="/coin/prices/2449?_hash=6f1c2a&amp;expires=1764547200"></div>'
    assert extract_prices_url(html, 2449).path == SIGNED


def test_extract_prices_url_from_inline_script_with_escaped_slashes():
    html = (
        "<script>var chart = {url: "
        '"https:\\/\\/www.ua-coins.info\\/coin\\/prices\\/2449?_hash=6f1c2a&expires=1764547200"'
        "};</script>"
    )
    assert extract_prices_url(html, 2449).path == SIGNED


def test_extract_prices_url_from_json_in_a_data_attribute():
    html = (
        '<div data-chart="{&quot;src&quot;:&quot;/coin/prices/2449'
        '?_hash=6f1c2a&amp;expires=1764547200&quot;}"></div>'
    )
    assert extract_prices_url(html, 2449).path == SIGNED


def test_extract_prices_url_prefers_the_signed_link_over_a_bare_one():
    html = f'<a href="/coin/prices/2449">stub</a><a href="{SIGNED}">real</a>'
    ep = extract_prices_url(html, 2449)
    assert ep.path == SIGNED
    assert ep.signed


def test_extract_prices_url_ignores_another_coins_chart():
    html = f'<a href="/coin/prices/1111?_hash=x&expires=1">other</a><a href="{SIGNED}">mine</a>'
    assert extract_prices_url(html, 2449).path == SIGNED


def test_extract_prices_url_falls_back_to_a_different_id_and_says_so():
    # Worth a warning, not worth losing the history over.
    html = '<a href="/coin/prices/1111?_hash=x&expires=1">other</a>'
    ep = extract_prices_url(html, 2449)
    assert ep.coin_id == 1111


def test_extract_prices_url_returns_none_when_the_page_has_no_chart():
    assert extract_prices_url("<html><body>no chart here</body></html>", 2449) is None


# ---------------------------------------------------------------------- #
# parse_price_series
# ---------------------------------------------------------------------- #


def _series(*pairs):
    return [{"date": d, "price": p} for d, p in pairs]


def test_parse_price_series_happy_path():
    points = parse_price_series(_series(("2023-11-24", 481), ("2023-11-25", 480)))
    assert [p.day for p in points] == [date(2023, 11, 24), date(2023, 11, 25)]
    assert [p.price for p in points] == [Decimal("481"), Decimal("480")]
    assert points[0].raw == {"date": "2023-11-24", "price": 481}


def test_parse_price_series_accepts_gaps_in_the_dates():
    # Missing days and missing weeks are normal for this source.
    points = parse_price_series(
        _series(("2023-11-24", 481), ("2023-11-25", 480), ("2023-12-18", 495))
    )
    assert len(points) == 3


def test_parse_price_series_accepts_single_day_spikes():
    # A thin market: 520 -> 650 -> 520 is a real trade, not noise.
    points = parse_price_series(
        _series(("2024-01-01", 520), ("2024-01-02", 650), ("2024-01-03", 520))
    )
    assert [p.price for p in points] == [Decimal("520"), Decimal("650"), Decimal("520")]


def test_parse_price_series_accepts_float_prices():
    points = parse_price_series(_series(("2024-01-01", 1234.5)))
    assert points[0].price == Decimal("1234.5")


def test_parse_price_series_accepts_an_empty_history():
    assert parse_price_series([]) == []


def test_parse_price_series_rejects_dates_out_of_order():
    with pytest.raises(PriceSeriesAnomaly, match="strictly increasing"):
        parse_price_series(_series(("2024-01-02", 10), ("2024-01-01", 11)))


def test_parse_price_series_rejects_a_repeated_date():
    with pytest.raises(PriceSeriesAnomaly, match="strictly increasing"):
        parse_price_series(_series(("2024-01-01", 10), ("2024-01-01", 11)))


def test_parse_price_series_rejects_an_unparseable_date():
    with pytest.raises(PriceSeriesAnomaly, match="not an ISO date"):
        parse_price_series(_series(("24.11.2023", 481)))


def test_parse_price_series_rejects_a_non_positive_price():
    with pytest.raises(PriceSeriesAnomaly, match="not > 0"):
        parse_price_series(_series(("2024-01-01", 0)))


def test_parse_price_series_rejects_a_non_numeric_price():
    with pytest.raises(PriceSeriesAnomaly, match="not a number"):
        parse_price_series(_series(("2024-01-01", None)))


def test_parse_price_series_rejects_a_boolean_price():
    # bool is an int in Python -- `true` would otherwise become 1 UAH.
    with pytest.raises(PriceSeriesAnomaly, match="boolean"):
        parse_price_series(_series(("2024-01-01", True)))


def test_parse_price_series_rejects_a_price_too_big_for_the_column():
    with pytest.raises(PriceSeriesAnomaly, match="numeric"):
        parse_price_series(_series(("2024-01-01", 10**13)))


def test_parse_price_series_rejects_a_non_array_response():
    with pytest.raises(PriceSeriesAnomaly, match="expected a JSON array"):
        parse_price_series({"error": "expired"})


# ---------------------------------------------------------------------- #
# the page's own current price -- the control point
# ---------------------------------------------------------------------- #


# The markup ua-coins actually serves, copied from
# staging/ua/_ua_coins/pages/43.html. The h1 is in here on purpose: its
# "5 грн." is the coin's FACE VALUE, and reading that as the current
# price is exactly the bug these tests exist to keep out.
def _coin_page(note: str, h1: str = "Монета «Різдво Христове» 5 грн., 1999, нейзильбер") -> str:
    return f"""
    <body>
      <h1>{h1}</h1>
      <div class="coin-price">568 грн.</div>
      <section class="coin-price-note" aria-labelledby="coin-price-note-title">
        <h2 class="coin-price-note__title" id="coin-price-note-title">
            Скільки коштує «Різдво Христове»
        </h2><p class="coin-price-note__text">
            {note}
        </p>
      </section>
    </body>
    """


REAL_NOTE = (
    "Орієнтовна ринкова ціна станом на 09.09.2026 — 568 грн.\n\n"
    "Від 18.07.2016 ринкова ціна зросла з 188 до 568 грн. — на 202.1%.\n"
    "Ринковий орієнтир вищий за ціну НБУ на 11 260.0%."
)


def test_extract_current_price_reads_the_price_note():
    best, _ = extract_current_price(_coin_page(REAL_NOTE))
    assert best.price == Decimal("568")
    assert best.date == date(2026, 9, 9)
    assert best.found_by == "price-note"


def test_extract_current_price_is_not_fooled_by_the_face_value_in_the_h1():
    # The whole reason the control point is anchored on a sentence: the
    # first dated "<number> грн" on a real page is the denomination.
    best, _ = extract_current_price(_coin_page(REAL_NOTE))
    assert best.price != Decimal("5")


def test_extract_current_price_ignores_the_starting_price_in_the_same_note():
    # "зросла з 188 до 568" sits in the very same paragraph; only the
    # "станом на" clause names the current price.
    best, _ = extract_current_price(_coin_page(REAL_NOTE))
    assert best.price == Decimal("568")


def test_extract_current_price_handles_thousands_separators():
    note = "Орієнтовна ринкова ціна станом на 09.09.2026 — 8 060 грн."
    best, _ = extract_current_price(_coin_page(note))
    assert best.price == Decimal("8060")


def test_extract_current_price_finds_the_sentence_without_the_note_class():
    # Class renamed, sentence intact -- still found, via the page-wide pass.
    html = "<body><h1>Монета 5 грн., 1999</h1><p>Ціна станом на 09.09.2026 — 568 грн.</p></body>"
    best, _ = extract_current_price(html)
    assert best.price == Decimal("568")
    assert best.found_by == "as-of-sentence"


def test_extract_current_price_falls_back_to_a_scan_when_the_sentence_is_gone():
    html = '<body><div class="price">Ціна: 480 грн на 25.11.2023</div></body>'
    best, _ = extract_current_price(html)
    assert best.price == Decimal("480")
    assert best.date == date(2023, 11, 25)
    assert best.found_by == "scan"


def test_extract_current_price_scan_takes_the_date_from_the_parent():
    html = '<body><div>станом на 2023-11-25 <span>480 грн</span></div></body>'
    best, _ = extract_current_price(html)
    assert best.price == Decimal("480")
    assert best.date == date(2023, 11, 25)


def test_extract_current_price_scan_handles_thousands_without_a_separator():
    # A separator-shaped pattern silently truncates this to its first
    # three digits.
    best, _ = extract_current_price('<body><span>12500 грн 25.11.2023</span></body>')
    assert best.price == Decimal("12500")


def test_extract_current_price_is_case_insensitive_about_the_currency():
    best, _ = extract_current_price('<body><span>481 ГРН на 24.11.2023</span></body>')
    assert best.price == Decimal("481")


def test_extract_current_price_ignores_numbers_that_are_not_prices():
    # Mintage and a bare year sit on the same page as the price.
    best, candidates = extract_current_price('<body><div>Тираж 30000 шт, 2023 рік</div></body>')
    assert best is None and candidates == []


def test_extract_current_price_still_lists_scan_candidates_for_the_meta_file():
    # When the sentence stops matching, the meta has to show what the
    # page does say instead of a bare "not found".
    _, candidates = extract_current_price(_coin_page(REAL_NOTE))
    assert any(c.price == Decimal("568") for c in candidates)


def test_check_control_point_ok_when_the_page_agrees_with_the_last_point():
    points = parse_price_series(_series(("2026-09-08", 560), ("2026-09-09", 568)))
    best, _ = extract_current_price(_coin_page(REAL_NOTE))
    verdict, warning = check_control_point(points, best)
    assert verdict == "ok" and warning is None


def test_check_control_point_warns_on_a_price_mismatch():
    points = parse_price_series(_series(("2026-09-09", 999)))
    best, _ = extract_current_price(_coin_page(REAL_NOTE))
    verdict, warning = check_control_point(points, best)
    assert verdict == "mismatch"
    assert "568" in warning


def test_check_control_point_reports_a_missing_page_price():
    points = parse_price_series(_series(("2023-11-25", 480)))
    verdict, warning = check_control_point(points, None)
    assert verdict == "not_found" and warning


def test_check_control_point_accepts_a_history_that_simply_stopped():
    # nbu:88 / ua-coins 1998: last traded 2024-09-25 and the page quotes
    # that same day. A stale history is not a broken one.
    points = parse_price_series(_series(("2024-09-24", 59000), ("2024-09-25", 60000)))
    note = "Орієнтовна ринкова ціна станом на 25.09.2024 — 60 000 грн."
    best, _ = extract_current_price(_coin_page(note))
    assert check_control_point(points, best) == ("ok", None)


# ---------------------------------------------------------------------- #
# build_copy_rows
# ---------------------------------------------------------------------- #

PAGE_URL = "https://www.ua-coins.info/ua/list/2449-zakhysnytsi"


def test_build_copy_rows_shape():
    points = parse_price_series(_series(("2023-11-24", 481), ("2023-11-25", 480)))
    rows = build_copy_rows(77, points, PAGE_URL)
    assert rows == [
        (
            77,
            SOURCE,
            GRADE,
            Decimal("481"),
            CURRENCY,
            datetime(2023, 11, 24, tzinfo=timezone.utc),
            PAGE_URL,
            {"date": "2023-11-24", "price": 481},
        ),
        (
            77,
            SOURCE,
            GRADE,
            Decimal("480"),
            CURRENCY,
            datetime(2023, 11, 25, tzinfo=timezone.utc),
            PAGE_URL,
            {"date": "2023-11-25", "price": 480},
        ),
    ]


def test_build_copy_rows_pins_observed_at_to_midnight_utc():
    # Rerunning the load must land on the exact same instant, or the
    # unique constraint has nothing to recognise.
    points = parse_price_series(_series(("2024-02-29", 100)))
    observed_at = build_copy_rows(1, points, PAGE_URL)[0][5]
    assert observed_at == datetime(2024, 2, 29, 0, 0, 0, tzinfo=timezone.utc)
    assert observed_at.utcoffset().total_seconds() == 0


def test_build_copy_rows_uses_the_coin_page_not_the_signed_chart_url():
    # The signed URL expires in about two days; the history keeps a link
    # that still resolves next year.
    points = parse_price_series(_series(("2024-01-01", 100)))
    assert build_copy_rows(1, points, PAGE_URL)[0][6] == PAGE_URL


def test_build_copy_rows_of_an_empty_history_is_empty():
    assert build_copy_rows(1, [], PAGE_URL) == []


# ---------------------------------------------------------------------- #
# build_card_index
# ---------------------------------------------------------------------- #


def _write_cards(tmp_path, slug, cards):
    parsed = tmp_path / "ua" / slug / "parsed"
    parsed.mkdir(parents=True, exist_ok=True)
    (parsed / "cards.json").write_text(
        __import__("json").dumps({"cards": cards}, ensure_ascii=False), encoding="utf-8"
    )


def _card(source_id, ua_coins_id):
    return {
        "source_id": source_id,
        "ua_coins": {"id": ua_coins_id, "url": f"https://www.ua-coins.info/ua/list/{ua_coins_id}-x"},
    }


def test_build_card_index_maps_ua_coins_id_to_card(tmp_path):
    _write_cards(tmp_path, "series-a", [_card("nbu:88", 1998), _card("nbu:89", 1999)])
    index, warnings = build_card_index(tmp_path)
    assert index[1998].source_id == "nbu:88"
    assert index[1999].source_id == "nbu:89"
    assert warnings == []


def test_build_card_index_skips_unmatched_cards(tmp_path):
    _write_cards(tmp_path, "series-a", [{"source_id": "nbu:90", "ua_coins": None}])
    index, _ = build_card_index(tmp_path)
    assert index == {}


def test_build_card_index_ignores_the_shared_cache_dir(tmp_path):
    _write_cards(tmp_path, "series-a", [_card("nbu:88", 1998)])
    _write_cards(tmp_path, "_ua_coins", [_card("nbu:999", 4242)])
    index, _ = build_card_index(tmp_path)
    assert set(index) == {1998}


def test_build_card_index_narrows_to_one_series(tmp_path):
    _write_cards(tmp_path, "series-a", [_card("nbu:88", 1998)])
    _write_cards(tmp_path, "series-b", [_card("nbu:95", 1993)])
    index, _ = build_card_index(tmp_path, "series-b")
    assert set(index) == {1993}


def test_build_card_index_drops_an_id_two_series_disagree_about(tmp_path):
    # Ambiguous link back to an NBU card -- guessing which series is right
    # would attach a price history to the wrong coin.
    _write_cards(tmp_path, "series-a", [_card("nbu:88", 1998)])
    _write_cards(tmp_path, "series-b", [_card("nbu:777", 1998)])
    index, warnings = build_card_index(tmp_path)
    assert 1998 not in index
    assert any("1998" in w for w in warnings)


# ---------------------------------------------------------------------- #
# staging layout -- files are named after the NBU card, not the ua-coins row
# ---------------------------------------------------------------------- #


def test_file_stem_replaces_the_colon():
    assert file_stem("nbu:161") == "nbu_161"


def test_staging_paths_are_keyed_by_the_nbu_card(tmp_path):
    # ua-coins ids look like years -- 43 and 1998 are both coins of
    # 1999/2000 in the pilot series -- so a directory listing named after
    # them reads as nonsense. The NBU id is what the coin IS to us.
    assert prices_path(tmp_path, "nbu:161").name == "nbu_161.json"
    assert meta_path(tmp_path, "nbu:161").name == "nbu_161.meta.json"
    assert page_path(tmp_path, "nbu:161").name == "nbu_161.html"


def test_all_three_files_of_a_coin_sit_under_one_stem(tmp_path):
    stems = {
        prices_path(tmp_path, "nbu:88").stem,
        meta_path(tmp_path, "nbu:88").stem.removesuffix(".meta"),
        page_path(tmp_path, "nbu:88").stem,
    }
    assert stems == {"nbu_88"}


def _write_meta(tmp_path, source_id, ua_coins_id):
    path = meta_path(tmp_path, source_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        __import__("json").dumps({"ua_coins_id": ua_coins_id, "source_id": source_id}),
        encoding="utf-8",
    )


def test_cached_ua_coins_id_reads_the_row_the_history_came_from(tmp_path):
    _write_meta(tmp_path, "nbu:161", 43)
    assert cached_ua_coins_id(tmp_path, "nbu:161") == 43


def test_cached_ua_coins_id_is_none_without_a_meta_file(tmp_path):
    assert cached_ua_coins_id(tmp_path, "nbu:161") is None


def test_cached_ua_coins_id_survives_an_unreadable_meta(tmp_path):
    # A truncated meta must not take the whole run down; it just means
    # "cannot tell", and fetch treats that as "keep the cache".
    path = meta_path(tmp_path, "nbu:161")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert cached_ua_coins_id(tmp_path, "nbu:161") is None


def test_cached_ua_coins_id_detects_a_rematched_card(tmp_path):
    # The point of naming by NBU id: this comparison has somewhere to
    # happen. A card re-matched from ua-coins 2441 to 2449 used to write
    # a second file beside the first, both looking current.
    _write_meta(tmp_path, "nbu:161", 2441)
    assert cached_ua_coins_id(tmp_path, "nbu:161") != 2449


# ---------------------------------------------------------------------- #
# dropping the legacy uCoin history (load_prices._handle_ucoin)
#
# The SQL itself needs a real Postgres, but the rule that makes this safe
# -- only ever touching the coins whose ua-coins history this run just
# loaded -- is worth pinning down, so the statements are captured against
# a stand-in connection and asserted on.
# ---------------------------------------------------------------------- #


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    """Answers the two statements _handle_ucoin issues and records them."""

    def __init__(self, counts=None, deleted=None):
        self.counts = counts or {}
        self.deleted = deleted or {}
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if sql.lstrip().startswith("DELETE"):
            rows = [(item_id,) for item_id, n in self.deleted.items() for _ in range(n)]
            return _Result(rows)
        return _Result(list(self.counts.items()))

    @property
    def deletes(self):
        return [(s, p) for s, p in self.statements if s.lstrip().startswith("DELETE")]


def _loadable(*ids):
    reports = [
        load_prices_mod.PriceCoinReport(ua_coins_id=100 + i, source_id=f"nbu:{i}", status="loaded")
        for i in ids
    ]
    return list(zip(reports, ids)), reports


def test_ucoin_rows_are_counted_but_kept_without_the_flag():
    loadable, reports = _loadable(559, 560)
    conn = _FakeConn(counts={559: 2, 560: 1})
    summary = load_prices_mod.LoadPricesSummary(drop_ucoin=False)
    load_prices_mod._handle_ucoin(conn, loadable, summary)
    assert [r.ucoin_present for r in reports] == [2, 1]
    assert [r.ucoin_dropped for r in reports] == [0, 0]
    assert conn.deletes == []


def test_ucoin_rows_are_deleted_with_the_flag():
    loadable, reports = _loadable(559, 560)
    conn = _FakeConn(counts={559: 2, 560: 1}, deleted={559: 2, 560: 1})
    summary = load_prices_mod.LoadPricesSummary(drop_ucoin=True)
    load_prices_mod._handle_ucoin(conn, loadable, summary)
    assert [r.ucoin_dropped for r in reports] == [2, 1]
    assert len(conn.deletes) == 1


def test_only_the_coins_loaded_this_run_are_ever_touched():
    # The whole safety argument: a coin ua-coins does not quote never
    # reaches `loadable`, so its uCoin rows -- its only prices -- are not
    # in the ids the DELETE is given.
    loadable, _ = _loadable(559, 560)
    conn = _FakeConn(counts={}, deleted={})
    summary = load_prices_mod.LoadPricesSummary(drop_ucoin=True)
    load_prices_mod._handle_ucoin(conn, loadable, summary)
    _sql, params = conn.deletes[0]
    assert params["ids"] == [559, 560]
    assert params["source"] == "uCoin"
    assert "created_by IS NULL" in _sql


def test_nothing_loaded_means_no_statements_at_all():
    conn = _FakeConn()
    summary = load_prices_mod.LoadPricesSummary(drop_ucoin=True)
    load_prices_mod._handle_ucoin(conn, [], summary)
    assert conn.statements == []
