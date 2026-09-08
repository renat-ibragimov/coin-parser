"""Classic (non-ML) background removal for round coin photos.

Originally vendored from coin_keeper's backend/app/services/media_background.py
(https://github.com/renat-ibragimov/coin_keeper); the incident note on
ALREADY_TRANSPARENT_FRACTION_MIN is load-bearing documentation, kept as-is.
References to docs/06-media-storage.md and other coin_keeper-only paths point
at that repo, not this one. The shape test and the outline repair have since
diverged from that source and no longer track it.

Only a genuinely uniform background (white or, since proof coins are often
shot against black felt, dark) and a genuinely compact object are cut; a
blister pack, a colored backdrop or a coin that touches the frame is left
alone. Note "compact", not "round": the NBU catalog holds an egg-shaped
pysanka and a heart struck as two half-heart coins, and the shape test here
is an area ratio (see EXTENT_MIN/EXTENT_MAX) that accepts all three.
See docs/06-media-storage.md, "Удаление фона", for the rule and the
runbook. Pillow plus stdlib only, no opencv/rembg/numpy.

`classify` decides; `cut_background` executes the decision, then trims the
result to its alpha bbox with `trim_to_alpha` so every cut coin fills its
frame at the same visible size regardless of the source photo's margins.
All three take and return Pillow images so the same functions serve the
batch script (backend/scripts/remove_photo_backgrounds.py) and the ingest
path (app.core.images.process_image).
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass

from PIL import Image, ImageFilter, ImageStat

# "Almost white": every channel at least this bright, and close enough to
# each other that a pale tint (cream, light gray-blue) does not pass as a
# white background.
CORNER_WHITE_MIN = 235
CORNER_CHANNEL_SPREAD_MAX = 12
CORNER_SAMPLE_PX = 12

# "Almost black": every channel darker than this, with the same tint check
# (CORNER_CHANNEL_SPREAD_MAX) as the white corners. Proof coins in the
# ua-coins set are commonly shot against black felt/velvet, and that
# background is exactly as recognizable by its corners as white is.
CORNER_DARK_MAX = 30

# The flood fill grows from the border while a pixel stays within this
# distance (per channel) of the sampled background color -- wide enough for
# a mild vignette or JPEG noise, narrow enough to stop at a metal coin edge.
#
# Was 28 until the proof-coin review (see docs/01_findings.md): against a
# 255 background that let every pixel from 227 up read as background, and a
# polished rim highlight sits at 230-250. The flood fill then walked along
# the rim and shaved it, costing 2-5% of the disc's area on the worst files
# (nbu:88 obverse: 4.1% at 28, 0.9% at 18). The dark branch below already
# carried a tighter bar for exactly this reason; the white branch had been
# left behind.
FLOOD_TOLERANCE = 18

# The dark branch gets a much tighter tolerance than FLOOD_TOLERANCE. A proof
# coin shot against black studio lighting reflects that same black in its
# mirrored field, so the tonal boundary between coin and background can be
# muddy right where they meet; a loose tolerance risks the flood fill eating
# into that mirrored rim and clipping part of the disc. Better to leave a
# genuinely ambiguous dark photo as a border/fragment skip than to bite off
# part of the coin -- reviewed separately as cut:dark, this branch is meant
# to be conservative.
FLOOD_TOLERANCE_DARK = 14

# Classification runs on a shrunk copy: a pure-Python flood fill over a
# 1200x1200 source is slow, and a coin's silhouette does not need per-pixel
# resolution to be classified. The resulting mask is scaled back up (and
# feathered in cut_background) before it ever touches the full image.
CLASSIFY_MAX_SIDE = 400

# A component smaller than this fraction of the frame is a fleck of shadow
# or compression noise, not a second object -- dropped rather than counted
# against "one connected component".
NOISE_AREA_FRACTION = 0.0008

# The background must reach at least this fraction of the frame's own
# border pixels. A coin touching the edge is not itself a problem: the
# flood fill still seeds from the whole frame and the mask stays valid, and
# the source photo already has a flat chord wherever the coin was cropped
# tight against its edge. What this threshold actually guards against is a
# rectangular pack photographed edge-to-edge, where the object hugs the
# border on most or all sides -- and that shape is caught twice over, since
# losing the border on opposite sides also pushes extent above EXTENT_MAX.
BORDER_BACKGROUND_MIN = 0.75

# Extent = object area / its own bounding-box area. A disc is ~0.785, a
# square 1.0 -- the upper bound is what actually screens out rectangular
# blister packs, the lower bound catches shapes too spindly to be a coin.
# Named "circularity" until the odd-shape review; the formula never changed,
# only the name, which had always oversold what an area ratio can tell you.
# The odd-shaped references sit comfortably inside the band (an egg-shaped
# pysanka at 0.762, a half-heart at 0.770), which is the whole point of
# gating on this rather than on roundness. The metrics dict key changed with
# the constants, so `metrics["extent"]` replaces `metrics["circularity"]`.
EXTENT_MAX = 0.87
EXTENT_MIN = 0.55

# Everything below this share of the *largest* object's area is a fleck of
# shadow, a highlight island or compression noise rather than a second
# object. The old rule -- exactly one component above a fixed fraction of
# the frame -- failed twice over: a 40-pixel highlight speck on nbu:96's
# obverse was enough to skip an otherwise clean cut, and a legitimately
# two-object photo (the UA/PL heart is struck as two half-heart coins and
# photographed together) could never pass at all. Now the specks are
# dropped and every surviving object is shape-checked on its own.
COMPANION_AREA_FRACTION = 0.20

# A coin's raised relief blows out to near-white under studio lighting and,
# where that highlight reaches the object's true edge, the flood fill above
# reads it as background and eats into the outline (see docs/01_findings.md,
# "nbu:88 bitten edge" / "nbu:161 false rejection"). `_repair_outline`
# patches that back in before the mask becomes the visible alpha.
#
# The damage comes in two shapes and gets two repairs. On a *disc* it is
# usually not one notch at all but erosion spread right around the rim --
# measured 2-5% of the disc's area gone, with up to 80% of the angular
# buckets sitting more than 2% inside the true radius and no single dip
# deeper than 5%. The old isolated-notch repair could not see that (it
# needed one deep, narrow bite) so the disc branch instead fits the
# outline's own radial envelope and fills back to it.
#
# That fit is only valid for a disc, so it is gated on the envelope being
# consistent with itself: measured spread is 0.0001-0.0086 for real round
# coins, 0.050 for the pysanka and 0.111 for a half-heart -- a 6x margin
# either side of the bar below. Anything that is not a disc falls through
# to the shape-agnostic branch, a morphological closing that fills
# concavities narrower than its kernel and leaves the large-scale outline
# alone (measured on the pysanka: +0.22% area).
OUTLINE_ANGLE_BUCKETS = 360
OUTLINE_ENVELOPE_PERCENTILE = 75
OUTLINE_DISC_SPREAD_MAX = 0.02
# As a fraction of the object's own radius, so the kernel scales with the
# photo instead of being tuned to one resolution. Sized to swallow the
# rim erosion measured above (up to ~5% of the radius) and nothing larger.
OUTLINE_CLOSE_RADIUS_FRACTION = 0.06

# A pixel this opaque or more does not count as "transparent" for the check
# below -- a feathered rim left by a *previous* cut sits just under fully
# opaque and must not itself trip the guard.
ALREADY_TRANSPARENT_ALPHA_MAX = 250

# If at least this fraction of an input's pixels are below that alpha, the
# photo already had its background removed upstream (a subset of the NBU
# originals ship this way, stored as alpha=0 over an arbitrary black matte).
# Incident (2026-09): the dark-background branch read that matte's RGB as a
# black backdrop, flood-filled it, and cut a fresh alpha from its own mask --
# discarding the real transparency and exposing whatever the matte used to
# hide (gradients, shadows, crop leftovers). See docs/06-media-storage.md,
# "Удаление фона", and the `--revert-transparent-originals` runbook there.
ALREADY_TRANSPARENT_FRACTION_MIN = 0.005

# Edge softening on the final mask so the cut does not look scissored.
FEATHER_RADIUS = 1.4

# Bbox threshold for trim_to_alpha: low enough to keep the feathered rim
# cut_background leaves (see FEATHER_RADIUS) inside the crop, high enough to
# ignore stray near-zero alpha noise at the very edge of the frame.
TRIM_ALPHA_THRESHOLD = 8

# Padding added around that bbox, as a fraction of its own larger side, so
# the coin does not end up touching the frame exactly.
TRIM_PADDING_FRACTION = 0.02
TRIM_PADDING_MIN_PX = 2


@dataclass(frozen=True, slots=True)
class Verdict:
    """A classification outcome; `mask` is set only when `cut`.

    `metrics["bgKind"]` ("white" or "dark") tells apart the two backgrounds
    `cut_background` treats identically -- callers that want to review dark
    cuts separately (see backend/scripts/remove_photo_backgrounds.py) key off
    that rather than a distinct `cut` value.
    """

    cut: bool
    reason: str | None
    mask: Image.Image | None = None  # mode "L", same size as the input image
    metrics: dict[str, float | str] | None = None


@dataclass(slots=True)
class _Component:
    area: int
    bbox: tuple[int, int, int, int]
    pixels: list[tuple[int, int]]


def classify(img: Image.Image) -> Verdict:
    """Decide whether `img` is a coin on a uniform background worth cutting.

    Checked before anything else: an input that already carries meaningful
    alpha transparency (see `transparent_pixel_fraction`, and `alpha_channel`
    for what counts as carrying it) has had its background removed already,
    and any further cut can only damage it -- `skip:already_transparent`, no
    metrics computed. This must run on `img` as given, before any
    `.convert("RGB")` throws the alpha away; a caller that pre-flattens to
    RGB defeats the guard (see the `--revert-transparent-originals` incident
    note on ALREADY_TRANSPARENT_FRACTION_MIN).

    Otherwise, two background kinds are recognized by their corners: white
    (the original, by far the most common case) and dark -- proof coins shot
    against black felt/velvet. Both run the same pipeline in
    `_classify_uniform_background`, differing only in flood-fill tolerance.
    Anything else -- textured, colored, or inconsistent corners -- is
    `skip:not_white_bg` regardless of brightness; that reason string predates
    the dark branch and is kept as-is so past and future runs stay
    comparable.

    `metrics` is populated as far as classification gets before a verdict is
    reached, so every row -- cut or skipped -- carries whatever numbers were
    already computed; a rejection early on (not_white_bg) simply leaves the
    later metrics out rather than blank-filling them.
    """
    transparent_fraction = transparent_pixel_fraction(img)
    if transparent_fraction > ALREADY_TRANSPARENT_FRACTION_MIN:
        return Verdict(
            cut=False,
            reason="skip:already_transparent",
            metrics={"transparentFraction": transparent_fraction},
        )

    rgb = img.convert("RGB")
    metrics: dict[str, float | str] = {"cornerWhiteness": _corner_whiteness(rgb)}
    if _corners_are_white(rgb):
        return _classify_uniform_background(
            rgb, metrics, bg_kind="white", flood_tolerance=FLOOD_TOLERANCE
        )
    if _corners_are_dark(rgb):
        return _classify_uniform_background(
            rgb, metrics, bg_kind="dark", flood_tolerance=FLOOD_TOLERANCE_DARK
        )
    return Verdict(cut=False, reason="skip:not_white_bg", metrics=metrics)


def _radius_profile(
    pixels: list[tuple[int, int]], center: tuple[float, float]
) -> tuple[list[float], list[bool]]:
    """Furthest object pixel per angle bucket around `center`, and which
    buckets had any pixel at all."""
    cx, cy = center
    bucket_width = 360.0 / OUTLINE_ANGLE_BUCKETS
    max_radius = [0.0] * OUTLINE_ANGLE_BUCKETS
    has_data = [False] * OUTLINE_ANGLE_BUCKETS
    for x, y in pixels:
        dx, dy = x - cx, y - cy
        radius = math.hypot(dx, dy)
        angle = math.degrees(math.atan2(dy, dx)) % 360.0
        idx = min(int(angle / bucket_width), OUTLINE_ANGLE_BUCKETS - 1)
        has_data[idx] = True
        if radius > max_radius[idx]:
            max_radius[idx] = radius
    return max_radius, has_data


def _component_from_pixels(pixels: list[tuple[int, int]]) -> _Component:
    xs = [x for x, _ in pixels]
    ys = [y for _, y in pixels]
    return _Component(
        area=len(pixels), bbox=(min(xs), min(ys), max(xs), max(ys)), pixels=pixels
    )


def _fit_disc(component: _Component) -> tuple[float, float, float] | None:
    """(cx, cy, radius) of the disc `component`'s outline agrees on, or None
    if the outline is not a disc.

    The radius is the OUTLINE_ENVELOPE_PERCENTILE of the per-bucket maxima,
    not their median: the whole point is to recover a rim the flood fill ate
    into, and a median would anchor the fit halfway inside the damage. The
    "is it a disc" test is the spread of the buckets that reach that
    envelope -- see OUTLINE_DISC_SPREAD_MAX for the measured separation.
    """
    pixels = component.pixels
    if not pixels:
        return None
    cx = sum(x for x, _ in pixels) / len(pixels)
    cy = sum(y for _, y in pixels) / len(pixels)

    max_radius, has_data = _radius_profile(pixels, (cx, cy))
    known = sorted(r for r, has in zip(max_radius, has_data) if has)
    if len(known) < OUTLINE_ANGLE_BUCKETS * 0.5:
        return None  # too sparse an outline to profile meaningfully

    index = min(len(known) - 1, int(len(known) * OUTLINE_ENVELOPE_PERCENTILE / 100))
    envelope = known[index]
    if envelope <= 0:
        return None
    outer = [r for r in known if r >= envelope]
    if len(outer) < 2 or statistics.pstdev(outer) / envelope > OUTLINE_DISC_SPREAD_MAX:
        return None  # not a disc -- leave the shape to the closing branch
    return cx, cy, envelope


def _close_component(component: _Component, size: tuple[int, int]) -> _Component:
    """`component` morphologically closed with a disc kernel sized to its
    own radius -- concavities narrower than the kernel are filled, the
    large-scale outline is untouched.

    Closed one component at a time and only then unioned by the caller: a
    single closing over the whole mask bridges objects that merely lie
    close together, which on the two-half-heart photo fused both coins into
    one blob and doubled the object's area.
    """
    x0, y0, x1, y1 = component.bbox
    radius = max(x1 - x0, y1 - y0) / 2.0
    kernel = int(round(radius * OUTLINE_CLOSE_RADIUS_FRACTION)) * 2 + 1
    if kernel < 3:
        return component

    # Padded so the dilation is not clipped by the frame and the erosion
    # that follows can undo it exactly; PIL's rank filters clamp at the
    # edges, which would otherwise leave a rim of the object smeared
    # against the border.
    pad = kernel
    canvas = Image.new("L", (size[0] + 2 * pad, size[1] + 2 * pad), 0)
    canvas_pixels = canvas.load()
    for x, y in component.pixels:
        canvas_pixels[x + pad, y + pad] = 255  # type: ignore[index]
    closed = canvas.filter(ImageFilter.MaxFilter(kernel)).filter(ImageFilter.MinFilter(kernel))

    width, height = size
    pixels = [
        (x, y)
        for y in range(height)
        for x in range(width)
        if closed.getpixel((x + pad, y + pad))
    ]
    return _component_from_pixels(pixels) if pixels else component


def _repair_outline(component: _Component, size: tuple[int, int]) -> _Component:
    """`component` with flood-fill damage to its outline patched back in.

    A disc is refilled to its own radial envelope; any other shape gets a
    morphological closing instead. Either way the repair only ever adds
    pixels, and only ever inside the outline the component itself implies --
    it cannot invent a shape the photo does not already show.
    """
    disc = _fit_disc(component)
    if disc is None:
        return _close_component(component, size)

    cx, cy, radius = disc
    pad = int(math.ceil(radius)) + 2
    lo_x, hi_x = max(0, int(cx - pad)), min(size[0] - 1, int(cx + pad))
    lo_y, hi_y = max(0, int(cy - pad)), min(size[1] - 1, int(cy + pad))

    patched = set(component.pixels)
    for y in range(lo_y, hi_y + 1):
        for x in range(lo_x, hi_x + 1):
            if (x, y) in patched:
                continue
            if math.hypot(x - cx, y - cy) <= radius:
                patched.add((x, y))
    return _component_from_pixels(sorted(patched))


def _classify_uniform_background(
    rgb: Image.Image,
    metrics: dict[str, float | str],
    *,
    bg_kind: str,
    flood_tolerance: int,
) -> Verdict:
    """The shared pipeline once the background is known to be uniform.

    Flood fill, border containment, object splitting and the extent check
    are identical for white and dark; only `flood_tolerance` varies between
    the two callers in `classify`.
    """
    metrics["bgKind"] = bg_kind
    scale = min(1.0, CLASSIFY_MAX_SIDE / max(rgb.size))
    small = (
        rgb
        if scale == 1.0
        else rgb.resize(
            (max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))),
            Image.Resampling.BILINEAR,
        )
    )
    background = _flood_fill_background(small, _sample_background_color(small), flood_tolerance)

    border_fraction = _border_background_fraction(background, small.size)
    metrics["borderBackgroundFraction"] = border_fraction
    if border_fraction < BORDER_BACKGROUND_MIN:
        return Verdict(cut=False, reason="skip:object_touches_border", metrics=metrics)

    frame_area = small.width * small.height
    found = [
        component
        for component in _object_components(background, small.size)
        if component.area >= NOISE_AREA_FRACTION * frame_area
    ]
    if not found:
        return Verdict(cut=False, reason="skip:fragments", metrics=metrics)

    # Specks are dropped rather than counted as objects; whatever is left
    # must each pass the shape check on its own, so a coin photographed
    # next to its certificate still skips on the certificate's extent.
    largest = max(component.area for component in found)
    components = [c for c in found if c.area >= COMPANION_AREA_FRACTION * largest]
    metrics["objects"] = len(components)

    repaired = []
    for component in components:
        patched = _repair_outline(component, small.size)
        if patched.area != component.area:
            metrics["outlineRepaired"] = True
        repaired.append(patched)

    # One boxy object is enough to make the whole photo a packaging shot,
    # so both bounds are checked against the object that offends each of
    # them, and `metrics["extent"]` always holds the number that decided.
    extents = [_extent(component) for component in repaired]
    metrics["extent"] = max(extents)
    if max(extents) > EXTENT_MAX:
        return Verdict(cut=False, reason="skip:not_round", metrics=metrics)
    if min(extents) < EXTENT_MIN:
        metrics["extent"] = min(extents)
        return Verdict(cut=False, reason="skip:odd_shape", metrics=metrics)

    small_mask = _components_mask(repaired, small.size)
    mask = small_mask if scale == 1.0 else small_mask.resize(rgb.size, Image.Resampling.BILINEAR)
    return Verdict(cut=True, reason=None, mask=mask, metrics=metrics)


def cut_background(img: Image.Image, mask: Image.Image) -> Image.Image:
    """RGBA copy of `img` with `mask` (255 = object) as alpha, edges feathered.

    Trimmed to the alpha bbox as a last step: source photos carry wildly
    different empty margins around the coin, and leaving them in the frame is
    what made cut coins render at different visible sizes in a grid of tiles.
    """
    if mask.size != img.size:
        mask = mask.resize(img.size, Image.Resampling.BILINEAR)
    feathered = mask.filter(ImageFilter.GaussianBlur(FEATHER_RADIUS))
    rgba = img.convert("RGBA")
    rgba.putalpha(feathered)
    return trim_to_alpha(rgba)


def trim_to_alpha(img: Image.Image, padding_fraction: float = TRIM_PADDING_FRACTION) -> Image.Image:
    """Crop `img` to its non-transparent bbox, plus a uniform padding.

    The bbox is taken at TRIM_ALPHA_THRESHOLD, not at fully opaque, so the
    feathered rim `cut_background` leaves is never clipped. Padding is a
    fraction of the bbox's own larger side (floored at TRIM_PADDING_MIN_PX)
    and never pushes the crop past the original frame. An image with nothing
    above the threshold has no object to crop to and is returned unchanged.
    """
    rgba = img if img.mode == "RGBA" else img.convert("RGBA")
    alpha_mask = rgba.split()[3].point(lambda a: 255 if a > TRIM_ALPHA_THRESHOLD else 0)
    bbox = alpha_mask.getbbox()
    if bbox is None:
        return img

    x0, y0, x1, y1 = bbox
    padding = max(TRIM_PADDING_MIN_PX, round(padding_fraction * max(x1 - x0, y1 - y0)))
    width, height = img.size
    crop_box = (
        max(0, x0 - padding),
        max(0, y0 - padding),
        min(width, x1 + padding),
        min(height, y1 + padding),
    )
    return img.crop(crop_box)


def alpha_channel(img: Image.Image) -> Image.Image | None:
    """`img`'s alpha as an "L" image, or None if it genuinely has none.

    Not the same question as `"A" in img.getbands()`. A palette PNG carries
    its transparency in a `tRNS` chunk, which Pillow exposes as
    `info["transparency"]` over a single "P" band -- so the band test says
    "opaque" about an image that is 22% transparent. NBU's full-resolution
    coin images (/files/coins_images/<id>a.png) are exactly that: mode "P"
    with tRNS over a *black* matte, which is the worst possible combination
    for the guard below to miss, since flattening them to RGB hands the
    dark-background branch a perfect black backdrop to flood-fill.
    """
    if img.mode in ("P", "PA") and "transparency" in img.info:
        return img.convert("RGBA").getchannel("A")
    if "A" in img.getbands():
        return img.getchannel("A")
    return None


def transparent_pixel_fraction(img: Image.Image) -> float:
    """Fraction of pixels with alpha < ALREADY_TRANSPARENT_ALPHA_MAX; 0 for an opaque image.

    Public so the `--revert-transparent-originals` runbook (backend/scripts/
    remove_photo_backgrounds.py) can apply the exact same criterion to a
    stored original as `classify` applies to its input, without duplicating
    the histogram math.
    """
    alpha = alpha_channel(img)
    if alpha is None:
        return 0.0
    total = img.width * img.height
    if total == 0:
        return 0.0
    alpha_histogram = alpha.histogram()
    below_max = sum(alpha_histogram[:ALREADY_TRANSPARENT_ALPHA_MAX])
    return below_max / total


def _corner_boxes(img: Image.Image) -> list[tuple[int, int, int, int]]:
    width, height = img.size
    side = max(1, min(CORNER_SAMPLE_PX, width // 2, height // 2))
    return [
        (0, 0, side, side),
        (width - side, 0, width, side),
        (0, height - side, side, height),
        (width - side, height - side, width, height),
    ]


def _average_color(patch: Image.Image) -> tuple[float, float, float]:
    r, g, b = ImageStat.Stat(patch).mean
    return r, g, b


def _corner_whiteness(img: Image.Image) -> float:
    """Worst-case corner brightness: the darkest channel across all four corners.

    A single number for the review CSV, on the same scale as CORNER_WHITE_MIN.
    The pass/fail check in `_corners_are_white` additionally screens the
    per-channel spread (a tint), which this metric does not capture.
    """
    return min(min(_average_color(img.crop(box))) for box in _corner_boxes(img))


def _corners_are_white(img: Image.Image) -> bool:
    for box in _corner_boxes(img):
        r, g, b = _average_color(img.crop(box))
        if min(r, g, b) < CORNER_WHITE_MIN:
            return False
        if max(r, g, b) - min(r, g, b) > CORNER_CHANNEL_SPREAD_MAX:
            return False
    return True


def _corners_are_dark(img: Image.Image) -> bool:
    for box in _corner_boxes(img):
        r, g, b = _average_color(img.crop(box))
        if max(r, g, b) > CORNER_DARK_MAX:
            return False
        if max(r, g, b) - min(r, g, b) > CORNER_CHANNEL_SPREAD_MAX:
            return False
    return True


def _sample_background_color(img: Image.Image) -> tuple[float, float, float]:
    corners = [_average_color(img.crop(box)) for box in _corner_boxes(img)]
    return (
        sum(c[0] for c in corners) / len(corners),
        sum(c[1] for c in corners) / len(corners),
        sum(c[2] for c in corners) / len(corners),
    )


def _flood_fill_background(
    img: Image.Image, bg: tuple[float, float, float], tolerance: float
) -> bytearray:
    """1 = background, reached from the border within `tolerance` per channel; 0 = object."""
    width, height = img.size
    pixels = img.load()
    visited = bytearray(width * height)
    queue: deque[tuple[int, int]] = deque()

    def is_background(x: int, y: int) -> bool:
        r, g, b = pixels[x, y]  # type: ignore[index, misc]
        return bool(
            abs(r - bg[0]) <= tolerance
            and abs(g - bg[1]) <= tolerance
            and abs(b - bg[2]) <= tolerance
        )

    def seed(x: int, y: int) -> None:
        idx = y * width + x
        if not visited[idx] and is_background(x, y):
            visited[idx] = 1
            queue.append((x, y))

    for x in range(width):
        seed(x, 0)
        seed(x, height - 1)
    for y in range(height):
        seed(0, y)
        seed(width - 1, y)

    while queue:
        x, y = queue.popleft()
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < width and 0 <= ny < height:
                idx = ny * width + nx
                if not visited[idx] and is_background(nx, ny):
                    visited[idx] = 1
                    queue.append((nx, ny))
    return visited


def _border_pixels(size: tuple[int, int]) -> set[tuple[int, int]]:
    width, height = size
    coords = {(x, 0) for x in range(width)} | {(x, height - 1) for x in range(width)}
    coords |= {(0, y) for y in range(height)} | {(width - 1, y) for y in range(height)}
    return coords


def _border_background_fraction(visited: bytearray, size: tuple[int, int]) -> float:
    width, _ = size
    border = _border_pixels(size)
    if not border:
        return 1.0
    background = sum(1 for x, y in border if visited[y * width + x])
    return background / len(border)


def _object_components(visited: bytearray, size: tuple[int, int]) -> list[_Component]:
    width, height = size
    labeled = bytearray(width * height)
    components: list[_Component] = []
    for start_y in range(height):
        for start_x in range(width):
            start_idx = start_y * width + start_x
            if visited[start_idx] or labeled[start_idx]:
                continue
            labeled[start_idx] = 1
            pixels: list[tuple[int, int]] = []
            queue: deque[tuple[int, int]] = deque([(start_x, start_y)])
            min_x = max_x = start_x
            min_y = max_y = start_y
            while queue:
                x, y = queue.popleft()
                pixels.append((x, y))
                min_x, max_x = min(min_x, x), max(max_x, x)
                min_y, max_y = min(min_y, y), max(max_y, y)
                for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                    if 0 <= nx < width and 0 <= ny < height:
                        nidx = ny * width + nx
                        if not visited[nidx] and not labeled[nidx]:
                            labeled[nidx] = 1
                            queue.append((nx, ny))
            components.append(
                _Component(area=len(pixels), bbox=(min_x, min_y, max_x, max_y), pixels=pixels)
            )
    return components


def _extent(component: _Component) -> float:
    """Object area over its own bounding-box area -- see EXTENT_MIN/MAX."""
    x0, y0, x1, y1 = component.bbox
    bbox_area = (x1 - x0 + 1) * (y1 - y0 + 1)
    return component.area / bbox_area if bbox_area else 0.0


def _components_mask(components: list[_Component], size: tuple[int, int]) -> Image.Image:
    mask = Image.new("L", size, 0)
    pixels = mask.load()
    for component in components:
        for x, y in component.pixels:
            pixels[x, y] = 255  # type: ignore[index]
    return mask
