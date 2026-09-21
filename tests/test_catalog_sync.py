from collector.countries.ua.catalog_sync import (
    CatalogSyncSummary,
    _series_by_id,
    _unknown_idless_titles,
)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_args, **_kwargs):
        return _Result(self.rows)


def test_recent_page_keeps_series_and_reports_cards_without_an_id():
    html = """
    <div class="search-result">
      <div class="tag">Флора і фауна України</div>
      <div class="title">Зубр</div>
      <div class="img-container"><img src="/media/coins/1734/avers.jpg"></div>
    </div>
    <div class="search-result"><div class="title">Ще без фото</div></div>
    """
    assert _series_by_id(html) == (
        {"1734": "Флора і фауна України"},
        ["Ще без фото"],
    )


def test_idless_card_with_normalized_title_match_is_not_a_daily_warning():
    conn = _Connection(
        [('Набір із двох монет "Мешканці морських глибин" у сувенірному пакованні', None)]
    )
    incoming = ['Набір із двох монет “Мешканці морських глибин” у сувенірному пакованні']
    assert _unknown_idless_titles(conn, incoming) == []


def test_summary_marks_warnings_partial():
    summary = CatalogSyncSummary(scanned=25, known=23, new_ids=["nbu:1771"], warnings=["x"])
    payload = summary.report_payload()
    assert summary.exit_code == 1
    assert payload["status"] == "partial"
    assert payload["stats"]["new"] == 1
