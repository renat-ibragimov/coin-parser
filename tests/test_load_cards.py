"""Pure decision-logic tests for load_cards.py -- no database.

Same split as test_load_series.py: the SQL execution helpers and the
transaction wiring need a real Postgres to test honestly and this repo
has none, so what is covered here is where the decisions actually live
-- the card-to-column mapping, the diff that makes a rerun a no-op, the
edited_fields rule, and the media keys/rows built from what the photo
step left on disk (that last one does touch the filesystem, on a
tmp_path, because reading the finished WebPs is half of what it does).
"""

import hashlib
import json
from datetime import date
from decimal import Decimal

import pytest

from collector.countries.ua.load_cards import (
    NEVER_WRITTEN,
    build_artists,
    build_item_values,
    build_links,
    build_media_uploads,
    diff_row,
    metal_kind_of,
    nbu_card_id,
    source_id_fs,
    storage_key,
    weight_of,
)


def _card(**overrides) -> dict:
    card = {
        "source_id": "nbu:161",
        "titles": {"uk": "Різдво Христове", "en": "Christmas"},
        "denomination": {"value": 5, "unit": "hryvnia"},
        "circulation_date": "1999-12-29",
        "year": 1999,
        "material": "nickel_silver",
        "mintage": {"announced": 500000, "actual": 0},
        "weight_grams": 16.54,
        "fine_weight_grams": None,
        "diameter_mm": 35.0,
        "quality": "uncirculated",
        "edge": "reeded",
        "description": {"uk": {"general": "…"}, "en": {"general": "…"}},
        "artists": {"designers": [{"uk": "Іваненко", "en": "Ivanenko"}], "sculptors": []},
        "ua_coins": {"id": 43, "url": "https://www.ua-coins.info/ua/list/43-rizdvo"},
    }
    card.update(overrides)
    return card


def _values(card=None, **kwargs):
    kwargs.setdefault("series_id", 8)
    kwargs.setdefault("denomination_id", 3)
    return build_item_values(card or _card(), **kwargs)


# ---------------------------------------------------------------------- #
# build_item_values
# ---------------------------------------------------------------------- #


def test_build_item_values_maps_the_whole_card():
    assert _values() == {
        "item_type": "coin",
        "series_id": 8,
        "denomination_id": 3,
        "title_original": "Різдво Христове",
        "original_lang": "uk",
        "title_uk": "Різдво Христове",
        "title_uk_source": "official",
        "title_en": "Christmas",
        "title_en_source": "official",
        "issue_year": 1999,
        "issue_date": date(1999, 12, 29),
        "mintage_announced": 500000,
        "mintage_actual": 0,
        "material": "nickel_silver",
        "metal_kind": "base",
        "weight_grams": Decimal("16.54"),
        "diameter_mm": Decimal("35.0"),
        "edge": "reeded",
        "quality": "uncirculated",
        "descriptions": {"uk": {"general": "…"}, "en": {"general": "…"}},
        "artists": {"designers": [{"uk": "Іваненко", "en": "Ivanenko"}], "sculptors": []},
        "status": "active",
        "source_key": "nbu:161",
    }


def test_build_item_values_never_writes_a_curator_column():
    assert not NEVER_WRITTEN & set(_values())


def test_build_item_values_keeps_a_printed_zero_mintage():
    # NBU printed "0" for the actual mintage; that is data, not a gap.
    assert _values()["mintage_actual"] == 0


def test_build_item_values_omits_a_missing_mintage_rather_than_nulling_it():
    values = _values(_card(mintage={"announced": None, "actual": None}))
    assert "mintage_announced" not in values
    assert "mintage_actual" not in values


def test_build_item_values_omits_english_columns_when_nbu_has_no_english():
    values = _values(_card(titles={"uk": "Різдво Христове", "en": None}))
    assert "title_en" not in values
    assert "title_en_source" not in values
    assert values["title_uk"] == "Різдво Христове"


def test_build_item_values_omits_measurements_the_card_does_not_carry():
    values = _values(_card(weight_grams=None, fine_weight_grams=None, diameter_mm=None))
    assert "weight_grams" not in values
    assert "diameter_mm" not in values


def test_build_item_values_omits_empty_descriptions_and_artists():
    values = _values(_card(description=None, artists=None))
    assert "descriptions" not in values
    assert "artists" not in values


def test_build_item_values_without_a_denomination_row():
    values = _values(_card(), denomination_id=None)
    assert "denomination_id" not in values


def test_build_item_values_needs_a_title_in_the_original_language():
    with pytest.raises(RuntimeError, match="title_original"):
        _values(_card(titles={"uk": None, "en": "Christmas"}))


def test_build_item_values_needs_an_issue_year():
    with pytest.raises(RuntimeError, match="issue_year"):
        _values(_card(year=None))


def test_build_item_values_gives_numbers_as_decimals():
    # numeric(10,3) reads back as Decimal('16.540'); a float 16.54 would
    # never compare equal to it, and the rerun would rewrite the row.
    values = _values()
    assert values["weight_grams"] == Decimal("16.540")
    assert values["diameter_mm"] == Decimal("35.000")


# ---------------------------------------------------------------------- #
# metal, weight, artists
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("material", "expected"),
    [
        ("gold", "precious"),
        ("silver", "precious"),
        ("nickel_silver", "base"),
        ("bimetallic", "base"),
        (None, "unknown"),
    ],
)
def test_metal_kind_of(material, expected):
    assert metal_kind_of(material) == expected


def test_weight_falls_back_to_the_fine_weight_for_gold():
    # NBU publishes only "вага у чистоті" for gold: 15.55 g of a
    # 900-fineness 50-hryvnia coin, and no gross weight anywhere.
    assert weight_of({"weight_grams": None, "fine_weight_grams": 15.55}) == Decimal("15.55")


def test_weight_prefers_the_gross_weight_when_there_is_one():
    assert weight_of({"weight_grams": 16.54, "fine_weight_grams": 15.55}) == Decimal("16.54")


def test_weight_is_none_when_the_card_has_neither():
    assert weight_of({"weight_grams": None, "fine_weight_grams": None}) is None


def test_build_artists_always_carries_both_keys():
    assert build_artists({"artists": {"designers": [{"uk": "А"}]}}) == {
        "designers": [{"uk": "А"}],
        "sculptors": [],
    }


def test_build_artists_is_none_when_nobody_is_credited():
    assert build_artists({"artists": {"designers": [], "sculptors": []}}) is None


# ---------------------------------------------------------------------- #
# diff_row
# ---------------------------------------------------------------------- #


def test_second_run_is_a_no_op():
    values = _values()
    # What a SELECT of the row this run wrote gives back.
    current = {"id": 559, "edited_fields": None, **values}
    changes, protected = diff_row(current, values, current["edited_fields"])
    assert changes == {}
    assert protected == []


def test_diff_row_reports_only_what_moved():
    current = {"title_uk": "Старе", "issue_year": 1999, "quality": "proof"}
    intended = {"title_uk": "Нове", "issue_year": 1999, "quality": "proof"}
    changes, protected = diff_row(current, intended)
    assert changes == {"title_uk": ("Старе", "Нове")}
    assert protected == []


def test_diff_row_ignores_columns_this_step_does_not_write():
    current = {"title_uk": "Тест", "notes": "рукою куратора"}
    changes, _ = diff_row(current, {"title_uk": "Тест"})
    assert changes == {}


def test_edited_field_is_skipped_and_reported_not_overwritten():
    current = {"title_uk": "Виправлено куратором", "quality": "proof"}
    intended = {"title_uk": "Різдво Христове", "quality": "uncirculated"}
    changes, protected = diff_row(current, intended, ["title_uk"])
    assert changes == {"quality": ("proof", "uncirculated")}
    assert protected == ["title_uk"]


def test_edited_field_that_already_agrees_is_not_reported_as_skipped():
    current = {"title_uk": "Різдво Христове"}
    changes, protected = diff_row(current, {"title_uk": "Різдво Христове"}, ["title_uk"])
    assert changes == {}
    assert protected == []


# ---------------------------------------------------------------------- #
# keys and links
# ---------------------------------------------------------------------- #


def test_source_id_fs_drops_the_colon():
    assert source_id_fs("nbu:88") == "nbu_88"


def test_nbu_card_id_is_the_bare_number():
    # price_source_links keeps NBU's card id, not a URL.
    assert nbu_card_id("nbu:161") == "161"


def test_storage_key_does_not_mention_the_catalog_item():
    # The mirror into the bucket runs before the row exists, and an
    # adopted legacy record keeps an id this side cannot predict.
    assert storage_key("nbu:88", "obverse", 1200) == "catalog-src/nbu_88/obverse_1200.webp"


def test_build_links_covers_both_sources_for_a_matched_card():
    assert build_links(_card()) == {
        "NBU": "161",
        "UA-Coins": "https://www.ua-coins.info/ua/list/43-rizdvo",
    }


def test_build_links_invents_no_ua_coins_link_for_an_unmatched_card():
    assert build_links(_card(ua_coins=None)) == {"NBU": "161"}


# ---------------------------------------------------------------------- #
# media
# ---------------------------------------------------------------------- #


def _stage_media(media_dir, source_id, sides=(300, 600), roles=("obverse", "reverse")):
    out = media_dir / "out" / source_id_fs(source_id)
    out.mkdir(parents=True)
    src = media_dir / "src" / source_id_fs(source_id)
    src.mkdir(parents=True)
    photos_card = {"source_id": source_id, "roles": {}}
    candidates = []
    for role in roles:
        for side in sides:
            (out / f"{role}_{side}.webp").write_bytes(f"{role}{side}".encode())
        photos_card["roles"][role] = {
            "winner": {"source": "ua_coins", "src_file": f"uacoins_{role}.webp"},
            "files": [
                {"file": f"{role}_{side}.webp", "width": side, "height": side} for side in sides
            ],
        }
        candidates.append(
            {"file": f"uacoins_{role}.webp", "url": f"https://ua-coins.info/{role}.webp"}
        )
    (src / "meta.json").write_text(json.dumps({"candidates": candidates}), encoding="utf-8")
    return photos_card


def test_build_media_uploads_builds_one_row_per_role(tmp_path):
    photos_card = _stage_media(tmp_path, "nbu:88")
    uploads, notes = build_media_uploads("nbu:88", photos_card, tmp_path)
    assert notes == []
    assert [u.role for u in uploads] == ["obverse", "reverse"]


def test_media_row_points_at_the_largest_size_and_lists_them_all(tmp_path):
    photos_card = _stage_media(tmp_path, "nbu:89", sides=(300, 600, 1200))
    uploads, _ = build_media_uploads("nbu:89", photos_card, tmp_path)
    row = uploads[0].row_values()
    assert row["storage_key"] == "catalog-src/nbu_89/obverse_1200.webp"
    assert row["thumbnail_key"] == "catalog-src/nbu_89/obverse_300.webp"
    assert row["variants"] == {
        "300": "catalog-src/nbu_89/obverse_300.webp",
        "600": "catalog-src/nbu_89/obverse_600.webp",
        "1200": "catalog-src/nbu_89/obverse_1200.webp",
    }
    assert row["width"] == 1200
    assert row["height"] == 1200


def test_media_row_carries_provenance_and_the_real_bytes(tmp_path):
    photos_card = _stage_media(tmp_path, "nbu:88")
    uploads, _ = build_media_uploads("nbu:88", photos_card, tmp_path)
    row = uploads[0].row_values()
    assert row["source"] == "ua_coins"
    assert row["attribution"] == "ua-coins.info"
    assert row["mime_type"] == "image/webp"
    assert row["external_url"] == "https://ua-coins.info/obverse.webp"
    # size_bytes is every stored size together, sha256 is the largest file.
    assert row["size_bytes"] == len(b"obverse300") + len(b"obverse600")
    assert row["sha256"] == hashlib.sha256(b"obverse600").hexdigest()


def test_a_tier_the_source_was_too_small_for_is_simply_absent(tmp_path):
    # Tiers are labels, not promises: a 600 px source has no 1200 px form.
    photos_card = _stage_media(tmp_path, "nbu:88", sides=(300, 600))
    uploads, _ = build_media_uploads("nbu:88", photos_card, tmp_path)
    assert set(uploads[0].variants_json) == {"300", "600"}


def test_a_role_without_a_winner_gets_no_row_and_a_note(tmp_path):
    photos_card = _stage_media(tmp_path, "nbu:88", roles=("obverse",))
    photos_card["roles"]["reverse"] = {"winner": None, "files": []}
    uploads, notes = build_media_uploads("nbu:88", photos_card, tmp_path)
    assert [u.role for u in uploads] == ["obverse"]
    assert notes == ["reverse: no winning photo, no media row"]


def test_a_file_photos_json_promised_but_that_is_gone_is_reported(tmp_path):
    photos_card = _stage_media(tmp_path, "nbu:88", roles=("obverse",))
    (tmp_path / "out" / "nbu_88" / "obverse_600.webp").unlink()
    uploads, notes = build_media_uploads("nbu:88", photos_card, tmp_path)
    assert set(uploads[0].variants_json) == {"300"}
    assert "obverse: obverse_600.webp listed in photos.json but missing on disk" in notes


def test_missing_src_meta_costs_the_reference_not_the_row(tmp_path):
    photos_card = _stage_media(tmp_path, "nbu:88", roles=("obverse",))
    (tmp_path / "src" / "nbu_88" / "meta.json").unlink()
    uploads, _ = build_media_uploads("nbu:88", photos_card, tmp_path)
    assert uploads[0].row_values()["external_url"] is None
    assert uploads[0].row_values()["storage_key"] == "catalog-src/nbu_88/obverse_600.webp"
