# coin-parser — agent guide

Canonical instructions for every coding agent in this repository (Codex reads this file
natively; Claude Code reads it through `CLAUDE.md`).

## What this is

The data collector for **Bakost Numismatics** (`coin_keeper` repository, the web app).
It scrapes official and market sources, normalizes coins, processes photos, and writes
straight into `coin_keeper`'s production database and object storage. It has no API or
UI of its own.

| Mode | Where | What |
|---|---|---|
| `update-prices` | server cron, daily | UA-Coins quotes for `nbu:*` coins → `market_price_snapshots` |
| `rates` | server cron, every 5 h | NBU USD/EUR → `exchange_rates` |
| `update-catalog` | server cron, daily | newest NBU cards → new issues as `status = 'draft'` for admin review |
| series work (`--step fetch/parse/match/…/load-*`) | workstation, by hand | building a country's catalog series by series |

Every scheduled run reports to `coin_keeper`'s API (`collector/core/job_report.py`).
The contract on the app side: `coin_keeper/docs/integrations.md` (who does what, loader
rules, price validation) and `coin_keeper/docs/admin.md` ("Job runs", "Watchdog").

## Layout

```
collector/__main__.py        CLI — every step and its preconditions in the docstring
collector/core/              shared: db, object storage, staging cache, photos, job report
collector/countries/ua/      Ukraine adapter: NBU catalog, UA-Coins, loaders, catalog sync
collector/rates/             NBU exchange rates
deploy/                      crontab, run-*.sh, Dockerfile, compose file for the server
docs/                        00_spec (design), 01_findings (research log),
                             02_series_artifacts, 03_series_playbook (production runbook)
staging/, current_ref/       local only, git-ignored
```

`docs/` is still in Russian and partly a research log — treat it as history pending
translation. References to `coin_keeper`'s `app/ukraine_pipeline/…` point at code that
repository has since deleted; they explain where values in the database came from.

## Commands

```bash
.venv/bin/pytest -q
pip install -e ".[photos,dev]"          # the photo pipeline needs the heavy extras
python -m collector ua --step …          # see collector/__main__.py
```

## Rules

- **Writes go to production.** Every `load-*` step, and any run with `DATABASE_URL`
  pointing at the server, changes the live app. Follow `docs/03_series_playbook.md` and
  get explicit approval first.
- **Never touch `collection_items`, `expenses` or personal positions** (`created_by IS NOT
  NULL`). The collector writes the shared catalog, snapshots and rates only.
- **Never delete shared-catalog rows**; new issues go in as drafts for review.
- **Validate prices before writing** (`coin_keeper/docs/integrations.md`,
  "Price validation"); a rejected price is logged, not written.
- **Fixtures only from live pages.** Save the real HTML to disk and test against it;
  two parser bugs in a row came from hand-written HTML.
- **Raw first, parse second.** Source responses are cached in `staging/`; re-parse the
  cache instead of re-fetching.
- **Everything in the repository is English** — code, comments, logs, commits, and new
  docs. Coin names keep their original language (data).
- **Cite `coin_keeper` docs by name**: `coin_keeper/docs/<file>.md, "Heading"` or
  `BR-N` — never section numbers.

## Coding behavior

### Think before coding
- State assumptions explicitly. If uncertain, ask — do not guess and proceed.
- If multiple interpretations exist, present them. Do not pick silently.
- If something is unclear, stop. Name what is confusing. Ask for clarification.
- If a simpler approach exists, say so and push back.

### Simplicity first
- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that was not requested.
- If 200 lines could be 50, rewrite it.

### Surgical changes
- Do not "improve" adjacent code, comments, or formatting.
- Do not refactor things that are not broken.
- Match existing style, even if you would do it differently.
- If you notice unrelated dead code, mention it — do not delete it.
- Every changed line must trace directly to the user's request.

### Goal-driven execution
For any task that changes more than 2 files or contains more than one logical unit of
change — before writing any code: read all affected files, output a numbered plan (one
logical unit per step), stop and wait for approval before step 1, and stop again after
each step. Simple isolated changes proceed directly.

## Permissions

Read-only operations (grep, cat, find, ls, read, git log/diff, tests) need no approval.
Always stop and wait for explicit approval before: modifying or creating any file,
running any command that writes to disk outside `staging/`, anything touching a
database, Redis or the object storage, any server command, and every commit or push.
