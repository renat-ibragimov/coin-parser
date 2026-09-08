from collector.countries.ua.normalize import (
    fix_homoglyphs,
    fix_stray_apostrophe,
    normalize_match,
    normalize_title,
    strip_quotes,
)


def test_strip_quotes_various_marks():
    assert strip_quotes('«Різдво Христове»') == "Різдво Христове"
    assert strip_quotes('"Гетьманські столиці"') == "Гетьманські столиці"
    assert strip_quotes("Без лапок") == "Без лапок"


def test_strip_quotes_does_not_mangle_embedded_quote_pair():
    # Real bug: nbu:146's title has a backtick-quote pair wrapping just
    # the ship's name, not the whole title -- the opening backtick isn't
    # at the string's edge, so naive str.strip(chars) only ate the
    # trailing one, leaving "Криголам `Капітан Бєлоусов" (dangling
    # unmatched backtick). Neither backtick is at BOTH edges here, so
    # strip_quotes must leave the string untouched, symmetrically.
    title = "Криголам `Капітан Бєлоусов`"
    assert strip_quotes(title) == title


def test_fix_stray_apostrophe_inside_word():
    # Real NBU data: nbu:591's en title uses a backtick for the
    # possessive apostrophe in "Catherine's".
    assert fix_stray_apostrophe("Catherine`s Glory Ship of the Line") == (
        "Catherine’s Glory Ship of the Line"
    )


def test_fix_stray_apostrophe_leaves_quote_pair_untouched():
    # The backticks here are quote marks (space before the opening one,
    # string edge after the closing one) -- neither neighbor pair is
    # letter-letter, so this must NOT touch them.
    title = "Icebreaker `Captain Belousov`"
    assert fix_stray_apostrophe(title) == title


def test_normalize_title_embedded_quote_pair_stays_as_is():
    # End-to-end: normalize_title must not produce a dangling backtick.
    clean, suffix = normalize_title("Криголам `Капітан Бєлоусов`  (c)")
    assert clean == "Криголам `Капітан Бєлоусов`"
    assert suffix == "с"


def test_normalize_title_fixes_stray_apostrophe():
    clean, _ = normalize_title("Catherine`s Glory Ship of the Line")
    assert clean == "Catherine’s Glory Ship of the Line"


def test_normalize_title_strips_metal_suffix():
    clean, suffix = normalize_title("Різдво Христове  (с)")
    assert clean == "Різдво Христове"
    assert suffix == "с"


def test_normalize_title_latin_c_suffix_maps_to_cyrillic_s():
    # NBU renders the срібло suffix with a Latin "c" instead of Cyrillic "с".
    clean, suffix = normalize_title("Хрещення Русі  (c)")
    assert clean == "Хрещення Русі"
    assert suffix == "с"


def test_normalize_title_no_suffix():
    clean, suffix = normalize_title("Christmas")
    assert clean == "Christmas"
    assert suffix is None


def test_normalize_title_collapses_whitespace_and_quotes():
    clean, suffix = normalize_title('«Гетьманські   століці»   (з)')
    assert clean == "Гетьманські століці"
    assert suffix == "з"


def test_fix_homoglyphs_replaces_latin_letter_between_cyrillic_neighbors():
    # Latin "i" inside a Cyrillic word ("рокiв").
    assert fix_homoglyphs("рокiв") == "років"


def test_fix_homoglyphs_leaves_latin_words_untouched():
    assert fix_homoglyphs("Sea Baby") == "Sea Baby"


def test_fix_homoglyphs_mixed_sentence():
    assert fix_homoglyphs("100 рокiв УHР") == "100 років УНР"


def test_fix_homoglyphs_word_final_latin_letter():
    # Real NBU data: nbu:88's title ends in a Latin "e" right before a
    # word boundary (space), not another Cyrillic letter.
    assert fix_homoglyphs("Різдво Христовe") == "Різдво Христове"


def test_fix_homoglyphs_leaves_pure_cyrillic_untouched():
    assert fix_homoglyphs("єднання Європи") == "єднання Європи"


def test_fix_homoglyphs_full_raw_title_with_suffix():
    # End-to-end shape as scraped: trailing Latin e, double space, suffix.
    assert fix_homoglyphs("Різдво Христовe  (з)") == "Різдво Христове  (з)"


def test_normalize_match_apostrophe_variants_are_equal():
    # Real NBU inconsistency: grave accent vs proper right single quote
    # vs a straight apostrophe, all meaning the same word.
    grave = "Античні пам`ятки України"
    curly = "Античні пам’ятки України"
    straight = "Античні пам'ятки України"
    modifier = "Античні памʼятки України"
    assert normalize_match(grave) == normalize_match(curly) == normalize_match(straight) == normalize_match(modifier)


def test_normalize_match_distinguishes_different_names():
    assert normalize_match("Пам’ятки архітектури України") != normalize_match(
        "Пам’ятки давніх культур України"
    )


def test_normalize_match_applies_homoglyph_fix():
    # Latin "i" (U+0069) standing in for the Cyrillic "і" (U+0456) in
    # "Знаки зодіаку" -- a real series name.
    raw = "Знаки зод" + chr(0x69) + "аку"
    assert normalize_match(raw) == normalize_match("Знаки зодіаку")
