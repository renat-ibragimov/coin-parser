"""Pure decision-logic tests for load_series.py -- no database. The SQL
execution functions (_select_current/_execute_update/_execute_insert)
and the transaction/connection wiring in load_series() itself are
deliberately NOT covered here: they need a real Postgres to test against
honestly, and this repo doesn't have one wired up. build_series_values()
and diff_row() are where the actual field-mapping/idempotency logic
lives, and both are fully exercised without touching a database.
"""

from collector.countries.ua.load_series import build_series_values, diff_row


def _entry(uk, en=None, year_range=None, missing_from_nbu=False):
    e = {"names": {"uk": uk, "en": en}, "year_range": year_range}
    if missing_from_nbu:
        e["missing_from_nbu"] = True
    return e


# ---------------------------------------------------------------------- #
# build_series_values
# ---------------------------------------------------------------------- #


def test_build_series_values_basic_fields():
    entry = _entry("2000-ліття Різдва Христового", "2000 Years of Christmas", [1999, 2000])
    values = build_series_values(entry, has_is_official=True)
    assert values == {
        "name_original": "2000-ліття Різдва Христового",
        "name_uk": "2000-ліття Різдва Христового",
        "name_uk_source": "official",
        "original_lang": "uk",
        "name_en": "2000 Years of Christmas",
        "name_en_source": "official",
        "start_year": 1999,
        "end_year": 2000,
        "is_official": True,
    }


def test_build_series_values_trims_whitespace():
    # Real case: series.json stores "My Immortal Ukraine " byte-exact
    # (NBU's own serie[] filter needs the trailing space) -- the DB
    # should get a human name, not a protocol detail.
    entry = _entry("Безсмертна моя Україно", "My Immortal Ukraine ", [2022, 2026])
    values = build_series_values(entry, has_is_official=True)
    assert values["name_original"] == "Безсмертна моя Україно"
    assert values["name_en"] == "My Immortal Ukraine"


def test_build_series_values_no_en_name_omits_en_columns():
    entry = _entry("Відродження християнської духовності в Україні", None, [2008, 2010])
    values = build_series_values(entry, has_is_official=True)
    assert "name_en" not in values
    assert "name_en_source" not in values


def test_build_series_values_no_year_range_omits_year_columns():
    entry = _entry("Тест", "Test", None)
    values = build_series_values(entry, has_is_official=True)
    assert "start_year" not in values
    assert "end_year" not in values


def test_build_series_values_is_official_omitted_when_column_missing():
    entry = _entry("Тест", "Test", [2000, 2000])
    values = build_series_values(entry, has_is_official=False)
    assert "is_official" not in values


# ---------------------------------------------------------------------- #
# diff_row
# ---------------------------------------------------------------------- #


def test_diff_row_no_changes():
    current = {"name_uk": "Тест", "start_year": 2000}
    intended = {"name_uk": "Тест", "start_year": 2000}
    assert diff_row(current, intended) == {}


def test_diff_row_reports_only_changed_columns():
    current = {"name_uk": "Old", "start_year": 2000, "end_year": 2000}
    intended = {"name_uk": "New", "start_year": 2000, "end_year": 2001}
    changes = diff_row(current, intended)
    assert changes == {"name_uk": ("Old", "New"), "end_year": (2000, 2001)}


def test_diff_row_ignores_columns_not_in_intended():
    # A column present in `current` but never part of `intended` (e.g.
    # name_en when the series has no en name) must never show as a
    # "change" -- it isn't being touched.
    current = {"name_uk": "Тест", "name_en": "some llm translation"}
    intended = {"name_uk": "Тест"}
    assert diff_row(current, intended) == {}


def test_diff_row_detects_new_value_where_current_was_none():
    current = {"name_en": None}
    intended = {"name_en": "Official Name"}
    assert diff_row(current, intended) == {"name_en": (None, "Official Name")}


# ---------------------------------------------------------------------- #
# idempotency, end to end on the pure functions: build -> diff against
# the same values should always be empty on a "second run"
# ---------------------------------------------------------------------- #


def test_second_run_is_a_no_op():
    entry = _entry("2000-ліття Різдва Христового", "2000 Years of Christmas", [1999, 2000])
    values = build_series_values(entry, has_is_official=True)
    # Simulate the row now holding exactly what the first run wrote.
    current_row = dict(values)
    assert diff_row(current_row, values) == {}
