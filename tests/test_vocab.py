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


def test_karbovanets_is_spelled_both_ways_by_nbu():
    # Real anomaly from series "Видатні особистості України": seven cards
    # priced in "карб" where the dictionary only knew "крб". Same
    # currency, so it is a variant of the existing code, not a new one.
    assert vocab.lookup(vocab.UNIT, "крб") == "karbovanets"
    assert vocab.lookup(vocab.UNIT, "карб") == "karbovanets"
    assert vocab.lookup(vocab.UNIT, "карб.") == "karbovanets"


def test_cupronickel_is_not_nickel_silver():
    # Same series, five cards of "мельхіор". It is copper-nickel;
    # "нейзильбер" is copper-nickel-zinc, and NBU uses both words in that
    # one series for different coins -- so they are different codes.
    assert vocab.lookup(vocab.MATERIALS, "мельхіор") == "cupronickel"
    assert vocab.lookup(vocab.MATERIALS, "нейзильбер") == "nickel_silver"
    assert vocab.lookup(vocab.MATERIALS, "мельхіор") != vocab.lookup(
        vocab.MATERIALS, "нейзильбер"
    )


def test_cupronickel_is_a_base_metal_for_the_loader():
    # metal_kind is an enum in coin_keeper (precious/base/unknown), so a
    # new material must land on one of those without a migration.
    from collector.countries.ua.load_cards import metal_kind_of

    assert metal_kind_of("cupronickel") == "base"


def test_proof_like_is_its_own_quality():
    # Real anomaly from "Видатні особистості України": NBU grades 26
    # coins "пруф" and 5 "пруф-лайк" in that one series, so they are
    # different tiers. Merging them would claim a quality the issuer
    # never gave those five coins.
    assert vocab.lookup(vocab.QUALITY, "пруф") == "proof"
    assert vocab.lookup(vocab.QUALITY, "пруф-лайк") == "proof_like"
    assert vocab.lookup(vocab.QUALITY, "пруф-лайк") != vocab.lookup(vocab.QUALITY, "пруф")


def test_zinc_alloy_is_its_own_material():
    # NBU's cheap commemorative metal since 2018. It is not нейзильбер:
    # that one is copper-nickel-zinc, this one is zinc-based, and
    # "Збройні сили України" carries 14 of the first against 11 of the
    # second -- the same series, different coins.
    assert vocab.lookup(vocab.MATERIALS, "сплав на основі цинку") == "zinc_alloy"
    assert vocab.lookup(vocab.MATERIALS, "сплав на основі цинку") != vocab.lookup(
        vocab.MATERIALS, "нейзильбер"
    )


def test_zinc_alloy_is_a_base_metal_for_the_loader():
    from collector.countries.ua.load_cards import metal_kind_of

    assert metal_kind_of("zinc_alloy") == "base"


def test_zinc_alloy_survives_the_homoglyph_NBU_puts_in_it():
    # "основі" with a Latin i, the typo NBU makes across its own pages.
    raw = "сплав на основ" + chr(0x69) + " цинку"
    assert vocab.lookup(vocab.MATERIALS, raw) == "zinc_alloy"
