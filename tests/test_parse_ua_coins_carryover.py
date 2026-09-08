"""parse() must not discard ua_coins blocks a prior match_ua_coins() run
wrote -- regenerating cards.json from raw/ is supposed to be safe to
rerun any time, and match results are the one thing parse() itself never
produces, only carries forward. See docs/01_findings.md.
"""

from collector.countries.ua.parser import ParserUkraine

_CARD_HTML = """
<div class="row cols search-result">
  <div class="col-md-10 col-1"><div class="box row">
    <div class="col-md-4"><div class="img-container">
      <div class="img"><img src="/media/coins/{id}/avers.jpg?v=1" /></div>
      <div class="img"><img src="/media/coins/{id}/revers.jpg?v=1" /></div>
    </div></div>
    <div class="col-md-8">
      <div class="title">{title}</div>
      <div class="close-lines">
        <div><span class="mark">Номінал:</span><span class="mark-text"> {denom} грн</span></div>
        <div><span class="mark">Дата введення в обіг:</span><span class="mark-text"> {date}</span></div>
        <div><span class="mark">Матеріал:</span><span class="mark-text"> срібло</span></div>
      </div>
      <div class="details hidden"><div class="description">
        <div class="description__text">Опис.</div>
      </div><div class="row">
        <div class="col-md-6 close-lines">
          <div><span class="mark">Художник:</span><span class="mark-text"> Хтось</span></div>
          <div><span class="mark">Тираж (оголошений/фактичний), шт.:</span><span class="mark-text"> 100/100</span></div>
        </div>
        <div class="col-md-6 close-lines">
          <div><span class="mark">Діаметр, мм:</span><span class="mark-text"> 10</span></div>
          <div><span class="mark">Категорія якості карбування:</span><span class="mark-text"> пруф</span></div>
          <div><span class="mark">Гурт:</span><span class="mark-text"> гладкий</span></div>
        </div>
      </div></div>
    </div>
  </div></div>
</div>
"""


def test_parse_carries_forward_existing_ua_coins_block(tmp_path):
    parser = ParserUkraine(series="Тестова серія", staging_root=tmp_path)
    html = _CARD_HTML.format(id=100, title="Тестова монета", denom=10, date="01.01.2000")
    parser.staging.write_raw("uk_p1.html", html)
    parser.staging.write_raw("en_p1.html", html)

    parser.parse()
    data = parser.staging.read_parsed()
    assert data["cards"][0]["ua_coins"] is None

    # Simulate a successful match_ua_coins() run by hand-editing cards.json
    # the same way match_ua_coins() itself would.
    match_block = {
        "id": 999,
        "url": "https://www.ua-coins.info/ua/list/999-test",
        "matched_by": "exact",
        "year_used": 2000,
        "matched_at": "2026-01-01T00:00:00+00:00",
    }
    data["cards"][0]["ua_coins"] = match_block
    parser.staging.write_parsed(data)

    # Reparse from the same raw/ -- must not be lost.
    parser.parse()
    data_after = parser.staging.read_parsed()
    assert data_after["cards"][0]["ua_coins"] == match_block


def test_parse_carries_forward_null_ua_coins_too():
    # An explicit "already tried, no match" null must survive a reparse
    # just like a real match does -- otherwise every reparse would look
    # like an untried card again.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        parser = ParserUkraine(series="Тестова серія 2", staging_root=Path(tmp))
        html = _CARD_HTML.format(id=101, title="Інша монета", denom=5, date="01.01.2000")
        parser.staging.write_raw("uk_p1.html", html)
        parser.staging.write_raw("en_p1.html", html)

        parser.parse()
        data = parser.staging.read_parsed()
        data["cards"][0]["ua_coins"] = None  # explicit "tried, no match"
        parser.staging.write_parsed(data)

        parser.parse()
        data_after = parser.staging.read_parsed()
        assert "ua_coins" in data_after["cards"][0]
        assert data_after["cards"][0]["ua_coins"] is None


def test_parse_new_card_with_no_prior_history_gets_null_ua_coins(tmp_path):
    parser = ParserUkraine(series="Тестова серія 3", staging_root=tmp_path)
    html = _CARD_HTML.format(id=102, title="Нова монета", denom=5, date="01.01.2000")
    parser.staging.write_raw("uk_p1.html", html)
    parser.staging.write_raw("en_p1.html", html)

    parser.parse()
    data = parser.staging.read_parsed()
    assert "ua_coins" in data["cards"][0]
    assert data["cards"][0]["ua_coins"] is None
