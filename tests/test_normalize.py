from collector.countries.ua.normalize import (
    fix_homoglyphs,
    fix_stray_apostrophe,
    normalize_match,
    normalize_title,
    split_packaging,
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


def test_normalize_title_strips_bimetallic_suffix():
    # NBU spells the bimetallic material code with two letters ("бн") where
    # every other material gets one (н/с/з/м) -- must not be left behind.
    clean, suffix = normalize_title("На межі тисячоліть  (бн)")
    assert clean == "На межі тисячоліть"
    assert suffix == "бн"


def test_normalize_title_keeps_a_real_parenthetical_alt_name():
    # "(Кролика)" is an alternate name (nbu:1520, "Рік Кота (Кролика)"),
    # not a material-suffix code -- must survive untouched.
    clean, suffix = normalize_title("Рік Кота (Кролика)")
    assert clean == "Рік Кота (Кролика)"
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


# ---------------------------------------------------------------------- #
# packaging tail
# ---------------------------------------------------------------------- #


def test_split_packaging_folds_the_upakovtsi_pakovanni_spelling():
    # Both sites use the two spellings interchangeably for the same
    # packaging, so the key must not distinguish them.
    _, tail_a, key_a = split_packaging("Захисниці у сувенірній упаковці")
    _, tail_b, key_b = split_packaging("Захисниці у сувенірному пакованні")
    assert tail_a == "у сувенірній упаковці"
    assert tail_b == "у сувенірному пакованні"
    assert key_a == key_b == "сувенірн"


def test_split_packaging_keeps_the_base_title():
    base, _, _ = split_packaging("В єдності - сила у сувенірній упаковці")
    assert base == "В єдності - сила"


def test_split_packaging_no_tail():
    assert split_packaging("Захисниці") == ("Захисниці", None, None)


def test_split_packaging_distinguishes_gift_from_souvenir_packaging():
    _, _, key = split_packaging("Назва у подарунковому пакованні")
    assert key == "подарунков"


def test_split_packaging_leaves_a_futlyar_alone():
    # "у футлярі" is part of the item's real name (a set in a case), not a
    # packaged-variant marker -- see the comment on _PACKAGING_RE.
    title = "Набір із двох срібних монет у футлярі"
    assert split_packaging(title) == (title, None, None)


def test_normalize_title_quoted_name_with_suffix_before_packaging_tail():
    # nbu:1718 exactly as NBU writes it: quotes wrap only the coin's own
    # name, and the metal suffix sits between the closing quote and the
    # packaging tail -- so neither the quote pair nor the suffix is at the
    # string's edge.
    clean, suffix = normalize_title(
        '"Країна супергероїв. Дякуємо зброярам!" (н) у сувенірному пакованні'
    )
    assert clean == "Країна супергероїв. Дякуємо зброярам! у сувенірному пакованні"
    assert suffix == "н"


def test_normalize_title_suffix_after_the_packaging_tail():
    # The two tails come in either order; both must be peeled.
    clean, suffix = normalize_title('«Захисниці» у сувенірній упаковці (с)')
    assert clean == "Захисниці у сувенірній упаковці"
    assert suffix == "с"


def test_normalize_match_drops_quote_marks():
    # ua-coins row 2659 keeps NBU's straight quotes; our parsed title has
    # them stripped. The two must still compare equal.
    ua_coins = '"Країна супергероїв. Дякуємо зброярам!" у сувенірному пакованні'
    ours = "Країна супергероїв. Дякуємо зброярам! у сувенірному пакованні"
    assert normalize_match(ua_coins) == normalize_match(ours)


def test_normalize_match_keeps_apostrophes():
    # Quote marks are noise, but an apostrophe carries a letter's worth of
    # meaning -- it is folded to one character, not dropped.
    assert normalize_match("Пам’ятки") != normalize_match("Памятки")


def test_normalize_match_drops_an_apostrophe_used_as_an_outer_quote():
    # ua-coins row 3003 wraps the whole name in ’…’ because the name
    # already contains straight quotes; NBU writes it unwrapped. Same
    # coin, and the only difference is those two characters.
    ua_coins = '’Українські народні казки. "Кирило Кожум’яка"’ у сувенірному пакованні'
    nbu = 'Українські народні казки. "Кирило Кожум’яка" у сувенірному пакованні'
    assert normalize_match(ua_coins) == normalize_match(nbu)


def test_normalize_match_drops_a_grave_accent_used_as_an_outer_quote():
    # NBU's own side of the same habit: `Вотан` in the Melitopol coin,
    # against ua-coins' unquoted form.
    nbu = "Прорив німецької лінії оборони `Вотан` та визволення Мелітополя"
    plain = "Прорив німецької лінії оборони Вотан та визволення Мелітополя"
    assert normalize_match(nbu) == normalize_match(plain)


def test_the_apostrophe_inside_the_wrapped_name_survives_the_wrapper_going():
    # The rule is positional, so both uses of ’ appear in this one title:
    # the outer pair goes, the one in Кожум’яка stays.
    wrapped = '’Кирило Кожум’яка’'
    assert normalize_match(wrapped) == "Кирило Кожум’яка"


def test_normalize_match_keeps_an_apostrophe_at_a_word_boundary_of_neither_kind():
    # An apostrophe only reads as a letter between two of them: "Кожум’яка"
    # keeps it, a trailing one is punctuation and goes.
    assert normalize_match("Кожум’яка") == "Кожум’яка"
    assert normalize_match("Кожумяка’") == "Кожумяка"


def test_normalize_match_still_folds_the_english_possessive():
    # CHILDREN’S ZODIAC -- letters both sides, so it is an apostrophe.
    assert normalize_match("CHILDREN'S ZODIAC") == normalize_match("CHILDREN’S ZODIAC")
    assert "’" in normalize_match("CHILDREN'S ZODIAC")
