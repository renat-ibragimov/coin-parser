"""Pure-logic tests for bg_removal.py's outline repair and object
splitting -- synthetic pixel sets only, no network, no real coin photos
needed.

Two failures drove this code, and both are covered below.

The first is rim erosion. A coin's raised relief blows out to near-white
under studio lighting, the flood fill reads that as background and walks
along the rim, and the mask comes back shaved -- measured 2-5% of the
disc's area gone, spread right around the outline, with no single dip deep
enough to look like a bite. The earlier repair here only knew how to patch
one isolated notch, so it never fired on the real defect.

The second is shape. The repair must not force a coin round: the NBU
catalog holds an egg-shaped pysanka and a heart struck as two half-heart
coins. So the disc fit is gated on the outline actually being a disc, and
everything else takes a shape-agnostic morphological closing instead.
"""

import math

import pytest
from PIL import Image, ImageDraw

from collector.core.bg_removal import (
    OUTLINE_DISC_SPREAD_MAX,
    alpha_channel,
    classify,
    transparent_pixel_fraction,
    _close_component,
    _Component,
    _component_from_pixels,
    _extent,
    _fit_disc,
    _repair_outline,
)

SIZE = (260, 260)
CX, CY = 130, 130
RADIUS = 90


def _pixels_where(predicate, size=SIZE) -> _Component:
    pixels = [
        (x, y) for y in range(size[1]) for x in range(size[0]) if predicate(x - CX, y - CY)
    ]
    return _component_from_pixels(pixels)


def _disc(radius=RADIUS) -> _Component:
    return _pixels_where(lambda dx, dy: math.hypot(dx, dy) <= radius)


def _eroded_disc(depth=4, bites=40, radius=RADIUS) -> _Component:
    """A disc chewed shallowly all the way round -- the real defect. Every
    bite is far too shallow to register as a notch on its own; what makes
    it damage is that there are forty of them."""

    def inside(dx, dy):
        r = math.hypot(dx, dy)
        angle = math.degrees(math.atan2(dy, dx)) % 360.0
        local = radius - depth if int(angle / (360.0 / bites)) % 2 == 0 else radius
        return r <= local

    return _pixels_where(inside)


def _egg(radius=RADIUS) -> _Component:
    """Convex, smooth, and definitively not a circle: the top half is
    narrower than the bottom."""

    def inside(dx, dy):
        half_width = radius * (0.72 if dy < 0 else 0.92)
        return (dx / half_width) ** 2 + (dy / radius) ** 2 <= 1.0

    return _pixels_where(inside)


def _half_heart(radius=RADIUS) -> _Component:
    """A lobe on top tapering to a point below -- one of the two coins the
    UA/PL heart is struck as."""

    def inside(dx, dy):
        if dy <= 0:
            return math.hypot(dx, dy) <= radius
        return abs(dx) <= radius * (1.0 - dy / radius) and dy <= radius
    return _pixels_where(inside)


# ---------------------------------------------------------------------- #
# _fit_disc -- "may I assume this outline is a circle?"
# ---------------------------------------------------------------------- #


def test_fit_disc_recognises_a_disc():
    fit = _fit_disc(_disc())
    assert fit is not None
    _cx, _cy, radius = fit
    assert abs(radius - RADIUS) < 2


def test_fit_disc_recovers_the_true_radius_of_an_eroded_disc():
    # The fit anchors on the outline's upper envelope, not its median, so
    # the radius it returns is the one the erosion took away -- otherwise
    # the repair would refill to halfway inside the damage.
    fit = _fit_disc(_eroded_disc())
    assert fit is not None
    assert abs(fit[2] - RADIUS) < 2


@pytest.mark.parametrize("shape", [_egg, _half_heart])
def test_fit_disc_refuses_a_non_disc(shape):
    # The load-bearing guard: no disc fit means no rounding-off of a coin
    # that is not round.
    assert _fit_disc(shape()) is None


def test_disc_and_non_disc_envelope_spreads_are_far_apart():
    # Documents the margin the OUTLINE_DISC_SPREAD_MAX bar sits in; on the
    # real corpus it is 0.0001-0.0086 for round coins against 0.050 for the
    # pysanka and 0.111 for a half-heart.
    assert _fit_disc(_disc()) is not None
    assert _fit_disc(_eroded_disc()) is not None
    assert _fit_disc(_egg()) is None
    assert OUTLINE_DISC_SPREAD_MAX < 0.05


# ---------------------------------------------------------------------- #
# _repair_outline -- disc branch
# ---------------------------------------------------------------------- #


def test_repair_refills_an_evenly_eroded_rim():
    full = _disc()
    eroded = _eroded_disc()
    assert eroded.area < full.area  # the erosion really removed pixels

    repaired = _repair_outline(eroded, SIZE)
    recovered = (repaired.area - eroded.area) / (full.area - eroded.area)
    assert recovered > 0.9


def test_repair_leaves_a_clean_disc_essentially_alone():
    full = _disc()
    repaired = _repair_outline(full, SIZE)
    assert abs(repaired.area - full.area) / full.area < 0.01


def test_repair_only_ever_adds_pixels():
    eroded = _eroded_disc()
    repaired = _repair_outline(eroded, SIZE)
    assert set(eroded.pixels) <= set(repaired.pixels)


# ---------------------------------------------------------------------- #
# _repair_outline -- shape-agnostic branch
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize("shape", [_egg, _half_heart])
def test_repair_does_not_round_off_a_non_disc(shape):
    original = shape()
    repaired = _repair_outline(original, SIZE)
    # The closing may tidy the outline, but the coin must still be the
    # shape it was -- a disc fit would have pushed extent to pi/4.
    assert abs(_extent(repaired) - _extent(original)) < 0.03
    assert repaired.bbox == original.bbox


def test_closing_does_not_bridge_two_nearby_objects():
    # The UA/PL heart is two half-heart coins photographed a few pixels
    # apart. Closing the whole mask at once fused them into a single blob
    # and doubled the object's area; closing each component on its own is
    # what keeps them two coins.
    left = _pixels_where(lambda dx, dy: math.hypot(dx + 55, dy) <= 45)
    closed = _close_component(left, SIZE)
    assert closed.bbox[2] < CX  # never reaches across to where the other coin sits
    assert closed.area < left.area * 1.2


def test_close_component_is_a_no_op_on_a_tiny_component():
    tiny = _Component(area=1, bbox=(5, 5, 5, 5), pixels=[(5, 5)])
    assert _close_component(tiny, SIZE) is tiny


def test_repair_of_an_empty_component_is_safe():
    empty = _Component(area=0, bbox=(0, 0, 0, 0), pixels=[])
    assert _repair_outline(empty, SIZE).area == 0


# ---------------------------------------------------------------------- #
# alpha_channel -- what counts as "this photo already has a cut background"
# ---------------------------------------------------------------------- #

def _paletted_png(tmp_path, name="paletted.png"):
    """An 8-bit palette PNG carrying its transparency in a tRNS chunk --
    the shape NBU's full-resolution originals come in
    (/files/coins_images/<id>a.png, 1120x1120, 22% transparent).

    Pillow reports mode "P" and a single "P" band for these, so asking
    `"A" in img.getbands()` calls them fully opaque. Flattening one to RGB
    then paints the transparent area black, which is precisely the backdrop
    the dark-background branch is looking for.
    """
    rgba = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
    ImageDraw.Draw(rgba).ellipse([10, 10, 50, 50], fill=(200, 170, 60, 255))
    path = tmp_path / name
    rgba.convert("P").save(path)
    return path


def _opened(path):
    img = Image.open(path)
    img.load()
    return img


def test_the_fixture_really_is_the_awkward_case(tmp_path):
    # If Pillow ever starts reporting an "A" band for these, the tests
    # below stop proving anything and this one says so.
    img = _opened(_paletted_png(tmp_path))
    assert img.mode == "P"
    assert "A" not in img.getbands()
    assert "transparency" in img.info


def test_palette_transparency_is_seen(tmp_path):
    img = _opened(_paletted_png(tmp_path))
    assert alpha_channel(img) is not None
    assert transparent_pixel_fraction(img) > 0.3


def test_a_paletted_original_is_not_cut_again(tmp_path):
    # The guard this restores: an image whose background was already
    # removed can only be damaged by cutting it again.
    img = _opened(_paletted_png(tmp_path, "again.png"))
    assert classify(img).reason == "skip:already_transparent"


def test_flattening_a_paletted_original_is_what_the_guard_prevents(tmp_path):
    # Shows the trap rather than assuming it: the transparent area becomes
    # pure black, and black corners are what sends classify() down the
    # dark-background branch to flood-fill and re-cut.
    img = _opened(_paletted_png(tmp_path, "flat.png"))
    flattened = img.convert("RGB")
    assert flattened.getpixel((0, 0)) == (0, 0, 0)
    assert classify(flattened).cut is True


def test_rgba_and_la_still_work():
    assert alpha_channel(Image.new("RGBA", (8, 8), (0, 0, 0, 0))) is not None
    assert alpha_channel(Image.new("LA", (8, 8), (0, 0))) is not None


@pytest.mark.parametrize(
    "img",
    [Image.new("RGB", (8, 8), (255, 255, 255)), Image.new("P", (8, 8))],
    ids=["rgb", "palette without tRNS"],
)
def test_an_opaque_image_still_reports_no_alpha(img):
    assert alpha_channel(img) is None
    assert transparent_pixel_fraction(img) == 0.0
