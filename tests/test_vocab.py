from collector.countries.ua import vocab


def test_lookup_material_exact():
    assert vocab.lookup(vocab.MATERIALS, "срібло") == "silver"


def test_lookup_is_case_and_whitespace_insensitive():
    assert vocab.lookup(vocab.MATERIALS, "  СРІБЛО  ") == "silver"


def test_lookup_tolerates_homoglyphs():
    # "срібло" with a Latin "i" (U+0069) standing in for the Cyrillic "і"
    # (U+0456) -- same style of typo NBU makes in the metal-suffix parens
    # (see normalize.py), shouldn't cause a false miss.
    raw = "ср" + chr(0x69) + "бло"
    assert vocab.lookup(vocab.MATERIALS, raw) == "silver"


def test_lookup_unknown_value_returns_none():
    assert vocab.lookup(vocab.MATERIALS, "невідомий метал") is None


def test_lookup_empty_or_none_returns_none():
    assert vocab.lookup(vocab.MATERIALS, "") is None
    assert vocab.lookup(vocab.MATERIALS, None) is None


def test_quality_synonyms_map_to_same_code():
    # "звичайна" and "анциркулейтед" are two different NBU raw strings
    # for the same finish tier -- both map to "uncirculated" on purpose.
    assert vocab.lookup(vocab.QUALITY, "звичайна") == "uncirculated"
    assert vocab.lookup(vocab.QUALITY, "анциркулейтед") == "uncirculated"


def test_unit_dot_variants():
    assert vocab.lookup(vocab.UNIT, "грн") == "hryvnia"
    assert vocab.lookup(vocab.UNIT, "грн.") == "hryvnia"


def test_edge_singular_and_plural_variants_same_code():
    # Real anomaly from series "Античні пам'ятки України" (nbu:482): NBU
    # used the plural "написами" where the pilot series only ever showed
    # the singular "написом" -- both belong under plain_incuse_lettering.
    assert vocab.lookup(vocab.EDGE, "гладкий із заглибленим написом") == "plain_incuse_lettering"
    assert vocab.lookup(vocab.EDGE, "гладкий із заглибленими написами") == "plain_incuse_lettering"
