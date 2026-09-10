"""Reporting a run to coin_keeper.

The rule these are here to hold: reporting never changes what a run does.
An API that is down, slow or answering nonsense costs a log line and nothing
else -- the prices are already in the database by then, and the summary line
still goes to the log file.
"""

from __future__ import annotations

import httpx
import pytest

from collector.core.job_report import JobReporter


class FakeClient:
    """Stands in for httpx.Client, recording what would have been sent."""

    def __init__(self, responses, calls, error=None):
        self._responses = list(responses)
        self._calls = calls
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def request(self, method, url, json=None, headers=None):
        self._calls.append({"method": method, "url": url, "json": json, "headers": headers})
        if self._error is not None:
            raise self._error
        return self._responses.pop(0)


def response(status_code=200, payload=None):
    return httpx.Response(
        status_code, json=payload if payload is not None else {}, request=httpx.Request("POST", "http://api")
    )


@pytest.fixture
def calls():
    return []


def patch_client(monkeypatch, calls, responses=(), error=None):
    monkeypatch.setattr(
        "collector.core.job_report.httpx.Client",
        lambda **_: FakeClient(responses, calls, error),
    )


def reporter() -> JobReporter:
    return JobReporter("update-prices", "http://api:8000/api/v1", "secret")


def test_unconfigured_reporter_does_nothing(monkeypatch, calls):
    patch_client(monkeypatch, calls)
    quiet = JobReporter("update-prices", None, None)

    quiet.open()
    quiet.finish({"status": "ok"})

    assert calls == [], "a workstation run has no business in the production admin section"


def test_open_then_finish(monkeypatch, calls):
    patch_client(monkeypatch, calls, responses=[response(201, {"id": 7}), response(200, {"id": 7})])
    job = reporter()

    job.open()
    job.finish({"status": "ok", "summary": "update-prices ok"})

    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://api:8000/api/v1/internal/job-runs"
    assert calls[0]["headers"] == {"X-Job-Token": "secret"}
    assert calls[1]["method"] == "PATCH"
    assert calls[1]["url"].endswith("/internal/job-runs/7")
    assert calls[1]["json"]["status"] == "ok"


def test_a_run_that_could_not_be_opened_is_posted_whole(monkeypatch, calls):
    """The API was down at 03:15 but back by 03:23. The report still lands."""
    patch_client(monkeypatch, calls, responses=[response(503), response(201, {"id": 9})])
    job = reporter()

    job.open()
    job.finish({"status": "ok", "summary": "update-prices ok"})

    assert [call["method"] for call in calls] == ["POST", "POST"]
    assert calls[1]["json"]["job"] == "update-prices"
    assert calls[1]["json"]["status"] == "ok"


def test_an_unreachable_api_is_survivable(monkeypatch, calls):
    patch_client(monkeypatch, calls, error=httpx.ConnectError("no route to host"))
    job = reporter()

    job.open()
    job.finish({"status": "ok"})  # must not raise

    assert len(calls) == 2


def test_a_crash_is_reported_as_failed(monkeypatch, calls):
    patch_client(monkeypatch, calls, responses=[response(201, {"id": 3}), response(200, {})])
    job = reporter()
    job.open()

    job.crashed("RuntimeError: boom")

    assert calls[1]["json"]["status"] == "failed"
    assert "boom" in calls[1]["json"]["details"]
