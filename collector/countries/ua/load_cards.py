"""Loads a collected series' coins into coin_keeper's catalog_items, with
their media_files rows and their price_source_links. The third and
central step in this repo that writes to the production database, after
load_series (which it depends on) and before load_prices (which stops
being a no-op once this has run).

Order of operations, and it is not interchangeable -- the files go into
the bucket BEFORE the rows go into the database, never the other way
round, because a catalog row pointing at an object that is not there yet
is a broken card on a live site:

    1. rsync the processed media to the server:
       rsync -av staging/ua/<slug>/media/out/ deploy@server:~/media-drop/<slug>/

    2. mirror that drop into the bucket, on the server:
       docker compose run --rm -v ~/media-drop:/drop --entrypoint /bin/sh minio-init -c \
         "mc alias set local http://minio:9000 $S3_ACCESS_KEY $S3_SECRET_KEY && \
          mc mirror --overwrite /drop/<slug>/ local/coinkeeper-media/catalog-src/"

    3. python -m collector ua --series "<name>" --step load-cards
       (through an ssh tunnel to postgres, DATABASE_URL pointed at it)

    4. python -m collector ua --series "<name>" --step load-prices
       -- skipped:not_in_db for this series drops to zero here.

That mc invocation is why media storage keys are
`catalog-src/<source_id_fs>/<role>_<size>.webp` and carry no
catalog_item_id: `mc mirror` copies `media/out/nbu_88/obverse_300.webp`
to `catalog-src/nbu_88/obverse_300.webp`, and it can do that before the
row for nbu:88 exists -- or when that row is about to be created, or
adopted, and its id is not known on this side at all.

Records are never deleted. A coin already in the shared catalog is
updated in place, keeping its id, because collection items hang off that
id; the worst thing this step could do is replace a row rather than
correct it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from collector.core.db import existing_columns, get_database_url
from collector.core.staging import DEFAULT_STAGING_ROOT
from collector.countries.ua.load_series import load_db_map
from collector.countries.ua.series import find_official_series, load_series_json

TABLE = "catalog_items"
LINKS_TABLE = "price_source_links"
MEDIA_TABLE = "media_files"
DENOMINATIONS_TABLE = "denominations"
SERIES_TABLE = "coin_series"

# coin_keeper's countries.id for Ukraine, the same adapter-level constant
# load_series uses: this adapter only ever writes Ukraine's rows.
COUNTRY_ID = 2

# The labels price_source_links already carries in production (see
# coin_keeper app/ukraine_pipeline/catalog.py LINK_SOURCES). Reusing them
# is what makes this an update of the existing links rather than a second
# set of rows next to them.
LINK_SOURCE_NBU = "NBU"
LINK_SOURCE_UA_COINS = "UA-Coins"

MIGRATION_HINT = "run migration 0006 in coin_keeper (alembic upgrade head)"

# Every column this step is allowed to write. Anything not here it does
# not touch, on any run.
OWNED_COLUMNS = [
    "item_type",
    "series_id",
    "denomination_id",
    "title_original",
    "original_lang",
    "title_uk",
    "title_uk_source",
    "title_en",
    "title_en_source",
    "issue_year",
    "issue_date",
    "mintage_announced",
    "mintage_actual",
    "material",
    "metal_kind",
    "weight_grams",
    "diameter_mm",
    "edge",
    "quality",
    "descriptions",
    "artists",
    "status",
    "source_key",
]

# Written once, when the row is created, and never again: what group a
# coin belongs to and who owns the record are not this step's to revise.
# 'commemorative' is what coin_keeper's own NBU-side creator
# (app/ukraine_pipeline/gaps.py) puts on a card from the same catalog,
# and collection_group is NOT NULL with no default, so an insert has to
# say something.
INSERT_ONLY_COLUMNS = {
    "country_id": COUNTRY_ID,
    "collection_group": "commemorative",
    "created_by": None,
}

# Never written at all, on any run. Curator territory (the catalogue
# numbers, the notes, the subtype) and the archive workflow. Two more
# columns a loader must not revise -- collection_group and created_by --
# are in INSERT_ONLY_COLUMNS instead: they are NOT NULL or meaningful
# only at creation, so a new row has to state them once and no run ever
# touches them again.
NEVER_WRITTEN = frozenset(
    {
        "catalog_km",
        "catalog_uc",
        "catalog_numista",
        "notes",
        "subtype",
        "is_archived",
        "archived_at",
        "archive_reason",
        "edited_fields",
    }
)

# Postgres enum columns: psycopg sends a str as text, and text does not
# implicitly become an enum in an INSERT/UPDATE target, so the cast is
# spelled out. jsonb goes through Jsonb() instead and needs none.
COLUMN_CASTS = {
    "collection_group": "collection_group",
    "metal_kind": "metal_kind",
    "title_uk_source": "translation_source",
    "title_en_source": "translation_source",
}
JSONB_COLUMNS = frozenset({"descriptions", "artists"})

REQUIRED_COLUMNS = {
    TABLE: [
        "id",
        "country_id",
        "collection_group",
        "created_by",
        "edited_fields",
        *OWNED_COLUMNS,
    ],
    SERIES_TABLE: ["id", "is_official"],
    LINKS_TABLE: ["catalog_item_id", "source", "external_id", "match_status", "matched_at"],
    MEDIA_TABLE: [
        "catalog_item_id",
        "collection_item_id",
        "owner_id",
        "role",
        "source",
        "license",
        "attribution",
        "storage_key",
        "external_url",
        "thumbnail_key",
        "variants",
        "mime_type",
        "width",
        "height",
        "size_bytes",
        "sha256",
    ],
    DENOMINATIONS_TABLE: ["id", "country_id", "currency_code", "value", "unit", "sort_order"],
}

# Gold and silver are the precious ones; nickel silver and bimetal are
# not, whatever the "silver" in the name suggests.
PRECIOUS_MATERIALS = frozenset({"gold", "silver"})

# unit -> (currency_code, what one unit is worth in the currency's
# smallest unit). The second number is denominations.sort_order, computed
# the same way coin_keeper computes it (reference_data/denominations.py).
DENOMINATION_UNITS = {
    "hryvnia": ("UAH", 100),
    "karbovanets": ("UAK", 100),
}

# ---------------------------------------------------------------------- #
# media
# ---------------------------------------------------------------------- #

MEDIA_ROLES = ("obverse", "reverse")
STORAGE_PREFIX = "catalog-src"
MIME_TYPE = "image/webp"
_VARIANT_FILE_RE = re.compile(r"^(?P<role>[a-z_]+)_(?P<side>\d+)\.webp$")

# Provenance travels with the file: source says who took the picture,
# attribution whom to credit. Both strings are coin_keeper's own
# (app/ukraine_pipeline/photos.py) so a row written here is
# indistinguishable from one that pipeline wrote.
MEDIA_ATTRIBUTION = {
    "nbu": "Національний банк України",
    "ua_coins": "ua-coins.info",
}
MEDIA_LICENSE = {
    "nbu": "bank.gov.ua/ua/useterms: the material may be used with a reference to the source",
    "ua_coins": "ua-coins.info, © 2015-2026, used with attribution",
}


def source_id_fs(source_id: str) -> str:
    """"nbu:88" -> "nbu_88" -- the same colon-free spelling the staging
    directories and the price cache use, and the one the bucket keys are
    built from."""
    return source_id.replace(":", "_")


def storage_key(source_id: str, role: str, side: int) -> str:
    """Where this file lives in the bucket.

    Deliberately free of catalog_item_id: the mirror step runs before
    the rows exist, and an adopted legacy record keeps an id this side
    has no way to predict.
    """
    return f"{STORAGE_PREFIX}/{source_id_fs(source_id)}/{role}_{side}.webp"


@dataclass(frozen=True)
class MediaVariant:
    side: int
    key: str
    width: int
    height: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class MediaUpload:
    """One role of one coin: every size of it that exists on disk."""

    role: str
    source: str  # 'nbu' | 'ua_coins' -- the winner's provenance
    external_url: str | None
    variants: tuple[MediaVariant, ...]  # ascending by side

    @property
    def largest(self) -> MediaVariant:
        return self.variants[-1]

    @property
    def storage_key(self) -> str:
        return self.largest.key

    @property
    def thumbnail_key(self) -> str:
        return self.variants[0].key

    @property
    def variants_json(self) -> dict[str, str]:
        """media_files.variants as JSON has it: string keys, sorted."""
        return {str(v.side): v.key for v in self.variants}

    @property
    def total_bytes(self) -> int:
        return sum(v.size_bytes for v in self.variants)

    def row_values(self) -> dict:
        return {
            "role": self.role,
            "source": self.source,
            "license": MEDIA_LICENSE.get(self.source),
            "attribution": MEDIA_ATTRIBUTION.get(self.source),
            "storage_key": self.storage_key,
            "thumbnail_key": self.thumbnail_key,
            "variants": self.variants_json,
            "external_url": self.external_url,
            "mime_type": MIME_TYPE,
            "width": self.largest.width,
            "height": self.largest.height,
            "size_bytes": self.total_bytes,
            "sha256": self.largest.sha256,
        }


def _candidate_urls(src_dir: Path) -> dict[str, str]:
    """{downloaded file name: where it came from} from media/src/<id>/meta.json.

    Only used to fill media_files.external_url, which records the first
    publisher of the picture -- nothing ever loads it (docs/06-media-storage.md
    in coin_keeper), so a missing meta.json costs a reference, not a row.
    """
    meta_path = src_dir / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {c["file"]: c.get("url") for c in meta.get("candidates", []) if c.get("file")}


def build_media_uploads(
    source_id: str, photos_card: dict, media_dir: Path
) -> tuple[list[MediaUpload], list[str]]:
    """The media_files rows this coin should have, plus notes on the roles
    that will get none.

    Reads the finished WebPs to get their size and hash: photos.json
    records the pixel dimensions of every tier it wrote, but not the bytes,
    and both belong in the row.
    """
    out_dir = media_dir / "out" / source_id_fs(source_id)
    urls = _candidate_urls(media_dir / "src" / source_id_fs(source_id))
    uploads: list[MediaUpload] = []
    notes: list[str] = []

    roles = photos_card.get("roles", {})
    for role in MEDIA_ROLES:
        role_data = roles.get(role) or {}
        winner = role_data.get("winner")
        if not winner:
            # An anomaly of the photo step, already reported there. No row
            # is better than a row pointing at nothing.
            notes.append(f"{role}: no winning photo, no media row")
            continue

        variants: list[MediaVariant] = []
        for entry in role_data.get("files", []):
            match = _VARIANT_FILE_RE.match(entry["file"])
            if match is None or match.group("role") != role:
                notes.append(f"{role}: unexpected file name {entry['file']!r}, skipped")
                continue
            path = out_dir / entry["file"]
            if not path.exists():
                notes.append(f"{role}: {path.name} listed in photos.json but missing on disk")
                continue
            payload = path.read_bytes()
            side = int(match.group("side"))
            variants.append(
                MediaVariant(
                    side=side,
                    key=storage_key(source_id, role, side),
                    width=entry["width"],
                    height=entry["height"],
                    size_bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
            )

        if not variants:
            notes.append(f"{role}: winner recorded but no files on disk, no media row")
            continue

        variants.sort(key=lambda v: v.side)
        uploads.append(
            MediaUpload(
                role=role,
                source=winner["source"],
                external_url=urls.get(winner.get("src_file")),
                variants=tuple(variants),
            )
        )

    return uploads, notes


# ---------------------------------------------------------------------- #
# card -> column values (pure)
# ---------------------------------------------------------------------- #


def _decimal(value) -> Decimal | None:
    """Numeric columns compare as Decimal, so the intended value has to be
    one too: 15.55 read from JSON against Decimal('15.550') read from a
    numeric(10,3) column is only equal on the Decimal side."""
    if value is None:
        return None
    return Decimal(str(value))


def metal_kind_of(material: str | None) -> str:
    if material is None:
        return "unknown"
    return "precious" if material in PRECIOUS_MATERIALS else "base"


def weight_of(card: dict) -> Decimal | None:
    """weight_grams, falling back to the weight in fine metal.

    For gold coins NBU publishes only the fine weight ("вага у чистоті",
    15.55 g for a 900-fineness 50-hryvnia coin) and no gross weight at
    all. Recording the fine weight as the weight is a small lie that
    beats an empty column on every card of every gold series; the gross
    weight is nowhere to be had.
    """
    if card.get("weight_grams") is not None:
        return _decimal(card["weight_grams"])
    return _decimal(card.get("fine_weight_grams"))


def build_artists(card: dict) -> dict | None:
    artists = card.get("artists") or {}
    designers = artists.get("designers") or []
    sculptors = artists.get("sculptors") or []
    if not designers and not sculptors:
        return None
    return {"designers": designers, "sculptors": sculptors}


def build_item_values(
    card: dict, *, series_id: int, denomination_id: int | None, original_lang: str = "uk"
) -> dict:
    """The {column: value} this card should have in catalog_items.

    Only columns this step actually has something to say about are
    included -- as in load_series, a column left out is a column left
    alone, so a coin whose English title NBU never published does not
    have an existing title_en wiped by a NULL.

    Values are already in the types the database uses (date, Decimal,
    int, dict), which is what lets the diff against a selected row be a
    plain `==`.
    """
    titles = card.get("titles") or {}
    title_original = titles.get(original_lang)
    if not title_original:
        raise RuntimeError(
            f"{card['source_id']}: no {original_lang!r} title -- catalog_items.title_original "
            "is NOT NULL and this adapter has no second source for it"
        )

    if card.get("year") is None:
        raise RuntimeError(
            f"{card['source_id']}: no issue year -- catalog_items.issue_year is NOT NULL"
        )

    values: dict = {
        "item_type": "coin",
        "series_id": series_id,
        "title_original": title_original,
        "original_lang": original_lang,
        "issue_year": card["year"],
        "material": card.get("material"),
        "metal_kind": metal_kind_of(card.get("material")),
        "edge": card.get("edge"),
        "quality": card.get("quality"),
        # An imported record is published straight away; keeping drafts
        # out of the catalog is an admin-stage job and nothing reads the
        # column yet (coin_keeper migration 0006). A curator who does
        # change it protects it through edited_fields, like any field.
        "status": "active",
        "source_key": card["source_id"],
    }

    if denomination_id is not None:
        values["denomination_id"] = denomination_id

    title_uk = titles.get("uk")
    if title_uk:
        values["title_uk"] = title_uk
        values["title_uk_source"] = "official"
    title_en = titles.get("en")
    if title_en:
        values["title_en"] = title_en
        # "official" unless the card itself says otherwise -- normal
        # parse() output never sets this key, only a series with no NBU
        # English page at all gets a title_en filled in by hand/LLM after
        # the fact (see vidrodzhennia-khrystyianskoi-dukhovnosti-v-ukraini),
        # and that title_en_source travels with it in cards.json.
        values["title_en_source"] = card.get("title_en_source", "official")

    if card.get("circulation_date"):
        values["issue_date"] = date.fromisoformat(card["circulation_date"])

    mintage = card.get("mintage") or {}
    # 0 is what NBU printed, not "unknown" -- only None means no data.
    if mintage.get("announced") is not None:
        values["mintage_announced"] = mintage["announced"]
    if mintage.get("actual") is not None:
        values["mintage_actual"] = mintage["actual"]

    weight = weight_of(card)
    if weight is not None:
        values["weight_grams"] = weight
    diameter = _decimal(card.get("diameter_mm"))
    if diameter is not None:
        values["diameter_mm"] = diameter

    if card.get("description"):
        values["descriptions"] = card["description"]
    artists = build_artists(card)
    if artists is not None:
        values["artists"] = artists

    return values


def diff_row(current: dict, intended: dict, edited_fields=None) -> tuple[dict, list[str]]:
    """({column: (old, new)} for what differs, [columns left alone]).

    edited_fields names the fields a human has corrected in coin_keeper.
    Nothing writes it yet -- it is NULL on every row today -- but the rule
    holds from the first run: a field in that list is skipped and said so
    in the report, never quietly overwritten on the next load.
    """
    protected = set(edited_fields or ())
    changes: dict[str, tuple] = {}
    skipped: list[str] = []
    for col, new_value in intended.items():
        if col in protected:
            if current.get(col) != new_value:
                skipped.append(col)
            continue
        if current.get(col) != new_value:
            changes[col] = (current.get(col), new_value)
    return changes, skipped


def nbu_card_id(source_id: str) -> str:
    """"nbu:161" -> "161" -- what price_source_links keeps as the NBU
    external_id (a card id, not a URL)."""
    return source_id.split(":", 1)[1]


def build_links(card: dict) -> dict[str, str]:
    """{price_source_links.source: external_id} for this card.

    UA-Coins only when the card is actually matched: an unmatched coin
    gets no link invented for it, and an existing one is not removed
    either -- a match that failed today is not evidence the old link is
    wrong.

    uCoin links are never created here and never touched. The legacy ones
    are cleaned up by a separate one-off command, not by a loader.
    """
    links = {LINK_SOURCE_NBU: nbu_card_id(card["source_id"])}
    ua = card.get("ua_coins")
    if ua and ua.get("url"):
        links[LINK_SOURCE_UA_COINS] = ua["url"]
    return links


# ---------------------------------------------------------------------- #
# reporting
# ---------------------------------------------------------------------- #


def _short(value) -> str:
    text = repr(value)
    return text if len(text) <= 48 else text[:45] + "..."


@dataclass
class CardReport:
    source_id: str
    action: str  # "insert" | "update" | "adopt" | "unchanged" | "error"
    db_id: int | None = None
    changes: dict[str, tuple] = field(default_factory=dict)
    protected: list[str] = field(default_factory=list)
    links_written: list[str] = field(default_factory=list)
    media_written: list[str] = field(default_factory=list)
    media_unchanged: list[str] = field(default_factory=list)
    media_notes: list[str] = field(default_factory=list)
    note: str | None = None


@dataclass
class LoadCardsSummary:
    series: str | None = None
    series_id: int | None = None
    cards: list[CardReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rows_before: int | None = None
    rows_after: int | None = None
    committed: bool = False
    error: str | None = None

    def print_report(self) -> None:
        print(f"[load-cards] {TABLE} in coin_keeper -- {self.series or '?'}")
        if self.error:
            print(f"[load-cards]   ERROR: {self.error}")
            print(
                "[load-cards]   transaction rolled back, nothing written"
                if self.rows_before is not None
                else "[load-cards]   nothing written"
            )
            return

        if self.series_id is not None:
            print(f"[load-cards]   series_id {self.series_id}")
        for c in self.cards:
            print(
                f"[load-cards]   {c.source_id:<10} {str(c.db_id or '-'):<7} {c.action:<10} "
                + (c.note or "")
            )
            for col, (old, new) in c.changes.items():
                print(f"[load-cards]     {col}: {_short(old)} -> {_short(new)}")
            for col in c.protected:
                print(f"[load-cards]     {col}: SKIPPED, listed in edited_fields")
            if c.links_written:
                print(f"[load-cards]     links: {', '.join(c.links_written)}")
            media = [f"{r} written" for r in c.media_written]
            media += [f"{r} unchanged" for r in c.media_unchanged]
            if media:
                print(f"[load-cards]     media: {', '.join(media)}")
            for note in c.media_notes:
                print(f"[load-cards]     media: {note}")

        for w in self.warnings:
            print(f"[load-cards]   WARNING: {w}")

        counts = {a: 0 for a in ("insert", "update", "adopt", "unchanged")}
        for c in self.cards:
            if c.action in counts:
                counts[c.action] += 1
        media_written = sum(len(c.media_written) for c in self.cards)
        media_unchanged = sum(len(c.media_unchanged) for c in self.cards)
        media_notes = sum(len(c.media_notes) for c in self.cards)
        protected = sum(len(c.protected) for c in self.cards)
        links = sum(len(c.links_written) for c in self.cards)
        print(
            f"[load-cards] coins: inserted {counts['insert']}, updated {counts['update']}, "
            f"adopted {counts['adopt']}, unchanged {counts['unchanged']}"
        )
        print(
            f"[load-cards] media roles: written {media_written}, unchanged {media_unchanged}, "
            f"reported without a row {media_notes}"
        )
        print(
            f"[load-cards] source links written {links}, "
            f"fields left to their editor {protected}"
        )
        if self.rows_before is not None:
            print(
                f"[load-cards] {TABLE} row count: {self.rows_before} -> {self.rows_after} "
                "(nothing is ever deleted here)"
            )
        if self.committed:
            print(
                "[load-cards] committed. Rerun with no new collect -- it should report "
                "0 changes, then run --step load-prices."
            )


# ---------------------------------------------------------------------- #
# SQL
# ---------------------------------------------------------------------- #


def _guard_schema(conn: psycopg.Connection) -> None:
    missing: list[str] = []
    for table, columns in REQUIRED_COLUMNS.items():
        present = existing_columns(conn, table)
        if not present:
            missing.append(f"{table} (table not found)")
            continue
        missing += [f"{table}.{c}" for c in columns if c not in present]
    if missing:
        raise RuntimeError(
            f"coin_keeper's schema is missing: {', '.join(missing)} -- {MIGRATION_HINT}"
        )


def _placeholder(col: str) -> str:
    cast = COLUMN_CASTS.get(col)
    return f"%({col})s::{cast}" if cast else f"%({col})s"


def _params(values: dict) -> dict:
    return {
        col: Jsonb(value) if col in JSONB_COLUMNS and value is not None else value
        for col, value in values.items()
    }


def _select_item(conn: psycopg.Connection, source_key: str) -> dict | None:
    """The shared-catalog row for this source_key, or None.

    created_by IS NULL is not optional: source_key is unique per user, so
    the same "nbu:88" legitimately exists again as a collector's private
    copy, and without the filter one of those could answer instead of the
    catalog row (docs/01_findings.md).
    """
    columns = ["id", "edited_fields", *OWNED_COLUMNS]
    row = conn.execute(
        f"SELECT {', '.join(columns)} FROM {TABLE} "
        "WHERE source_key = %(key)s AND created_by IS NULL",
        {"key": source_key},
    ).fetchone()
    return dict(zip(columns, row)) if row else None


def _select_item_for_adoption(conn: psycopg.Connection, external_id: str) -> dict | None:
    """The pre-NBU record for this card, found through its NBU link.

    A coin catalogued in the Excel/uCoin era has a source_key from that
    era and collection items hanging off its id, but it was linked to its
    NBU card by an earlier bridge run. That link is the only bridge back
    to it; matching on anything else would be guessing.
    """
    columns = ["id", "edited_fields", *OWNED_COLUMNS]
    prefixed = ", ".join(f"i.{c}" for c in columns)
    rows = conn.execute(
        f"""
        SELECT {prefixed}
        FROM {TABLE} i
        JOIN {LINKS_TABLE} l ON l.catalog_item_id = i.id
        WHERE l.source = %(source)s
          AND l.external_id = %(external_id)s
          AND i.created_by IS NULL
          AND (i.source_key IS NULL OR i.source_key NOT LIKE 'nbu:%%')
        """,
        {"source": LINK_SOURCE_NBU, "external_id": external_id},
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        ids = [r[0] for r in rows]
        raise RuntimeError(
            f"NBU card {external_id} is linked to more than one shared record ({ids}) -- "
            "resolve the duplicate in coin_keeper before loading"
        )
    return dict(zip(columns, rows[0]))


def _execute_update(conn: psycopg.Connection, item_id: int, values: dict) -> None:
    # Column names come only from OWNED_COLUMNS, never from data, so
    # composing them into the statement carries no injection risk; every
    # value is parameterized.
    set_clause = ", ".join(f"{col} = {_placeholder(col)}" for col in values)
    conn.execute(
        f"UPDATE {TABLE} SET {set_clause} WHERE id = %(id__)s",
        {**_params(values), "id__": item_id},
    )


def _execute_insert(conn: psycopg.Connection, values: dict) -> int:
    all_values = {**values, **INSERT_ONLY_COLUMNS}
    cols = list(all_values)
    row = conn.execute(
        f"INSERT INTO {TABLE} ({', '.join(cols)}) "
        f"VALUES ({', '.join(_placeholder(c) for c in cols)}) RETURNING id",
        _params(all_values),
    ).fetchone()
    return row[0]


def _resolve_denomination(
    conn: psycopg.Connection, denomination: dict, cache: dict[tuple, int]
) -> int | None:
    """denominations.id for a face value, created when it is missing."""
    value = denomination.get("value")
    unit = denomination.get("unit")
    if value is None or unit is None:
        return None
    if unit not in DENOMINATION_UNITS:
        raise RuntimeError(
            f"unknown denomination unit {unit!r} -- add it to DENOMINATION_UNITS "
            "with the currency it belongs to"
        )
    currency_code, minor_units = DENOMINATION_UNITS[unit]
    value = _decimal(value)
    key = (unit, value)
    if key in cache:
        return cache[key]

    row = conn.execute(
        f"SELECT id FROM {DENOMINATIONS_TABLE} "
        "WHERE country_id = %(country_id)s AND unit = %(unit)s AND value = %(value)s",
        {"country_id": COUNTRY_ID, "unit": unit, "value": value},
    ).fetchone()
    if row is None:
        row = conn.execute(
            f"INSERT INTO {DENOMINATIONS_TABLE} "
            "(country_id, currency_code, value, unit, sort_order) "
            "VALUES (%(country_id)s, %(currency_code)s, %(value)s, %(unit)s, %(sort_order)s) "
            "RETURNING id",
            {
                "country_id": COUNTRY_ID,
                "currency_code": currency_code,
                "value": value,
                "unit": unit,
                "sort_order": int(value * minor_units),
            },
        ).fetchone()
    cache[key] = row[0]
    return row[0]


def _upsert_links(conn: psycopg.Connection, item_id: int, links: dict[str, str]) -> list[str]:
    """Write the source links that are not already exactly right.

    Rewriting an unchanged row would move matched_at on every run and
    turn a no-op rerun into a table full of updates, so an identical row
    is left completely alone.
    """
    written: list[str] = []
    for source, external_id in links.items():
        row = conn.execute(
            f"SELECT external_id, match_status FROM {LINKS_TABLE} "
            "WHERE catalog_item_id = %(item_id)s AND source = %(source)s",
            {"item_id": item_id, "source": source},
        ).fetchone()
        if row is not None and row[0] == external_id and str(row[1]) == "confirmed":
            continue
        conn.execute(
            f"""
            INSERT INTO {LINKS_TABLE}
                (catalog_item_id, source, external_id, match_status, matched_at)
            VALUES (%(item_id)s, %(source)s, %(external_id)s, 'confirmed'::match_status, %(now)s)
            ON CONFLICT ON CONSTRAINT uq_price_source_links_catalog_item_id_source
            DO UPDATE SET external_id = EXCLUDED.external_id,
                          match_status = EXCLUDED.match_status,
                          matched_at = EXCLUDED.matched_at
            """,
            {
                "item_id": item_id,
                "source": source,
                "external_id": external_id,
                "now": datetime.now(timezone.utc),
            },
        )
        written.append(source)
    return written


def _write_media(
    conn: psycopg.Connection, item_id: int, uploads: list[MediaUpload]
) -> tuple[list[str], list[str]]:
    """(roles rewritten, roles already correct).

    A role is replaced whole: the shared rows for it go, then one new row
    arrives. Shared means owner_id IS NULL -- a collector's own photo of
    the same coin belongs to them and is never touched, and neither is a
    photo attached to a collection item rather than to the catalog record.
    A leftover uCoin hotlink for the role does go, which is the point: it
    is exactly what the new file replaces.
    """
    written: list[str] = []
    unchanged: list[str] = []
    for upload in uploads:
        values = upload.row_values()
        rows = conn.execute(
            f"SELECT id, storage_key, thumbnail_key, variants, width, height, size_bytes "
            f"FROM {MEDIA_TABLE} "
            "WHERE catalog_item_id = %(item_id)s AND role = %(role)s::media_role "
            "AND owner_id IS NULL AND collection_item_id IS NULL",
            {"item_id": item_id, "role": upload.role},
        ).fetchall()

        current = [
            {
                "storage_key": r[1],
                "thumbnail_key": r[2],
                "variants": r[3],
                "width": r[4],
                "height": r[5],
                "size_bytes": r[6],
            }
            for r in rows
        ]
        wanted = {k: values[k] for k in current[0]} if current else None
        if len(current) == 1 and current[0] == wanted:
            unchanged.append(upload.role)
            continue

        conn.execute(
            f"DELETE FROM {MEDIA_TABLE} "
            "WHERE catalog_item_id = %(item_id)s AND role = %(role)s::media_role "
            "AND owner_id IS NULL AND collection_item_id IS NULL",
            {"item_id": item_id, "role": upload.role},
        )
        conn.execute(
            f"""
            INSERT INTO {MEDIA_TABLE}
                (catalog_item_id, collection_item_id, owner_id, role, source, license,
                 attribution, storage_key, external_url, thumbnail_key, variants,
                 mime_type, width, height, size_bytes, sha256)
            VALUES (%(item_id)s, NULL, NULL, %(role)s::media_role, %(source)s::media_source,
                    %(license)s, %(attribution)s, %(storage_key)s, %(external_url)s,
                    %(thumbnail_key)s, %(variants)s, %(mime_type)s, %(width)s, %(height)s,
                    %(size_bytes)s, %(sha256)s)
            """,
            {**values, "item_id": item_id, "variants": Jsonb(values["variants"])},
        )
        written.append(upload.role)
    return written, unchanged


# ---------------------------------------------------------------------- #
# staging -> what to write (before any connection is opened)
# ---------------------------------------------------------------------- #


@dataclass
class CardPlan:
    card: dict
    uploads: list[MediaUpload]
    media_notes: list[str]


def _resolve_series_id(cards_data: dict) -> int:
    """coin_series.id for this series, through the fixed chain
    names.uk -> slug (series.json) -> id (db_map.json).

    Never by name-matching against the database: series correspondence is
    a hand-verified table, and a series that is not in it is a missing
    load-series run, not something to resolve by guessing.
    """
    uk_name = (cards_data.get("series") or {}).get("uk")
    if not uk_name:
        raise RuntimeError("cards.json has no series name -- rerun --step parse")
    entry = find_official_series(load_series_json(), uk_name)
    if entry is None:
        raise RuntimeError(
            f"series {uk_name!r} is not in countries/ua/series.json -- run --step series first"
        )
    slug = entry["slug"]
    db_map = load_db_map().get("series", {})
    if slug not in db_map:
        raise RuntimeError(
            f"series {slug!r} has no coin_series id in db_map.json -- run load-series first"
        )
    return db_map[slug]


def collect(series_dir: Path, summary: LoadCardsSummary) -> tuple[list[CardPlan], dict]:
    """Read and check everything on disk before opening a connection, so a
    broken staging directory never leaves a half-open transaction behind."""
    cards_path = series_dir / "parsed" / "cards.json"
    if not cards_path.exists():
        raise RuntimeError(f"no parsed cards at {cards_path} -- run --step parse/match first")
    cards_data = json.loads(cards_path.read_text(encoding="utf-8"))

    photos_path = series_dir / "parsed" / "photos.json"
    photos_by_id: dict[str, dict] = {}
    if photos_path.exists():
        photos_data = json.loads(photos_path.read_text(encoding="utf-8"))
        photos_by_id = {c["source_id"]: c for c in photos_data.get("cards", [])}
    else:
        summary.warnings.append(
            f"no {photos_path.name} -- run --step process-photos; no media rows will be written"
        )

    plans: list[CardPlan] = []
    for card in cards_data.get("cards", []):
        source_id = card["source_id"]
        photos_card = photos_by_id.get(source_id)
        if photos_card is None:
            plans.append(CardPlan(card=card, uploads=[], media_notes=["no entry in photos.json"]))
            continue
        uploads, notes = build_media_uploads(source_id, photos_card, series_dir / "media")
        plans.append(CardPlan(card=card, uploads=uploads, media_notes=notes))

    return plans, cards_data


# ---------------------------------------------------------------------- #
# entry point
# ---------------------------------------------------------------------- #


def _run_transaction(
    conn: psycopg.Connection,
    plans: list[CardPlan],
    cards_data: dict,
    summary: LoadCardsSummary,
) -> None:
    _guard_schema(conn)

    series_id = _resolve_series_id(cards_data)
    summary.series_id = series_id
    original_lang = cards_data.get("original_lang", "uk")

    summary.rows_before = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    denomination_cache: dict[tuple, int] = {}

    for plan in plans:
        card = plan.card
        source_id = card["source_id"]
        report = CardReport(source_id=source_id, action="unchanged")
        report.media_notes = list(plan.media_notes)

        denomination_id = _resolve_denomination(
            conn, card.get("denomination") or {}, denomination_cache
        )
        intended = build_item_values(
            card,
            series_id=series_id,
            denomination_id=denomination_id,
            original_lang=original_lang,
        )

        current = _select_item(conn, source_id)
        if current is None:
            # Second attempt: a record from before this catalog existed,
            # reachable only through the NBU link an earlier bridge wrote.
            current = _select_item_for_adoption(conn, nbu_card_id(source_id))
            adopting = current is not None
        else:
            adopting = False

        if current is None:
            item_id = _execute_insert(conn, intended)
            report.action = "insert"
            report.db_id = item_id
            report.changes = {col: (None, value) for col, value in intended.items()}
        else:
            item_id = current["id"]
            report.db_id = item_id
            changes, protected = diff_row(current, intended, current.get("edited_fields"))
            report.protected = protected
            if changes:
                _execute_update(conn, item_id, {col: new for col, (_, new) in changes.items()})
                report.changes = changes
                report.action = "adopt" if adopting else "update"
            elif adopting:
                # Cannot actually happen -- adoption always changes
                # source_key -- but the action should say what happened.
                report.action = "adopt"
            if adopting:
                report.note = f"legacy record, source_key was {current.get('source_key')!r}"

        report.links_written = _upsert_links(conn, item_id, build_links(card))
        written, unchanged = _write_media(conn, item_id, plan.uploads)
        report.media_written = written
        report.media_unchanged = unchanged
        summary.cards.append(report)

    summary.rows_after = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]


def load_cards(
    series_slug: str,
    dsn: str | None = None,
    staging_root: Path = DEFAULT_STAGING_ROOT,
) -> LoadCardsSummary:
    summary = LoadCardsSummary(series=series_slug)
    series_dir = staging_root / "ua" / series_slug

    try:
        plans, cards_data = collect(series_dir, summary)
    except Exception as exc:
        summary.error = f"{type(exc).__name__}: {exc}"
        summary.print_report()
        return summary

    print(f"[load-cards] {len(plans)} card(s) in staging, {series_dir}")

    try:
        dsn = dsn or get_database_url()
    except RuntimeError as exc:
        summary.error = str(exc)
        summary.print_report()
        return summary

    try:
        with psycopg.connect(dsn) as conn:
            with conn.transaction():
                _run_transaction(conn, plans, cards_data, summary)
            # conn.transaction() exited without raising -> committed.
    except Exception as exc:
        summary.error = f"{type(exc).__name__}: {exc}"
        summary.print_report()
        return summary

    summary.committed = True
    summary.print_report()
    return summary
