"""Pure-logic tests for photos.py -- synthetic images only, no network, no
real coin photos needed. (tests/test_photo_corpus.py runs the same code
over the real staging corpus when it is present.)

Covers the three decisions this module owns: which candidate wins a role,
whether a finished cut is trustworthy, and what gets written to disk.
"""

import math

import numpy as np
import pytest
from PIL import Image, ImageDraw

from collector.countries.ua.photos import (
    LOW_RES_THRESHOLD,
    _assign_roles,
    _fetch_nbu_candidates,
    OUTPUT_SIZES,
    PAIR_SILHOUETTE_MIN,
    _outline_dip_fraction,
    _is_truecolour,
    _Processed,
    _rank_key,
    _role_from_text,
    _silhouette_iou,
    _write_sizes,
)
from collector.core import coin_classifier


def _disc(size=400, radius=170, nibbles=0, ellipse_aspect=1.0) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    c = size // 2
    ry = int(radius * ellipse_aspect)
    draw.ellipse([c - radius, c - ry, c + radius, c + ry], fill=(200, 200, 200, 255))
    for i in range(nibbles):
        angle = 2 * math.pi * i / nibbles
        bx, by = c + radius * math.cos(angle), c + ry * math.sin(angle)
        draw.ellipse([bx - 7, by - 7, bx + 7, by + 7], fill=(0, 0, 0, 0))
    return img


# ---------------------------------------------------------------------- #
# _outline_dip_fraction -- the rim-erosion number that goes in photos.json
# ---------------------------------------------------------------------- #


def test_dip_is_zero_on_a_clean_disc():
    assert _outline_dip_fraction(_disc()) == 0.0


def test_dip_rises_on_a_chewed_rim():
    # The defect the old flood tolerance produced: shallow bites all round,
    # none deep enough to look like a bite on its own.
    assert _outline_dip_fraction(_disc(nibbles=40)) > 0.1


def test_dip_is_none_for_a_coin_that_is_not_round():
    # The measure is deviation from a circle, so on a pysanka or a
    # half-heart it would report the shape itself and land near 1.0.
    # Reporting no number is the only honest answer -- a number here reads
    # in the run report as "this coin was chewed to bits".
    assert _outline_dip_fraction(_disc(ellipse_aspect=0.53)) is None


def test_dip_is_none_when_there_is_no_object():
    assert _outline_dip_fraction(Image.new("RGBA", (100, 100), (0, 0, 0, 0))) is None


# ---------------------------------------------------------------------- #
# _silhouette_iou -- the two sides of one coin check each other
# ---------------------------------------------------------------------- #


def test_two_sides_of_one_coin_agree():
    # Healthy pairs in the real corpus measure 0.970-0.999.
    assert _silhouette_iou(_disc(), _disc(radius=150)) > PAIR_SILHOUETTE_MIN


def test_pair_check_survives_a_difference_in_framing_and_resolution():
    # The silhouettes are cropped to their bbox and rescaled, so the same
    # coin shot at another size and margin still matches itself.
    assert _silhouette_iou(_disc(size=400, radius=170), _disc(size=900, radius=300)) > 0.99


def test_a_side_cut_in_half_stops_matching_the_other():
    half = _disc()
    ImageDraw.Draw(half).rectangle([0, 0, 200, 400], fill=(0, 0, 0, 0))
    assert _silhouette_iou(_disc(), half) < PAIR_SILHOUETTE_MIN


def test_a_non_round_coin_still_matches_its_own_other_side():
    # The reason this check replaced an absolute roundness test: it makes no
    # assumption about shape, so an egg matches an egg.
    egg = _disc(ellipse_aspect=0.53)
    assert _silhouette_iou(egg, egg) == 1.0


def test_pair_check_on_an_empty_image_is_none():
    assert _silhouette_iou(_disc(), Image.new("RGBA", (100, 100), (0, 0, 0, 0))) is None


def test_half_heart_and_half_disc_are_indistinguishable_by_shape_alone():
    # Why the pair check exists at all. A coin cut clean in half and a
    # genuine half-heart coin have the same geometry, so no absolute shape
    # test can accept one and reject the other -- only comparing a coin
    # against its own other side can.
    half_disc = _disc()
    ImageDraw.Draw(half_disc).rectangle([0, 0, 200, 400], fill=(0, 0, 0, 0))
    alpha = np.array(half_disc.split()[-1])
    damaged = coin_classifier.classify_mask((alpha > 128).astype(np.uint8) * 255)
    assert abs(damaged.worst_aspect - 0.5) < 0.1


# ---------------------------------------------------------------------- #
# _rank_key -- (background class, shape verdict, -resolution)
# ---------------------------------------------------------------------- #


def _fake(klass, width, height, is_coin=True, truecolour=True):
    verdict = coin_classifier.Verdict(is_coin, 1, 1.0, 0.78, 1.0, 0.9, 0.99, "ok")
    return _Processed(
        candidate=None,
        img=Image.new("RGB", (width, height)),
        klass=klass,
        bg_verdict=None,
        coin_verdict=verdict,
        truecolour=truecolour,
    )


def test_rank_key_class_beats_everything():
    small_transparent = _fake(1, 300, 300)
    big_cuttable = _fake(2, 1200, 1200)
    assert sorted([big_cuttable, small_transparent], key=_rank_key)[0] is small_transparent


def test_rank_key_shape_verdict_beats_size_within_a_class():
    big_but_odd = _fake(2, 1200, 1200, is_coin=False)
    small_but_clean = _fake(2, 300, 300)
    assert sorted([big_but_odd, small_but_clean], key=_rank_key)[0] is small_but_clean


def test_rank_key_size_breaks_the_last_tie():
    small, big = _fake(2, 200, 200), _fake(2, 600, 600)
    assert sorted([small, big], key=_rank_key)[0] is big


def test_a_photo_the_classifier_dislikes_is_still_ranked_not_dropped():
    # The contract that cost nbu:161 its obverse when it was broken: a
    # failing shape verdict demotes a candidate, it never removes it.
    only_candidate = _fake(2, 600, 600, is_coin=False)
    assert sorted([only_candidate], key=_rank_key) == [only_candidate]


def test_colour_depth_breaks_a_tie_on_resolution():
    # nbu:482 has two candidates at an identical 1120x1120 with a
    # pixel-identical alpha channel: NBU's own full-resolution original is
    # an 8-bit palette PNG holding 230 colours, ua-coins' WebP of the same
    # image holds 48015. Before this term the tie fell to whichever source
    # happened to be fetched first.
    paletted = _fake(1, 1120, 1120, truecolour=False)
    full_colour = _fake(1, 1120, 1120)
    assert sorted([paletted, full_colour], key=_rank_key)[0] is full_colour


def test_resolution_past_the_top_tier_does_not_outrank_colour_depth():
    # Nothing above the largest OUTPUT_SIZES tier is ever written out, so
    # extra pixels there are free to no one and must not buy a worse image
    # the win.
    big_paletted = _fake(1, 1600, 1600, truecolour=False)
    full_colour = _fake(1, 1200, 1200)
    assert sorted([big_paletted, full_colour], key=_rank_key)[0] is full_colour


def test_resolution_below_the_top_tier_still_beats_colour_depth():
    # Below the cap the pixels are real: a 1200px palette image genuinely
    # serves a larger view than a 600px truecolour one.
    big_paletted = _fake(1, 1200, 1200, truecolour=False)
    small_full_colour = _fake(1, 600, 600)
    assert sorted([big_paletted, small_full_colour], key=_rank_key)[0] is big_paletted


def test_actual_resolution_is_the_final_tiebreak():
    # Two candidates equal on everything else: the genuinely larger source
    # wins, even though both are capped to the same tier on the way out.
    bigger = _fake(1, 1600, 1600)
    smaller = _fake(1, 1300, 1300)
    assert sorted([smaller, bigger], key=_rank_key)[0] is bigger


def test_class_still_beats_colour_depth():
    paletted_transparent = _fake(1, 600, 600, truecolour=False)
    full_colour_cuttable = _fake(2, 600, 600)
    ranked = sorted([full_colour_cuttable, paletted_transparent], key=_rank_key)
    assert ranked[0] is paletted_transparent


def test_is_truecolour_measures_pixels_not_mode():
    # An RGB file someone quantised before saving is the same loss as a
    # palette one, and only one of the two says so in its mode.
    gradient = Image.new("RGB", (300, 300))
    gradient.putdata([(x % 256, y % 256, (x + y) % 256) for y in range(300) for x in range(300)])
    assert _is_truecolour(gradient)
    assert not _is_truecolour(gradient.convert("P", palette=Image.Palette.ADAPTIVE, colors=64))
    assert not _is_truecolour(Image.new("RGB", (300, 300), (200, 170, 60)))


def test_rank_key_class_3_is_worst():
    ranked = sorted([_fake(3, 1200, 1200), _fake(2, 200, 200), _fake(1, 200, 200)], key=_rank_key)
    assert [p.klass for p in ranked] == [1, 2, 3]


# ---------------------------------------------------------------------- #
# _write_sizes -- tiers are labels, capped at what the source really has
# ---------------------------------------------------------------------- #


def _names(files):
    return [f["file"] for f in files]


def _sides(files):
    return [max(f["width"], f["height"]) for f in files]


def test_tiers_from_a_600px_source(tmp_path):
    files, _ = _write_sizes(Image.new("RGBA", (600, 600)), tmp_path, "obverse")
    assert _names(files) == ["obverse_300.webp", "obverse_600.webp"]
    assert _sides(files) == [300, 600]
    for f in files:
        assert (tmp_path / f["file"]).exists()


def test_tiers_from_a_1200px_source(tmp_path):
    files, low_res = _write_sizes(Image.new("RGBA", (1200, 1200)), tmp_path, "reverse")
    assert _names(files) == ["reverse_300.webp", "reverse_600.webp", "reverse_1200.webp"]
    assert _sides(files) == [300, 600, 1200]
    assert low_res is False


def test_a_source_stranded_between_tiers_fills_the_upper_one(tmp_path):
    # nbu:482 arrives at 1120px: 7% short of the 1200 label, 87% above the
    # 600 one. It used to collapse to 600 and throw away all 87% to avoid
    # inventing 7%. Now the 1200 label is written at its true 1120px.
    files, _ = _write_sizes(Image.new("RGBA", (1120, 1120)), tmp_path, "obverse")
    assert _names(files) == ["obverse_300.webp", "obverse_600.webp", "obverse_1200.webp"]
    assert _sides(files) == [300, 600, 1120]


def test_the_top_file_is_never_upscaled(tmp_path):
    files, _ = _write_sizes(Image.new("RGBA", (1120, 1120)), tmp_path, "obverse")
    with Image.open(tmp_path / "obverse_1200.webp") as saved:
        assert saved.size == (1120, 1120)


def test_a_source_above_the_top_tier_is_capped_at_it(tmp_path):
    files, _ = _write_sizes(Image.new("RGBA", (1600, 1600)), tmp_path, "obverse")
    assert _sides(files) == [300, 600, 1200]


def test_trimming_a_few_pixels_does_not_cost_a_tier(tmp_path):
    # cut_background trims to the alpha bbox, so one side of a coin lands at
    # 597px and the other at 600px purely from how much margin each photo
    # carried. Both must still offer the same filenames -- nbu:161's two
    # sides came out with different tier sets before.
    files, _ = _write_sizes(Image.new("RGBA", (591, 597)), tmp_path, "obverse", 600)
    assert _names(files) == ["obverse_300.webp", "obverse_600.webp"]
    assert _sides(files) == [300, 597]  # the label says 600, the file is 597


def test_a_small_source_is_written_once_not_once_per_tier(tmp_path):
    # Real case: old NBU series ship 200x200 originals. Capping every tier
    # at the source would otherwise write the same 200px image three times.
    files, low_res = _write_sizes(Image.new("RGBA", (200, 200)), tmp_path, "obverse")
    assert _names(files) == ["obverse_300.webp"]
    assert _sides(files) == [200]
    assert low_res is True
    with Image.open(tmp_path / "obverse_300.webp") as saved:
        assert saved.size == (200, 200)  # native, not upscaled to 300


def test_returned_sizes_are_strictly_increasing(tmp_path):
    for side in (200, 400, 600, 900, 1120, 1600):
        files, _ = _write_sizes(Image.new("RGBA", (side, side)), tmp_path, "obverse")
        sides = _sides(files)
        assert sides == sorted(set(sides)), side


def test_reported_dimensions_match_the_files_on_disk(tmp_path):
    # The contract that replaces "parse the number out of the filename".
    files, _ = _write_sizes(Image.new("RGBA", (1120, 840)), tmp_path, "reverse")
    for f in files:
        with Image.open(tmp_path / f["file"]) as saved:
            assert saved.size == (f["width"], f["height"])


def test_aspect_ratio_is_preserved(tmp_path):
    files, _ = _write_sizes(Image.new("RGBA", (1120, 560)), tmp_path, "reverse")
    for f in files:
        assert abs(f["width"] / f["height"] - 2.0) < 0.02


def test_low_res_is_judged_on_the_source_not_on_the_trimmed_result(tmp_path):
    # A 600x600 source trimmed to 600x598 is not a low-resolution photo,
    # and used to be flagged as one over two pixels of cropped margin.
    _, low_res = _write_sizes(Image.new("RGBA", (600, 598)), tmp_path, "reverse", 600)
    assert low_res is False


def test_low_res_still_fires_for_a_genuinely_small_source(tmp_path):
    _, low_res = _write_sizes(Image.new("RGBA", (400, 400)), tmp_path, "reverse", 400)
    assert low_res is True
    assert LOW_RES_THRESHOLD == 600


def test_output_sizes_constant():
    assert OUTPUT_SIZES == (300, 600, 1200)


# ---------------------------------------------------------------------- #
# _role_from_text
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,role",
    [
        ("Аверс Різдво Христове", "obverse"),
        ("Реверс Різдво Христове", "reverse"),
        ("nbu_obverse.jpg", "obverse"),
        ("nbu_reverse.jpg", "reverse"),
        ("uacoins_01.webp", None),
        ("Криголан Капітан Бєлоусов", None),
        (None, None),
    ],
)
def test_role_from_text(text, role):
    assert _role_from_text(text) == role


# ---------------------------------------------------------------------- #
# _fetch_nbu_candidates -- the listing thumbnail plus the lightbox original
# ---------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


class _FakeClient:
    """Serves the URLs it was given and 404s the rest, recording the order."""

    def __init__(self, available):
        self.available = available
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        if url not in self.available:
            raise RuntimeError("404 Not Found")
        return _FakeResponse(self.available[url])


class _NoPacer:
    def wait(self):
        pass


def _png_bytes(side, colour=(200, 170, 60)):
    import io

    buf = io.BytesIO()
    Image.new("RGBA", (side, side), colour + (255,)).save(buf, "PNG")
    return buf.getvalue()


def _card_images(**urls):
    return {"source_id": "nbu:482", "images": urls}


def test_the_full_size_original_is_fetched_alongside_the_thumbnail(tmp_path):
    thumb, big = "https://x/avers.jpg", "https://x/482a.png"
    client = _FakeClient({thumb: _png_bytes(200), big: _png_bytes(1120)})
    errors = []
    got = _fetch_nbu_candidates(
        client,
        _NoPacer(),
        _card_images(obverse_url=thumb, obverse_big_url=big),
        tmp_path,
        False,
        errors,
    )
    assert [c.width for c in got] == [200, 1120]
    assert [c.file for c in got] == ["nbu_obverse.jpg", "nbu_big_obverse.png"]
    assert errors == []


def test_a_dead_lightbox_link_is_not_an_error(tmp_path):
    # NBU links files it no longer serves (nbu:438's 404s). The thumbnail is
    # already in the pool, so nothing is lost and the card must not be
    # reported as a failed fetch.
    thumb = "https://x/avers.jpg"
    client = _FakeClient({thumb: _png_bytes(200)})
    errors = []
    got = _fetch_nbu_candidates(
        client,
        _NoPacer(),
        _card_images(obverse_url=thumb, obverse_big_url="https://x/gone.png"),
        tmp_path,
        False,
        errors,
    )
    assert [c.file for c in got] == ["nbu_obverse.jpg"]
    assert errors == []


def test_a_missing_thumbnail_is_still_an_error(tmp_path):
    client = _FakeClient({})
    errors = []
    _fetch_nbu_candidates(
        client, _NoPacer(), _card_images(obverse_url="https://x/gone.jpg"), tmp_path, False, errors
    )
    assert len(errors) == 1


def test_a_card_with_no_lightbox_link_asks_for_nothing_extra(tmp_path):
    thumb = "https://x/avers.jpg"
    client = _FakeClient({thumb: _png_bytes(200)})
    _fetch_nbu_candidates(
        client, _NoPacer(), _card_images(obverse_url=thumb, obverse_big_url=None), tmp_path, False, []
    )
    assert client.requested == [thumb]


def test_the_full_size_filename_carries_its_role(tmp_path):
    # Roles are assigned from the filename, so the extra candidate has to
    # keep saying which side it is.
    assert _role_from_text("nbu_big_obverse.png") == "obverse"
    assert _role_from_text("nbu_big_reverse.png") == "reverse"


# ---------------------------------------------------------------------- #
# _assign_roles -- the caption is a label, not proof
# ---------------------------------------------------------------------- #


def _cand(file, alt=None, is_coin=True, klass=1, solidity=0.99):
    from collector.countries.ua.photos import FetchCandidate

    verdict = coin_classifier.Verdict(
        is_coin, 1, solidity, 0.78, 1.0, 0.9, 0.99, "ok" if is_coin else "boxy object in frame"
    )
    return _Processed(
        candidate=FetchCandidate(file=file, url="", alt=alt, source="ua_coins", width=0, height=0, bytes=0),
        img=Image.new("RGB", (1600, 1600)),
        klass=klass,
        bg_verdict=None,
        coin_verdict=verdict,
        truecolour=True,
    )


def _files(pool):
    return [p.candidate.file for p in pool]


def test_a_correct_caption_keeps_its_role():
    pools, unassigned, _ = _assign_roles(
        [
            _cand("nbu_obverse.jpg"),
            _cand("nbu_reverse.jpg"),
            _cand("uacoins_03.webp", alt="... - додаткове фото"),
        ]
    )
    assert _files(pools["obverse"]) == ["nbu_obverse.jpg"]
    assert _files(pools["reverse"]) == ["nbu_reverse.jpg"]
    assert _files(unassigned) == ["uacoins_03.webp"]


def test_a_caption_on_packaging_loses_the_role_to_the_coin_underneath():
    # nbu:1561's real shape: every "Аверс"/"Реверс" on the card is a scan
    # of the blister, and the coin sides are unnamed "додаткове фото".
    pools, unassigned, notes = _assign_roles(
        [
            _cand("nbu_obverse.jpg", is_coin=False, klass=3),
            _cand("nbu_reverse.jpg", is_coin=False, klass=3),
            _cand("uacoins_01.webp", alt="Аверс ... у сувенірній упаковці", is_coin=False, klass=3),
            _cand("uacoins_02.webp", alt="Реверс ... у сувенірній упаковці", is_coin=False, klass=3),
            _cand("uacoins_03.webp", alt="... - додаткове фото"),
            _cand("uacoins_04.webp", alt="... - додаткове фото"),
        ]
    )
    assert pools["obverse"][-1].candidate.file == "uacoins_03.webp"
    assert pools["reverse"][-1].candidate.file == "uacoins_04.webp"
    assert unassigned == []
    assert any("shape overrides the caption" in n for n in notes)


def test_the_overruled_captions_stay_in_the_pool_as_fallbacks():
    # Nothing is ever removed from a pool -- the rescue is appended, and
    # outranks the blister on background class anyway.
    pools, _, _ = _assign_roles(
        [
            _cand("uacoins_01.webp", alt="Аверс ...", is_coin=False, klass=3),
            _cand("uacoins_03.webp", alt="... - додаткове фото"),
        ]
    )
    assert _files(pools["obverse"]) == ["uacoins_01.webp", "uacoins_03.webp"]
    assert sorted(pools["obverse"], key=_rank_key)[0].candidate.file == "uacoins_03.webp"


def test_a_rejected_caption_keeps_its_role_when_nothing_better_is_offered():
    # Not a gate: a photo the classifier dislikes still ships when it is
    # all the card has (the nbu:161 contract).
    pools, _, notes = _assign_roles(
        [
            _cand("nbu_obverse.jpg", is_coin=False, klass=3),
            _cand("nbu_reverse.jpg", is_coin=False, klass=3),
            _cand("uacoins_05.webp", alt="Буклет, сторінка 1", is_coin=False, klass=3),
        ]
    )
    assert _files(pools["obverse"]) == ["nbu_obverse.jpg"]
    assert _files(pools["reverse"]) == ["nbu_reverse.jpg"]
    assert not any("shape overrides" in n for n in notes)


def test_the_booklet_pages_are_not_mistaken_for_the_coin():
    # nbu:1589 carries two extra gallery images the classifier rejects;
    # only the two it accepts may take the roles.
    pools, unassigned, _ = _assign_roles(
        [
            _cand("uacoins_01.webp", alt="Аверс ...", is_coin=False, klass=3),
            _cand("uacoins_02.webp", alt="Реверс ...", is_coin=False, klass=3),
            _cand("uacoins_03.webp", alt="... - додаткове фото"),
            _cand("uacoins_04.webp", alt="... - додаткове фото"),
            _cand("uacoins_05.webp", alt="Буклет, сторінка 1", is_coin=False, klass=3),
            _cand("uacoins_06.webp", alt="Буклет, сторінка 2", is_coin=False, klass=3),
        ]
    )
    assert pools["obverse"][-1].candidate.file == "uacoins_03.webp"
    assert pools["reverse"][-1].candidate.file == "uacoins_04.webp"
    assert _files(unassigned) == ["uacoins_05.webp", "uacoins_06.webp"]


def test_the_rescued_pair_is_split_by_gallery_order_not_by_score():
    # score() ranks how cleanly one object separates from its background
    # and explicitly does not identify sides; on nbu:1561 the two sides
    # differ by 0.003 and the reverse scored higher. Order decides.
    obverse = _cand("uacoins_03.webp", alt="додаткове фото", solidity=0.980)
    reverse = _cand("uacoins_04.webp", alt="додаткове фото", solidity=0.995)
    assert coin_classifier.score(reverse.coin_verdict) > coin_classifier.score(obverse.coin_verdict)
    pools, _, _ = _assign_roles(
        [_cand("uacoins_01.webp", alt="Аверс ...", is_coin=False, klass=3), obverse, reverse]
    )
    assert pools["obverse"][-1].candidate.file == "uacoins_03.webp"
    assert pools["reverse"][-1].candidate.file == "uacoins_04.webp"


def test_an_unlabelled_card_still_falls_back_to_the_geometry_tiebreak():
    # No caption anywhere and nothing the classifier accepts: the roles
    # would go empty, so the best of a bad lot is taken -- by score here,
    # since there is no accepted candidate to order.
    pools, _, notes = _assign_roles(
        [
            _cand("uacoins_01.webp", is_coin=False, klass=3, solidity=0.50),
            _cand("uacoins_02.webp", is_coin=False, klass=3, solidity=0.90),
        ]
    )
    assert pools["obverse"][0].candidate.file == "uacoins_02.webp"
    assert pools["reverse"][0].candidate.file == "uacoins_01.webp"
    assert all("geometry tiebreak" in n for n in notes if "filled by" in n)


def test_no_candidates_at_all_leaves_both_roles_empty():
    pools, unassigned, _ = _assign_roles([])
    assert pools == {"obverse": [], "reverse": []}
    assert unassigned == []
