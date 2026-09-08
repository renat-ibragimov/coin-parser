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


# ---------------------------------------------------------------------- #
# _big_image_url -- the full-resolution original behind a listing thumbnail
# ---------------------------------------------------------------------- #

from selectolax.parser import HTMLParser  # noqa: E402

from collector.countries.ua.parsing import _big_image_url, _parse_card_node  # noqa: E402

WRAPPED = (
    '<div class="img"><a class="big-image" href="/files/coins_images/482a.png?v=19">'
    '<img src="/media/coins/482/avers.jpg?v=19"></a></div>'
)
BARE = '<div class="img"><img src="/media/coins/438/avers.jpg?v=19"></div>'


def _first_img(html):
    return HTMLParser(html).css_first("img")


def test_big_image_url_reads_the_enclosing_lightbox_link():
    assert (
        _big_image_url(_first_img(WRAPPED))
        == "https://bank.gov.ua/files/coins_images/482a.png?v=19"
    )


def test_big_image_url_is_none_for_a_bare_thumbnail():
    # Not every card links a full-resolution original; nbu:438 does not.
    assert _big_image_url(_first_img(BARE)) is None


def test_big_image_url_ignores_an_unrelated_enclosing_link():
    html = '<div class="img"><a href="/somewhere-else"><img src="/thumb.jpg"></a></div>'
    assert _big_image_url(_first_img(html)) is None


def _card(images_html, source_id="482"):
    return HTMLParser(
        f'<div class="box row" id="coin_{source_id}">'
        f'<div class="img-container">{images_html}</div>'
        f'<div class="title">Тест</div></div>'
    ).css_first("div.box.row")


def test_a_side_with_a_link_and_a_side_without_stay_paired():
    # The reason this is read per thumbnail instead of by collecting every
    # a.big-image on the card: collecting would slide the obverse's link
    # onto the reverse whenever only one of the two has one.
    node = _card(BARE.replace("438", "482") + WRAPPED.replace("avers", "revers"))
    parsed = _parse_card_node(node, "uk")
    assert parsed["obverse_big_img"] is None
    assert parsed["reverse_big_img"].endswith("482a.png?v=19")
    assert parsed["obverse_img"].endswith("avers.jpg?v=19")
    assert parsed["reverse_img"].endswith("revers.jpg?v=19")


def test_a_card_with_no_links_at_all_parses():
    parsed = _parse_card_node(_card(BARE + BARE), "uk")
    assert parsed["obverse_big_img"] is None
    assert parsed["reverse_big_img"] is None
    assert parsed["obverse_img"] is not None
