"""Telling coin_keeper what a scheduled run did.

The report goes to coin_keeper's API rather than straight into its database
(docs/13-admin.md in that repository, 2.3): the API is what records the run
AND sends the telegram message, so a run that finishes at 03:23 is in the
chat at 03:23 without anything having to poll for it.

Nothing here is allowed to change the outcome of a run. Every failure is
logged and swallowed: a night's prices are worth more than the note saying
they were collected, and the summary line still goes to the log file either
way.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

TIMEOUT_SECONDS = 10.0
PREFIX = "[job-report]"

URL_ENV = "JOB_REPORT_URL"
TOKEN_ENV = "JOB_REPORT_TOKEN"


class JobReporter:
    """One run's report: opened before the work, closed after it.

    Unconfigured (no URL or no token) it does nothing at all, which is what a
    workstation wants -- a local rehearsal has no business appearing in the
    production admin section.
    """

    def __init__(self, job: str, base_url: str | None, token: str | None) -> None:
        self._job = job
        self._base_url = (base_url or "").rstrip("/")
        self._token = token or ""
        self._run_id: int | None = None

    @classmethod
    def from_env(cls, job: str) -> JobReporter:
        return cls(job, os.environ.get(URL_ENV), os.environ.get(TOKEN_ENV))

    @property
    def enabled(self) -> bool:
        return bool(self._base_url and self._token)

    def open(self) -> None:
        if not self.enabled:
            return
        body = self._post("", {"job": self._job})
        if body is not None:
            run_id = body.get("id")
            self._run_id = run_id if isinstance(run_id, int) else None
            print(f"{PREFIX} run {self._run_id} opened")

    def finish(self, payload: dict[str, Any]) -> None:
        """Close the run. With no open run -- the API was down when the work
        started -- post the whole thing at once instead, so the report is not
        lost for want of a row to attach it to."""
        if not self.enabled:
            return
        if self._run_id is None:
            self._post("", {"job": self._job, **payload})
            return
        self._patch(f"/{self._run_id}", payload)

    def crashed(self, error: str) -> None:
        """The step raised. Cron sees the traceback; this makes sure the admin
        section and the chat do too, instead of a row stuck in 'running'."""
        self.finish(
            {
                "status": "failed",
                "summary": f"{self._job} crashed",
                "details": error[:2000],
                "exitCode": 2,
            }
        )

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        return self._request("POST", path, body)

    def _patch(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        return self._request("PATCH", path, body)

    def _request(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        url = f"{self._base_url}/internal/job-runs{path}"
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                response = client.request(
                    method, url, json=body, headers={"X-Job-Token": self._token}
                )
        except httpx.HTTPError as exc:
            print(f"{PREFIX} could not reach {url}: {exc}")
            return None
        if response.status_code >= httpx.codes.BAD_REQUEST:
            print(f"{PREFIX} {url} answered {response.status_code}: {response.text[:200]}")
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None
