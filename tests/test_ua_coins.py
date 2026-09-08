from collector.countries.ua.ua_coins import UaCoinsRow, match_cards, parse_year


def _table_html(rows_html: str) -> str:
    return f"""
    <table class="col-md-12 table-bordered table-striped table-sm cf coin-list">
      <thead class="cf"><tr class="tr_head">
        <td class="text-center">Дата</td><td class="text-center">Номінал</td>
        <td class="text-center">Тираж тис.</td><td>Назва</td>
        <td class="text-center">Вартість 08.09.2026</td>
      </tr></thead>
      <tr><td colspan="5" class="ua-table-year-cell ua-table-year-cell--flush">
        <a href="/ua/catalog/all/1999">1999</a>
      </td></tr>
      {rows_html}
    </table>
    """


def _row_html(href: str, title: str, denom: str) -> str:
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
      <td class="ua-table-price-cell" data-title="Вартість 08.09.2026"><a href="{href}">7568</a></td>
    </tr>
    """


# ---------------------------------------------------------------------- #
# parse_year -- HTML fixtures, no network
# ---------------------------------------------------------------------- #


def test_parse_year_extracts_row_fields():
    html = _table_html(_row_html("/ua/list/1999-rizdvo-khrystove", "Різдво Христове", "10 грн."))
    rows = parse_year(html, 1999)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == 1999
    assert row.url == "https://www.ua-coins.info/ua/list/1999-rizdvo-khrystove"
    assert row.title == "Різдво Христове"
    assert row.year == 1999
    assert row.denomination == 10.0


def test_parse_year_skips_header_and_year_separator_rows():
    # _table_html already includes a thead row and a year-separator row
    # with no data-title="Назва" cell -- only the real coin row should
    # come back.
    html = _table_html(_row_html("/ua/list/43-rizdvo-khrystove", "Різдво Христове", "5 грн."))
    rows = parse_year(html, 1999)
    assert len(rows) == 1


def test_parse_year_denomination_with_trailing_note():
    # Real ua-coins markup: some cells have "5 грн.\n(біметал)" -- only
    # the leading number should be parsed.
    html = _table_html(
        _row_html("/ua/list/99-test", "Тестова монета", "5 грн.\n                (біметал)")
    )
    rows = parse_year(html, 2020)
    assert rows[0].denomination == 5.0


def test_parse_year_returns_empty_list_when_table_missing():
    assert parse_year("<html><body>no table here</body></html>", 1999) == []


def test_parse_year_multiple_rows():
    html = _table_html(
        _row_html("/ua/list/1999-rizdvo-khrystove", "Різдво Христове", "10 грн.")
        + _row_html("/ua/list/1998-rizdvo-khrystove", "Різдво Христовe", "50 грн.")
        + _row_html("/ua/list/43-rizdvo-khrystove", "Різдво Христове", "5 грн.")
    )
    rows = parse_year(html, 1999)
    assert len(rows) == 3
    assert {r.id for r in rows} == {1999, 1998, 43}


# ---------------------------------------------------------------------- #
# match_cards -- pure logic on already-parsed rows, no HTML/network
# ---------------------------------------------------------------------- #


def _row(id_, title, year, denom):
    from collector.countries.ua.normalize import normalize_match

    return UaCoinsRow(
        id=id_,
        url=f"https://www.ua-coins.info/ua/list/{id_}-slug",
        title=title,
        title_key=normalize_match(title),
        year=year,
        denomination=denom,
    )


def _card(source_id, title_uk, year, denom, quality_raw=None):
    return {
        "source_id": source_id,
        "titles": {"uk": title_uk},
        "year": year,
        "denomination": {"value": denom},
        "quality_raw": quality_raw,
    }


def test_match_cards_exact_match():
    cards = [_card("nbu:89", "Різдво Христове", 1999, 10)]
    rows_by_year = {1999: [_row(1999, "Різдво Христове", 1999, 10.0)]}
    cards, unmatched = match_cards(cards, rows_by_year, "2026-01-01T00:00:00+00:00")
    assert unmatched == []
    assert cards[0]["ua_coins"]["id"] == 1999
    assert cards[0]["ua_coins"]["matched_by"] == "exact"
    assert cards[0]["ua_coins"]["year_used"] == 1999
    assert cards[0]["ua_coins"]["url"] == "https://www.ua-coins.info/ua/list/1999-slug"


def test_match_cards_year_shift_when_exact_year_empty():
    # NBU circulation year 2000, but ua-coins only lists it under 1999.
    cards = [_card("nbu:96", "Хрещення Русі", 2000, 10)]
    rows_by_year = {
        1999: [_row(1992, "Хрещення Русі", 1999, 10.0)],
        2000: [],
        2001: [],
    }
    cards, unmatched = match_cards(cards, rows_by_year, "2026-01-01T00:00:00+00:00")
    assert unmatched == []
    assert cards[0]["ua_coins"]["matched_by"] == "year_shift"
    assert cards[0]["ua_coins"]["year_used"] == 1999
    assert cards[0]["ua_coins"]["id"] == 1992


def test_match_cards_title_startswith_rule():
    # ua-coins has a clarifying suffix NBU's title doesn't.
    cards = [_card("nbu:1", "Соня садова", 1999, 10)]
    rows_by_year = {1999: [_row(1, "Соня садова присвячена флорі", 1999, 10.0)]}
    cards, unmatched = match_cards(cards, rows_by_year, "2026-01-01T00:00:00+00:00")
    assert unmatched == []
    assert cards[0]["ua_coins"]["id"] == 1


def test_match_cards_conflict_two_candidates_same_year():
    cards = [_card("nbu:1", "Соня садова", 1999, 10)]
    rows_by_year = {
        1999: [
            _row(1, "Соня садова", 1999, 10.0),
            _row(2, "Соня садова", 1999, 10.0),
        ]
    }
    cards, unmatched = match_cards(cards, rows_by_year, "2026-01-01T00:00:00+00:00")
    assert cards[0]["ua_coins"] is None
    assert len(unmatched) == 1
    assert unmatched[0]["reason"].startswith("conflict")
    assert len(unmatched[0]["candidates"]) == 2


def test_match_cards_conflict_row_claimed_by_two_cards():
    # Two different NBU cards both uniquely resolve to the SAME ua-coins
    # row -- must not silently pick one.
    cards = [
        _card("nbu:1", "Соня садова", 1999, 10),
        _card("nbu:2", "Соня садова", 1999, 10),
    ]
    rows_by_year = {1999: [_row(1, "Соня садова", 1999, 10.0)]}
    cards, unmatched = match_cards(cards, rows_by_year, "2026-01-01T00:00:00+00:00")
    assert cards[0]["ua_coins"] is None
    assert cards[1]["ua_coins"] is None
    assert len(unmatched) == 2
    reasons = {e["source_id"]: e for e in unmatched}
    assert reasons["nbu:1"]["conflict_with"] == ["nbu:2"]
    assert reasons["nbu:2"]["conflict_with"] == ["nbu:1"]


def test_match_cards_unmatched_reports_candidates_seen():
    cards = [_card("nbu:1", "Неіснуюча Монета", 1999, 10)]
    rows_by_year = {
        1998: [_row(1, "Інша Монета А", 1998, 10.0)],
        1999: [_row(2, "Інша Монета Б", 1999, 10.0)],
        2000: [_row(3, "Інша Монета В", 2000, 10.0)],
    }
    cards, unmatched = match_cards(cards, rows_by_year, "2026-01-01T00:00:00+00:00")
    assert cards[0]["ua_coins"] is None
    assert len(unmatched) == 1
    seen = unmatched[0]["candidates_seen"]
    assert set(seen) == {"Інша Монета А", "Інша Монета Б", "Інша Монета В"}


def test_match_cards_never_matches_wrong_denomination():
    cards = [_card("nbu:1", "Соня садова", 1999, 10)]
    rows_by_year = {1999: [_row(1, "Соня садова", 1999, 2.0)]}
    cards, unmatched = match_cards(cards, rows_by_year, "2026-01-01T00:00:00+00:00")
    assert cards[0]["ua_coins"] is None
    assert len(unmatched) == 1


def test_match_cards_missing_year_or_denomination_is_unmatched_not_crash():
    cards = [{"source_id": "nbu:1", "titles": {"uk": "X"}, "year": None, "denomination": {"value": None}}]
    cards, unmatched = match_cards(cards, {}, "2026-01-01T00:00:00+00:00")
    assert cards[0]["ua_coins"] is None
    assert len(unmatched) == 1


# ---------------------------------------------------------------------- #
# quality-suffix disambiguation -- real case: nbu:64/nbu:229, both
# "100 років Київському політехнічному інституту", 2 hryvnia, 1998,
# ua-coins ids 28/29 differing only by "(анциркулейтед)"/"(звичайна)"
# ---------------------------------------------------------------------- #


def test_match_cards_disambiguates_quality_suffix_conflict():
    cards = [
        _card("nbu:64", "100 років Київському політехнічному інституту", 1998, 2, quality_raw="анциркулейтед"),
        _card("nbu:229", "100 років Київському політехнічному інституту", 1998, 2, quality_raw="звичайна"),
    ]
    rows_by_year = {
        1998: [
            _row(28, "100 років Київському політехнічному інституту (анциркулейтед)", 1998, 2.0),
            _row(29, "100 років Київському політехнічному інституту (звичайна)", 1998, 2.0),
        ]
    }
    cards, unmatched = match_cards(cards, rows_by_year, "2026-01-01T00:00:00+00:00")
    assert unmatched == []
    by_id = {c["source_id"]: c["ua_coins"] for c in cards}
    assert by_id["nbu:64"]["id"] == 28
    assert by_id["nbu:229"]["id"] == 29


def test_match_cards_quality_disambiguation_does_not_fire_without_card_quality():
    # No quality_raw on the card -- nothing to disambiguate with, stays
    # a conflict rather than guessing.
    cards = [_card("nbu:64", "100 років Київському політехнічному інституту", 1998, 2)]
    rows_by_year = {
        1998: [
            _row(28, "100 років Київському політехнічному інституту (анциркулейтед)", 1998, 2.0),
            _row(29, "100 років Київському політехнічному інституту (звичайна)", 1998, 2.0),
        ]
    }
    cards, unmatched = match_cards(cards, rows_by_year, "2026-01-01T00:00:00+00:00")
    assert cards[0]["ua_coins"] is None
    assert unmatched[0]["reason"].startswith("conflict")


def test_match_cards_quality_disambiguation_does_not_fire_on_genuine_name_parens():
    # "(богиня Апі)" is part of the real coin name, not a quality
    # variant marker -- there's only ever one candidate for this title in
    # the first place, so the disambiguation path never even runs, and
    # the parens stay part of the matched title as-is.
    cards = [_card("nbu:372", "Скіфське золото (богиня Апі)", 2018, 2, quality_raw="пруф")]
    rows_by_year = {2018: [_row(1883, "Скіфське золото (богиня Апі)", 2018, 2.0)]}
    cards, unmatched = match_cards(cards, rows_by_year, "2026-01-01T00:00:00+00:00")
    assert unmatched == []
    assert cards[0]["ua_coins"]["id"] == 1883


def test_match_cards_quality_disambiguation_no_unique_hint_stays_conflict():
    # Both candidates carry the SAME quality hint (or neither matches the
    # card's) -- must not guess between them.
    cards = [_card("nbu:1", "Тестова монета", 2000, 5, quality_raw="пруф")]
    rows_by_year = {
        2000: [
            _row(1, "Тестова монета (анциркулейтед)", 2000, 5.0),
            _row(2, "Тестова монета (звичайна)", 2000, 5.0),
        ]
    }
    cards, unmatched = match_cards(cards, rows_by_year, "2026-01-01T00:00:00+00:00")
    assert cards[0]["ua_coins"] is None
    assert unmatched[0]["reason"].startswith("conflict")
