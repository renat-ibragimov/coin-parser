"""Loads official series from countries/ua/series.json into coin_keeper's
coin_series table. The first (and so far only) step in this repo that
writes to the production database -- everything else only touches local
staging/ and this repo's own committed JSON files.

Series correspondence is a plain, hand-verified lookup table
(db_map.json), never name-matching -- see the module docstring intent in
docs/01_findings.md around series.json for why matching by name text is
never trusted for anything in this adapter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from collector.core.db import existing_columns, get_database_url
from collector.countries.ua.series import SERIES_JSON_PATH

DB_MAP_PATH = Path(__file__).parent / "db_map.json"
TABLE = "coin_series"

# coin_keeper's countries table id for Ukraine. Hardcoded because it's an
# adapter-level constant (this whole adapter only ever writes Ukraine's
# rows), not something derived from any NBU data.
COUNTRY_ID = 2

REQUIRED_COLUMNS = [
    "id",
    "country_id",
    "name_original",
    "name_uk",
    "name_uk_source",
    "name_en",
    "name_en_source",
    "original_lang",
    "start_year",
    "end_year",
]
# Present in a coin_keeper deploy only after its own migration lands;
# writing it is best-effort, never a hard requirement of this step.
OPTIONAL_COLUMNS = ["is_official"]


# ---------------------------------------------------------------------- #
# db_map.json IO
# ---------------------------------------------------------------------- #


def load_db_map(path: Path = DB_MAP_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_db_map(data: dict, path: Path = DB_MAP_PATH) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------- #
# pure decision logic -- no SQL, no network, fully unit-testable
# ---------------------------------------------------------------------- #


def build_series_values(entry: dict, has_is_official: bool) -> dict:
    """The {column: value} this series.json entry should have in
    coin_series, per the fixed field mapping. Only includes columns that
    should actually be written -- a None/absent en name or year_range
    means "don't touch that column", so it's simply left out here rather
    than written as NULL.
    """
    names = entry.get("names", {})
    uk_name = (names.get("uk") or "").strip()

    values: dict = {
        "name_original": uk_name,
        "name_uk": uk_name,
        "name_uk_source": "official",
        "original_lang": "uk",
    }

    en_name = names.get("en")
    if en_name is not None:
        values["name_en"] = en_name.strip()
        values["name_en_source"] = "official"

    year_range = entry.get("year_range")
    if year_range:
        values["start_year"] = year_range[0]
        values["end_year"] = year_range[1]

    if has_is_official:
        values["is_official"] = True

    return values


def diff_row(current: dict, intended: dict) -> dict[str, tuple]:
    """{column: (old, new)} for every column in `intended` whose value
    differs from `current`. Columns not present in `intended` (see
    build_series_values) are never compared -- they're not being
    touched, so they can't be "changed"."""
    changes = {}
    for col, new_val in intended.items():
        old_val = current.get(col)
        if old_val != new_val:
            changes[col] = (old_val, new_val)
    return changes


# ---------------------------------------------------------------------- #
# reporting
# ---------------------------------------------------------------------- #


@dataclass
class SeriesRowReport:
    slug: str
    db_id: int | None
    action: str  # "update" | "insert" | "unchanged" | "skip"
    changes: dict[str, tuple] = field(default_factory=dict)
    note: str | None = None


@dataclass
class LoadSeriesSummary:
    rows: list[SeriesRowReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    row_count_before: int | None = None
    row_count_after: int | None = None
    db_map_updated: bool = False
    committed: bool = False
    error: str | None = None

    def print_report(self) -> None:
        print("[load-series] coin_series in coin_keeper")
        if self.error:
            print(f"[load-series]   ERROR: {self.error}")
            if self.row_count_before is not None:
                print("[load-series]   transaction rolled back, nothing written")
            else:
                print("[load-series]   nothing written")
            return

        if self.rows:
            print(f"[load-series]   {'slug':<45} {'db_id':<7} {'action':<10} changes")
            for r in self.rows:
                if r.action == "unchanged":
                    changed = ""
                elif r.action == "skip":
                    changed = r.note or ""
                else:
                    changed = ", ".join(
                        f"{col}: {old!r} -> {new!r}" for col, (old, new) in r.changes.items()
                    )
                    if r.note:
                        changed = f"{changed} ({r.note})" if changed else r.note
                print(
                    f"[load-series]   {r.slug:<45} {str(r.db_id or '-'):<7} {r.action:<10} {changed}"
                )

        for w in self.warnings:
            print(f"[load-series]   WARNING: {w}")

        updated = sum(1 for r in self.rows if r.action == "update")
        inserted = sum(1 for r in self.rows if r.action == "insert")
        unchanged = sum(1 for r in self.rows if r.action == "unchanged")
        skipped = sum(1 for r in self.rows if r.action == "skip")
        print(
            f"[load-series] updated {updated}, inserted {inserted}, "
            f"unchanged {unchanged}, skipped {skipped}, warnings {len(self.warnings)}"
        )
        if self.row_count_before is not None:
            print(
                f"[load-series] coin_series row count: {self.row_count_before} -> "
                f"{self.row_count_after} (curated/other-country rows untouched)"
            )
        if self.db_map_updated:
            print("[load-series] db_map.json updated with new id(s) -- commit it")
        if self.committed:
            print(
                "[load-series] committed. Rerun with no source changes to verify "
                "idempotency -- it should report updated 0."
            )


# ---------------------------------------------------------------------- #
# SQL execution
# ---------------------------------------------------------------------- #


def _select_current(conn: psycopg.Connection, db_id: int, columns: list[str]) -> dict | None:
    col_clause = ", ".join(columns)
    row = conn.execute(
        f"SELECT {col_clause} FROM {TABLE} WHERE id = %(id)s AND country_id = %(country_id)s",
        {"id": db_id, "country_id": COUNTRY_ID},
    ).fetchone()
    if row is None:
        return None
    return dict(zip(columns, row))


def _execute_update(conn: psycopg.Connection, db_id: int, values: dict) -> None:
    # `values` keys only ever come from build_series_values(), i.e. our
    # own fixed REQUIRED_COLUMNS/OPTIONAL_COLUMNS constants -- never
    # external input -- so f-string column names here carry no
    # injection risk; only the VALUES are parameterized.
    set_clause = ", ".join(f"{col} = %({col})s" for col in values)
    conn.execute(
        f"UPDATE {TABLE} SET {set_clause} WHERE id = %(id)s AND country_id = %(country_id)s",
        {**values, "id": db_id, "country_id": COUNTRY_ID},
    )


def _execute_insert(conn: psycopg.Connection, values: dict) -> int:
    all_values = {**values, "country_id": COUNTRY_ID}
    cols = list(all_values.keys())
    col_clause = ", ".join(cols)
    placeholder_clause = ", ".join(f"%({c})s" for c in cols)
    row = conn.execute(
        f"INSERT INTO {TABLE} ({col_clause}) VALUES ({placeholder_clause}) RETURNING id",
        all_values,
    ).fetchone()
    return row[0]


# ---------------------------------------------------------------------- #
# entry point
# ---------------------------------------------------------------------- #


def _run_transaction(
    conn: psycopg.Connection, official: list[dict], db_map: dict[str, int], summary: LoadSeriesSummary
) -> bool:
    """Everything inside conn.transaction() -- commits on normal return,
    rolls back if it raises. Returns whether db_map was changed (new
    series inserted)."""
    cols = existing_columns(conn, TABLE)
    missing_required = [c for c in REQUIRED_COLUMNS if c not in cols]
    if missing_required:
        raise RuntimeError(f"{TABLE} is missing required column(s): {missing_required}")
    has_is_official = "is_official" in cols
    if not has_is_official:
        summary.warnings.append("column is_official missing -- run migration in coin_keeper")

    summary.row_count_before = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]

    select_columns = [c for c in REQUIRED_COLUMNS if c not in ("id", "country_id")]
    if has_is_official:
        select_columns.append("is_official")

    db_map_dirty = False
    for entry in sorted(official, key=lambda e: e["slug"]):
        slug = entry["slug"]

        if entry.get("missing_from_nbu"):
            summary.rows.append(
                SeriesRowReport(
                    slug=slug,
                    db_id=db_map.get(slug),
                    action="skip",
                    note="missing_from_nbu -- resolve by hand",
                )
            )
            continue

        values = build_series_values(entry, has_is_official)

        if slug in db_map:
            db_id = db_map[slug]
            current = _select_current(conn, db_id, select_columns)
            if current is None:
                raise RuntimeError(
                    f"db_map.json points {slug!r} at id={db_id}, but no such row "
                    f"exists in {TABLE} for country_id={COUNTRY_ID}"
                )
            changes = diff_row(current, values)
            if changes:
                _execute_update(conn, db_id, values)
                summary.rows.append(SeriesRowReport(slug=slug, db_id=db_id, action="update", changes=changes))
            else:
                summary.rows.append(SeriesRowReport(slug=slug, db_id=db_id, action="unchanged"))
        else:
            new_id = _execute_insert(conn, values)
            db_map[slug] = new_id
            db_map_dirty = True
            summary.rows.append(SeriesRowReport(slug=slug, db_id=new_id, action="insert", changes=values))

    summary.row_count_after = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    return db_map_dirty


def load_series(dsn: str | None = None) -> LoadSeriesSummary:
    series_data = json.loads(SERIES_JSON_PATH.read_text(encoding="utf-8"))
    official = [e for e in series_data.get("series", []) if e.get("is_official")]

    db_map_data = load_db_map()
    db_map: dict[str, int] = db_map_data.get("series", {})

    summary = LoadSeriesSummary()

    try:
        dsn = dsn or get_database_url()
    except RuntimeError as exc:
        summary.error = str(exc)
        summary.print_report()
        return summary

    try:
        with psycopg.connect(dsn) as conn:
            with conn.transaction():
                db_map_dirty = _run_transaction(conn, official, db_map, summary)
            # conn.transaction() exited without raising -> committed.
    except Exception as exc:
        summary.error = str(exc)
        summary.print_report()
        return summary

    summary.committed = True
    if db_map_dirty:
        db_map_data["series"] = db_map
        write_db_map(db_map_data)
        summary.db_map_updated = True

    summary.print_report()
    return summary
