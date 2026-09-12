from collector.countries.ua.packaging import find_packaging_pairs


def _card(source_id, title, year, weight, diameter):
    return {
        "source_id": source_id,
        "titles": {"uk": title},
        "year": year,
        "weight_grams": weight,
        "diameter_mm": diameter,
    }


def test_pairs_bare_and_packaged_of_same_weight_class():
    cards = [
        _card("nbu:1", "Рік Тигра", 2021, 16.54, 35.0),
        _card("nbu:2", "Рік Тигра у сувенірній упаковці", 2021, 16.54, 35.0),
    ]
    assert find_packaging_pairs(cards) == {"nbu:2": "nbu:1"}


def test_does_not_pair_across_different_years_same_title():
    # Real case: "Соломія Крушельницька" minted in 1997 and again in
    # 2022 -- same title, 25 years apart, not a packaging pair.
    cards = [
        _card("nbu:1", "Соломія Крушельницька", 1997, 14.35, 33.0),
        _card("nbu:2", "Соломія Крушельницька у сувенірній упаковці", 2022, 12.8, 31.0),
    ]
    assert find_packaging_pairs(cards) == {}


def test_does_not_pair_a_different_weight_class_premium_edition():
    # Real case: a 16.54g/35mm nickel-silver "standard" next to a
    # 31.1g/38.6mm silver "premium" edition of the same theme and year
    # -- two different NBU products, not a box.
    cards = [
        _card("nbu:1", "Захисниці", 2023, 31.1, 38.6),
        _card("nbu:2", "Захисниці у сувенірній упаковці", 2023, 16.54, 35.0),
    ]
    assert find_packaging_pairs(cards) == {}


def test_packaged_card_with_no_bare_twin_is_left_unpaired():
    cards = [
        _card("nbu:1", "Батьківське щастя у подарунковому пакованні", 2024, 15.55, 33.0),
        _card("nbu:2", "Батьківське щастя у сувенірному пакованні", 2024, 16.54, 35.0),
    ]
    assert find_packaging_pairs(cards) == {}


def test_mintage_difference_does_not_block_a_pair():
    cards = [
        _card("nbu:1", "Ой у лузі червона калина", 2022, 16.54, 35.0),
        _card("nbu:2", "Ой у лузі червона калина у сувенірній упаковці", 2022, 16.54, 35.0),
    ]
    cards[0]["mintage"] = {"announced": 105000, "actual": 105000}
    cards[1]["mintage"] = {"announced": 250000, "actual": 250000}
    assert find_packaging_pairs(cards) == {"nbu:2": "nbu:1"}
