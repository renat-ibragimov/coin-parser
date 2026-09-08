"""Pure-logic tests for coin_classifier.py's shape gate -- synthetic masks
only, no network, no real coin photos needed.

The gate has to hold two lines at once, and they pull in opposite
directions. It must pass coins that are not discs (the NBU catalog holds an
egg-shaped pysanka and a heart struck as two half-heart coins, and the
shapes will keep coming), and it must still reject packaging (a blister,
a slab, a tube, a roll). What it must NOT do is what the old circularity
test did: judge roundness, which rejected the odd shapes from one side and
collapsed on ordinary round coins with blown-out highlights from the other.

Every shape below is measured, not assumed -- the numbers in the assertions
are the ones the real corpus produces (see tests/test_photo_corpus.py for
the same bar applied to the actual photos).
"""

import numpy as np
import pytest
from PIL import Image, ImageDraw

from collector.core.coin_classifier import (
    MAX_EXTENT,
    MIN_ASPECT,
    MIN_SOLIDITY,
    Verdict,
    classify,
    classify_mask,
    score,
)

SIZE = 400
FG = (200, 200, 200, 255)


def _mask(img: Image.Image) -> np.ndarray:
    return (np.array(img.split()[-1]) > 128).astype(np.uint8) * 255


def _blank() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _disc(radius: int = 170) -> Image.Image:
    img, draw = _blank()
    c = SIZE // 2
    draw.ellipse([c - radius, c - radius, c + radius, c + radius], fill=FG)
    return img


def _ellipse(aspect: float) -> Image.Image:
    """A convex non-round coin, parameterised by the ratio that decides its
    fate. The real half-heart reference measures aspect 0.526."""
    img, draw = _blank()
    c = SIZE // 2
    half_w, half_h = 170, int(170 * aspect)
    draw.ellipse([c - half_w, c - half_h, c + half_w, c + half_h], fill=FG)
    return img


def _egg() -> Image.Image:
    """A pysanka: wider at the bottom than the top, convex, clearly not a
    circle. The real reference measures solidity 0.998, extent 0.762,
    aspect 0.733."""
    img, draw = _blank()
    draw.ellipse([120, 150, 280, 370], fill=FG)
    draw.ellipse([135, 60, 265, 250], fill=FG)
    return img


def _rectangle(x0=60, y0=130, x1=340, y1=270) -> Image.Image:
    """A blister, a slab or a certificate -- the thing the gate exists for."""
    img, draw = _blank()
    draw.rectangle([x0, y0, x1, y1], fill=FG)
    return img


def _tube() -> Image.Image:
    img, draw = _blank()
    draw.rectangle([170, 40, 230, 360], fill=FG)
    return img


def _roll() -> Image.Image:
    img, draw = _blank()
    draw.rectangle([40, 175, 360, 225], fill=FG)
    draw.ellipse([10, 175, 70, 225], fill=FG)
    draw.ellipse([330, 175, 390, 225], fill=FG)
    return img


# ---------------------------------------------------------------------- #
# What must pass: coins, whatever their shape
# ---------------------------------------------------------------------- #


def test_disc_passes():
    verdict = classify_mask(_mask(_disc()))
    assert verdict.is_coin
    assert verdict.reason == "ok"


def test_egg_shaped_coin_passes():
    # The pysanka. Under the old roundness test this was rejected outright.
    verdict = classify_mask(_mask(_egg()))
    assert verdict.is_coin
    assert verdict.worst_extent <= MAX_EXTENT
    assert verdict.worst_aspect >= MIN_ASPECT


def test_half_heart_aspect_passes():
    # The decisive number on the real half-heart reference is its aspect,
    # 0.526 -- comfortably the narrowest coin in the corpus and still a coin.
    verdict = classify_mask(_mask(_ellipse(aspect=0.53)))
    assert verdict.is_coin


def test_odd_shape_would_fail_a_roundness_test():
    # Guards the point of the whole change: these shapes are genuinely not
    # circles, so a circularity-based gate would have rejected them. If this
    # ever stops holding, the fixtures have drifted into being discs and the
    # two tests above stopped proving anything.
    for img in (_egg(), _ellipse(aspect=0.53)):
        assert classify_mask(_mask(img)).worst_circularity < 0.82


def test_two_coins_in_one_frame_pass():
    # The UA/PL heart is struck as two half-heart coins and photographed
    # together: two objects, both coins, one valid photo.
    img, draw = _blank()
    draw.ellipse([30, 130, 190, 290], fill=FG)
    draw.ellipse([210, 130, 370, 290], fill=FG)
    verdict = classify_mask(_mask(img))
    assert verdict.objects == 2
    assert verdict.is_coin


# ---------------------------------------------------------------------- #
# What must still fail: packaging
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize("shape", [_rectangle, _tube, _roll])
def test_packaging_rejected(shape):
    assert not classify_mask(_mask(shape())).is_coin


def test_a_flat_rectangle_is_rejected_for_being_boxy():
    assert classify_mask(_mask(_rectangle())).reason == "boxy object in frame"


@pytest.mark.parametrize("shape", [_tube, _roll])
def test_tube_and_roll_break_the_aspect_bound_too(shape):
    # Both are also boxy, and extent is reported first because it is the
    # bound that separates most cleanly -- so the reason string names that
    # one. Aspect is what would catch them if they were ever photographed
    # against a background that softened their corners.
    assert classify_mask(_mask(shape())).worst_aspect < MIN_ASPECT


def test_coin_next_to_a_certificate_is_rejected():
    # One boxy object in the frame is enough, even beside a perfect coin.
    img, draw = _blank()
    draw.ellipse([20, 140, 180, 300], fill=FG)
    draw.rectangle([210, 60, 380, 340], fill=FG)
    verdict = classify_mask(_mask(img))
    assert not verdict.is_coin
    assert verdict.reason == "boxy object in frame"


def test_extent_is_what_separates_a_rectangle_from_a_coin():
    # Both are solid and convex; solidity cannot tell them apart and is not
    # asked to. Extent is: pi/4 for a disc against ~1.0 for a rectangle.
    coin = classify_mask(_mask(_disc()))
    box = classify_mask(_mask(_rectangle()))
    assert coin.worst_solidity >= MIN_SOLIDITY and box.worst_solidity >= MIN_SOLIDITY
    assert coin.worst_extent < MAX_EXTENT < box.worst_extent


# ---------------------------------------------------------------------- #
# Highlight damage: a ragged outline must not cost a coin its verdict
# ---------------------------------------------------------------------- #


def _nibbled_disc(bites: int = 40, radius: int = 170) -> Image.Image:
    """A disc whose rim has been chewed all round, the way a flood fill
    walking along a blown-out rim highlight chews it -- shallow, everywhere,
    no single bite deep enough to look like one. This is the real nbu:161
    defect, and the shape that made perimeter-based circularity collapse to
    0.281 while the coin itself was perfectly fine."""
    import math

    img = _disc(radius)
    draw = ImageDraw.Draw(img)
    c = SIZE // 2
    for i in range(bites):
        angle = 2 * math.pi * i / bites
        bx, by = c + radius * math.cos(angle), c + radius * math.sin(angle)
        draw.ellipse([bx - 7, by - 7, bx + 7, by + 7], fill=(0, 0, 0, 0))
    return img


def test_nibbled_rim_still_passes():
    verdict = classify_mask(_mask(_nibbled_disc()))
    assert verdict.is_coin


def test_nibbled_rim_wrecks_circularity_but_not_the_area_ratios():
    # The reason the gate is built from area ratios: the same damage that
    # multiplies the perimeter barely touches the area.
    clean = classify_mask(_mask(_disc()))
    chewed = classify_mask(_mask(_nibbled_disc()))
    assert chewed.worst_circularity < clean.worst_circularity - 0.2
    assert chewed.worst_solidity >= MIN_SOLIDITY
    assert abs(chewed.worst_extent - clean.worst_extent) < 0.05


def test_score_prefers_the_cleaner_of_two_shots_of_one_coin():
    # Ranking only ever compares one coin's own photos, where shape is
    # constant and mask cleanliness is what varies.
    clean = classify_mask(_mask(_disc()))
    chewed = classify_mask(_mask(_nibbled_disc()))
    assert score(clean) > score(chewed)


# ---------------------------------------------------------------------- #
# Edges
# ---------------------------------------------------------------------- #


def test_empty_mask_finds_no_object():
    verdict = classify_mask(np.zeros((100, 100), np.uint8))
    assert not verdict.is_coin
    assert verdict.reason == "no object found"


def test_unreadable_file(tmp_path):
    path = tmp_path / "not-an-image.png"
    path.write_bytes(b"nope")
    assert classify(path) == Verdict(False, 0, 0.0, 0.0, 0.0, 0.0, 0.0, "unreadable")


def test_classify_reads_alpha_as_the_mask(tmp_path):
    # A transparent PNG hands the mask over directly; the egg must survive
    # that path too, since that is how the odd-shaped NBU originals arrive.
    path = tmp_path / "egg.png"
    _egg().save(path)
    assert classify(path).is_coin
