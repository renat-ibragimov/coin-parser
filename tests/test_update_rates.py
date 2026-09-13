"""Pure tests for the update-rates step -- no network, no database, same
spirit as test_update_prices.py. The write path (_upsert) is exercised
manually against a real database, not here -- see docs/00_spec.md.
"""

from datetime import date

import pytest

from collector.rates import update_rates as ur


def _ok(code: str, **kw) -> ur.CurrencyReport:
    return ur.CurrencyReport(currency_code=code, status="ok", **kw)


def _error(code: str, error: str = "boom") -> ur.CurrencyReport:
    return ur.CurrencyReport(currency_code=code, status="error", error=error)


def _summary(**kw) -> ur.UpdateRatesSummary:
    kw.setdefault("run_date", date(2026, 9, 13))
    kw.setdefault("start", date(2026, 8, 30))
    kw.setdefault("end", date(2026, 9, 13))
    return ur.UpdateRatesSummary(**kw)


# ---------------------------------------------------------------------- #
# exit code / status word
# ---------------------------------------------------------------------- #


def test_both_currencies_ok_is_ok():
    summary = _summary(currencies=[_ok("USD", inserted=1), _ok("EUR", unchanged=13)])
    assert summary.exit_code == ur.EXIT_OK
    assert summary.status_word() == "ok"


def test_one_currency_failed_is_partial():
    summary = _summary(currencies=[_ok("USD", unchanged=14), _error("EUR")])
    assert summary.exit_code == ur.EXIT_PARTIAL


def test_every_currency_failed_is_nothing_done():
    summary = _summary(currencies=[_error("USD"), _error("EUR")])
    assert summary.exit_code == ur.EXIT_NOTHING_DONE


def test_no_database_is_nothing_done():
    summary = _summary(currencies=[], error="DATABASE_URL is not set")
    assert summary.exit_code == ur.EXIT_NOTHING_DONE
    assert summary.status_word() == "failed"


def test_a_quiet_night_with_nothing_new_is_still_ok():
    # Most runs repeat a window the previous run already wrote -- all
    # unchanged is success, not a failure to find anything.
    summary = _summary(currencies=[_ok("USD", fetched=14, unchanged=14), _ok("EUR", fetched=14, unchanged=14)])
    assert summary.exit_code == ur.EXIT_OK


# ---------------------------------------------------------------------- #
# summary line -- one line, same keys in the same order every time
# ---------------------------------------------------------------------- #


def test_summary_line_counts_every_bucket():
    summary = _summary(
        currencies=[
            _ok("USD", fetched=14, inserted=1, updated=2, unchanged=11),
            _ok("EUR", fetched=14, inserted=0, updated=0, unchanged=14),
        ]
    )
    assert summary.summary_line() == (
        "update-rates ok currencies=2 inserted=1 updated=2 unchanged=25 failed=0"
    )


def test_the_report_ends_with_exactly_one_summary_line(capsys):
    summary = _summary(currencies=[_ok("USD", fetched=1, inserted=1), _ok("EUR", fetched=1, inserted=1)])
    summary.print_report()
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == summary.summary_line()
    assert sum(1 for line in out if line.startswith("update-rates ")) == 1


# ---------------------------------------------------------------------- #
# report payload -- what coin_keeper's admin section would store
# ---------------------------------------------------------------------- #


def test_report_payload_carries_the_counters():
    summary = _summary(currencies=[_ok("USD", fetched=1, inserted=1), _ok("EUR", fetched=1, updated=1)])
    payload = summary.report_payload()
    assert payload["status"] == "ok"
    assert payload["stats"]["inserted"] == 1
    assert payload["stats"]["updated"] == 1
    assert payload["details"] is None  # a good run explains nothing


def test_report_payload_explains_a_bad_run():
    summary = _summary(currencies=[_ok("USD", fetched=1, inserted=1), _error("EUR", "timeout")])
    payload = summary.report_payload()
    assert payload["status"] == "partial"
    assert "EUR: timeout" in payload["details"]


def test_report_payload_of_a_run_that_never_started():
    summary = _summary(currencies=[], error="DATABASE_URL is not set")
    payload = summary.report_payload()
    assert payload["status"] == "failed"
    assert payload["details"] == "DATABASE_URL is not set"


# ---------------------------------------------------------------------- #
# the top-level function's one no-network path
# ---------------------------------------------------------------------- #


def test_no_database_url_is_nothing_done_end_to_end(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    summary = ur.update_rates(today=date(2026, 9, 13))
    assert summary.exit_code == ur.EXIT_NOTHING_DONE
    assert summary.currencies == []
    assert summary.start == date(2026, 8, 30)  # today - DEFAULT_WINDOW_DAYS
    assert summary.end == date(2026, 9, 13)


def test_default_window_is_fourteen_days_ending_today():
    assert ur.DEFAULT_WINDOW_DAYS == 14
