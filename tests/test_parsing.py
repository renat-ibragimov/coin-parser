from collector.countries.ua.parsing import _pair_names, _split_names


def test_split_names_multiple_comma_separated():
    assert _split_names("Іванов Іван, Петров Петро") == ["Іванов Іван", "Петров Петро"]


def test_split_names_empty():
    assert _split_names(None) == []
    assert _split_names("") == []


def test_split_names_applies_homoglyph_fix():
    # Latin "i" (U+0069) between Cyrillic neighbors should become the
    # Cyrillic "і" (U+0456), same rule as elsewhere in normalize.py.
    raw = "Дан" + chr(0x69) + "ло Іванов"
    assert _split_names(raw) == ["Даніло Іванов"]


def test_pair_names_equal_counts():
    assert _pair_names("Чайковський Роман", "Roman Chaikovskyi") == [
        {"uk": "Чайковський Роман", "en": "Roman Chaikovskyi"}
    ]


def test_pair_names_uneven_counts_pads_with_none():
    result = _pair_names("Іванов Іван, Петров Петро", "Ivan Ivanov")
    assert result == [
        {"uk": "Іванов Іван", "en": "Ivan Ivanov"},
        {"uk": "Петров Петро", "en": None},
    ]


def test_pair_names_both_missing():
    assert _pair_names(None, None) == []


def test_pair_names_only_en_present():
    assert _pair_names(None, "Roman Chaikovskyi") == [{"uk": None, "en": "Roman Chaikovskyi"}]
