from collector.countries.ua.series import _merge, _pair_series, _validate_entry


# ---------------------------------------------------------------------- #
# _pair_series -- id-set-only matching, never by name text
# ---------------------------------------------------------------------- #


def test_pair_series_exact_match():
    uk = {"2000-ліття Різдва Христового": {"88", "89", "95", "96", "161", "163"}}
    en = {"2000 Years of Christmas": {"88", "89", "95", "96", "161", "163"}}
    warnings = []
    result = _pair_series(uk, en, warnings)
    assert result == {"2000-ліття Різдва Христового": "2000 Years of Christmas"}
    assert warnings == []


def test_pair_series_partial_overlap_warns_but_still_pairs():
    uk = {"Серія А": {"1", "2", "3"}}
    en = {"Series A": {"1", "2"}}  # missing id 3
    warnings = []
    result = _pair_series(uk, en, warnings)
    assert result == {"Серія А": "Series A"}
    assert any("incomplete" in w for w in warnings)


def test_pair_series_no_overlap_leaves_unpaired():
    uk = {"Серія Без Пари": {"1", "2"}}
    en = {"Unrelated Series": {"99", "100"}}
    warnings = []
    result = _pair_series(uk, en, warnings)
    assert result == {}
    assert any("has no en pair" in w for w in warnings)
    assert any("has no uk pair" in w for w in warnings)


def test_pair_series_never_uses_name_similarity():
    # Names look nothing alike, but ids overlap completely -- must pair.
    # Names look similar (same as a decoy), but ids don't overlap at all
    # -- must NOT pair on that basis.
    uk = {
        "Зовсім Інша Назва": {"5", "6"},
        "Схожа Назва": {"7", "8"},
    }
    en = {
        "Completely Different Name": {"5", "6"},
        "Similar-ish Name": {"999"},  # no overlap with "Схожа Назва"
    }
    warnings = []
    result = _pair_series(uk, en, warnings)
    assert result == {"Зовсім Інша Назва": "Completely Different Name"}
    assert "Схожа Назва" not in result


def test_pair_series_greedy_resolves_double_claim():
    # Two uk series both overlap the same en series; the one with the
    # larger overlap should win it, the other stays unpaired.
    uk = {
        "Серія Велика": {"1", "2", "3", "4"},
        "Серія Мала": {"1"},
    }
    en = {"Shared En Series": {"1", "2", "3", "4"}}
    warnings = []
    result = _pair_series(uk, en, warnings)
    assert result == {"Серія Велика": "Shared En Series"}
    assert "Серія Мала" not in result


# ---------------------------------------------------------------------- #
# _validate_entry
# ---------------------------------------------------------------------- #


def test_validate_official_entry_ok():
    entry = {
        "slug": "test-slug",
        "is_official": True,
        "names": {"uk": "Тест", "en": "Test"},
        "nbu_card_count": 1,
        "year_range": [2000, 2000],
    }
    assert _validate_entry(entry) == []


def test_validate_official_entry_rejects_membership():
    entry = {
        "slug": "test-slug",
        "is_official": True,
        "names": {"uk": "Тест"},
        "membership": {"title_prefix": "x"},
    }
    errors = _validate_entry(entry)
    assert any("must not have" in e for e in errors)


def test_validate_curated_entry_requires_membership():
    entry = {"slug": "curated-slug", "is_official": False, "names": {"uk": "Тест"}}
    errors = _validate_entry(entry)
    assert any("requires 'membership'" in e for e in errors)


def test_validate_curated_entry_with_title_prefix_ok():
    entry = {
        "slug": "curated-slug",
        "is_official": False,
        "names": {"uk": "Тест"},
        "membership": {"title_prefix": "Тест"},
    }
    assert _validate_entry(entry) == []


def test_validate_curated_entry_with_card_ids_ok():
    entry = {
        "slug": "curated-slug",
        "is_official": False,
        "names": {"uk": "Тест"},
        "membership": {"card_ids": ["1", "2"]},
    }
    assert _validate_entry(entry) == []


def test_validate_entry_missing_names_uk():
    entry = {"slug": "x", "is_official": True, "names": {}}
    errors = _validate_entry(entry)
    assert any("names.uk" in e for e in errors)


# ---------------------------------------------------------------------- #
# _merge
# ---------------------------------------------------------------------- #


def test_merge_new_series_gets_fresh_slug():
    fresh = [{"is_official": True, "names": {"uk": "Нова Серія", "en": "New Series"}, "nbu_card_count": 2, "year_range": [2020, 2020]}]
    merged, conflicts = _merge(fresh, [], [])
    assert conflicts == []
    assert len(merged) == 1
    assert merged[0]["slug"]
    assert merged[0]["is_official"] is True


def test_merge_reuses_existing_slug_by_normalize_match():
    existing = [
        {
            "slug": "old-stable-slug",
            "is_official": True,
            "names": {"uk": "Античні пам`ятки України", "en": "Old En"},
            "nbu_card_count": 1,
            "year_range": [2000, 2000],
        }
    ]
    # Fresh scan sees a different apostrophe character for the same series.
    fresh = [
        {
            "is_official": True,
            "names": {"uk": "Античні пам’ятки України", "en": "New En"},
            "nbu_card_count": 2,
            "year_range": [2001, 2001],
        }
    ]
    merged, conflicts = _merge(fresh, existing, [])
    assert conflicts == []
    assert len(merged) == 1
    assert merged[0]["slug"] == "old-stable-slug"
    assert merged[0]["nbu_card_count"] == 2  # official rows are fully overwritten


def test_merge_curated_entries_carried_over_unchanged():
    curated = {
        "slug": "curated-one",
        "is_official": False,
        "names": {"uk": "Кураторська Серія", "en": "Curated"},
        "membership": {"title_prefix": "Кураторська"},
    }
    merged, conflicts = _merge([], [curated], [])
    assert conflicts == []
    assert merged == [curated]


def test_merge_missing_official_series_flagged_not_deleted():
    existing = [
        {
            "slug": "gone-series",
            "is_official": True,
            "names": {"uk": "Зникла Серія", "en": "Gone"},
            "nbu_card_count": 3,
            "year_range": [1999, 1999],
        }
    ]
    warnings: list[str] = []
    merged, conflicts = _merge([], existing, warnings)
    assert conflicts == []
    assert len(merged) == 1
    assert merged[0]["slug"] == "gone-series"
    assert merged[0]["missing_from_nbu"] is True
    assert any("missing_from_nbu" in w for w in warnings)


def test_merge_conflict_when_fresh_official_matches_curated():
    curated = {
        "slug": "curated-slug",
        "is_official": False,
        "names": {"uk": "Ми сильні. Ми разом.", "en": "We Are Strong"},
        "membership": {"title_prefix": "Ми сильні"},
    }
    fresh = [
        {
            "is_official": True,
            "names": {"uk": "Ми сильні. Ми разом.", "en": "We Are Strong. We Are United"},
            "nbu_card_count": 25,
            "year_range": [2022, 2022],
        }
    ]
    merged, conflicts = _merge(fresh, [curated], [])
    assert merged is None
    assert len(conflicts) == 1
    assert "curated-slug" in conflicts[0]


def test_merge_result_sorted_by_slug():
    fresh = [
        {"is_official": True, "names": {"uk": "Я Остання", "en": None}, "nbu_card_count": 1, "year_range": None},
        {"is_official": True, "names": {"uk": "А Перша", "en": None}, "nbu_card_count": 1, "year_range": None},
    ]
    merged, conflicts = _merge(fresh, [], [])
    assert conflicts == []
    assert [e["slug"] for e in merged] == sorted(e["slug"] for e in merged)
