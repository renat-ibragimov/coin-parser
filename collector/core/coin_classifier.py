"""Tell a clean coin photograph from a packaging one (roll, tube, box).

Originally vendored from coin_keeper's
backend/app/ukraine_pipeline/classify_coin_photos.py
(https://github.com/renat-ibragimov/coin_keeper); the shape test has since
diverged from that source and no longer tracks it -- see "Why not
circularity" below.

A coin shot is one compact solid object on a plain background; packaging
is anything else. The check is geometric, not learned: find the foreground
against the background sampled from the borders, take the dominant
contours, and measure how compact and how solid they are. An image passes
as a coin photo when every significant object in it is one compact blob
(one side, or obverse+reverse laid side by side).

Why not circularity. The obvious test -- "is it a circle" -- fails from
both ends. It rejects the coins that are not discs (the NBU catalog holds
an egg-shaped pysanka, a heart struck as two half-heart coins, and more
will follow), and the perimeter-based form of it, 4*pi*area/perimeter^2,
also collapses on perfectly ordinary round coins whose blown-out highlights
punch holes into the foreground mask: measured 0.281 on nbu:161's obverse,
against 0.870 for the reverse of the same coin. Perimeter is the fragile
part -- one ragged contour multiplies it while barely touching the area.
So the gate is built from area ratios only:

    solidity = area / convex hull area   -- one blob, not several fused
    extent   = area / bounding box area  -- compact, not a rectangle
    aspect   = short side / long side    -- not a roll or a tube

Measured on the NBU/ua-coins corpus plus the two odd-shaped references:
round coins land at solidity 0.916-0.998, extent 0.691-0.786, aspect
0.960-1.000; the pysanka at 0.997/0.750/0.735; a half-heart at
0.997/0.758/0.526. Synthetic packaging lands well outside: a blister or
certificate at extent 0.974-0.995, a tube standing upright at aspect
0.251, a roll lying down at 0.178. Circularity and circle fill are still
computed, but only to rank equals in `score` -- neither gates anything.

This is a ranking, not a gate. `pick_best` takes a page's images in page
order and returns the best-looking ones; when nothing on the page passes as
a coin, it falls back to the first images exactly as a parser without this
module would, and only marks the choice as a fallback. The result can never
be worse than taking the page head — only sometimes better. Callers must
honour that contract: `is_coin` demotes a candidate, it does not delete it
(see collector/countries/ua/photos.py).

Usage:
    python classify_coin_photos.py IMAGE [IMAGE ...]
    python classify_coin_photos.py --move-rejected DIR IMAGE ...
    python classify_coin_photos.py --pick 2 IMAGE [IMAGE ...]

Exit code 0 always; verdicts are printed per file, `--pick` prints the chosen
files last, so the caller can grep or parse.
"""

from __future__ import annotations

import argparse
import math
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

# An object must be at least this share of the frame to be judged at all;
# smaller blobs are logos, shadows and dust.
MIN_AREA_SHARE = 0.02
# Area over convex hull area. A coin of any shape is convex or nearly so;
# the bar sits below the worst real photo measured (0.916, nbu:161's
# highlight-damaged obverse) so a ragged mask still reads as one object.
MIN_SOLIDITY = 0.88
# Area over bounding-box area. A disc is pi/4 = 0.785 and every real coin
# measured lands at or under that; a rectangle -- blister, slab, certificate,
# box -- is 0.95 and up. This is the one that actually screens out packaging.
MAX_EXTENT = 0.85
# Width to height of the bounding box. Wide enough for a half-heart (0.526),
# far too tight for a tube standing upright (0.251) or a roll lying down.
MIN_ASPECT = 0.45
# Foreground/background split: distance from the border colour, 0..255.
BACKGROUND_TOLERANCE = 28


@dataclass
class Verdict:
    is_coin: bool
    objects: int
    worst_solidity: float
    worst_extent: float
    worst_aspect: float
    worst_circularity: float
    worst_fill: float
    reason: str


def _fill_holes(mask: np.ndarray) -> np.ndarray:
    """`mask` with enclosed holes filled in.

    A blown-out highlight in the middle of a coin reads as background and
    leaves a hole; left alone it can survive as a "significant object" of
    its own and inflate the object count. The fill seeds from *every*
    border pixel, not from one corner: a coin cropped tight against the
    frame splits the surrounding background into disjoint regions, and
    seeding from a single corner would then mistake the other regions for
    holes and swallow the whole frame.
    """
    height, width = mask.shape
    padded = cv2.copyMakeBorder(
        (mask == 0).astype(np.uint8) * 255, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=255
    )
    flooded = padded.copy()
    cv2.floodFill(flooded, np.zeros((height + 4, width + 4), np.uint8), (0, 0), 0)
    return mask | flooded[1:-1, 1:-1]


def _foreground_mask(image: np.ndarray) -> np.ndarray:
    """Everything that is not the border colour.

    The background colour is read off the frame's own borders rather than
    assumed white: NBU shots are white, ua-coins are near-white, and a grey
    studio background should work the same way.
    """
    border = np.concatenate([image[0, :], image[-1, :], image[:, 0], image[:, -1]]).astype(
        np.float32
    )
    background = np.median(border, axis=0)
    distance = np.linalg.norm(image.astype(np.float32) - background, axis=2)
    mask = (distance > BACKGROUND_TOLERANCE).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    return _fill_holes(mask)


def _shape(contour: np.ndarray) -> tuple[float, float, float, float, float]:
    """(solidity, extent, aspect, circularity, circle fill) for one contour.

    The first three gate; the last two only rank. `cv2.contourArea` on an
    external contour is the polygon's own area, so holes inside the object
    never enter any of these -- only the outline does.
    """
    area = cv2.contourArea(contour)
    hull_area = cv2.contourArea(cv2.convexHull(contour))
    perimeter = cv2.arcLength(contour, closed=True)
    _x, _y, width, height = cv2.boundingRect(contour)
    (_, _), radius = cv2.minEnclosingCircle(contour)
    return (
        area / hull_area if hull_area else 0.0,
        area / (width * height) if width and height else 0.0,
        min(width, height) / max(width, height),
        4 * math.pi * area / (perimeter * perimeter) if perimeter else 0.0,
        area / (math.pi * radius * radius) if radius else 0.0,
    )


def classify(path: Path, max_side: int = 800) -> Verdict:
    raw = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if raw is None:
        return Verdict(False, 0, 0.0, 0.0, 0.0, 0.0, 0.0, "unreadable")
    # A transparent PNG hands us the mask for free: the alpha channel *is*
    # the foreground, cut by whoever prepared the image.
    if raw.ndim == 3 and raw.shape[2] == 4:
        alpha = raw[:, :, 3]
        image = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)
        premask = (alpha > 16).astype(np.uint8) * 255
    else:
        image = raw if raw.ndim == 3 else cv2.cvtColor(raw, cv2.COLOR_GRAY2BGR)
        premask = None

    scale = max_side / max(image.shape[:2])
    if scale < 1.0:
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if premask is not None:
            premask = cv2.resize(  # type: ignore[assignment]
                premask, image.shape[1::-1], interpolation=cv2.INTER_NEAREST
            )

    mask = premask if premask is not None else _foreground_mask(image)
    return classify_mask(mask)


def classify_mask(mask: np.ndarray) -> Verdict:
    """The shape gate on a foreground mask that is already cut.

    Split out of `classify` so the same bar can be applied to a mask this
    module did not produce -- specifically to the alpha of a freshly cut
    photo, where the question is whether the cut damaged the object's shape
    (see collector/countries/ua/photos.py). Non-zero is foreground.
    """
    frame_area = mask.shape[0] * mask.shape[1]
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    significant = [c for c in contours if cv2.contourArea(c) >= frame_area * MIN_AREA_SHARE]
    if not significant:
        return Verdict(False, 0, 0.0, 0.0, 0.0, 0.0, 0.0, "no object found")

    shapes = [_shape(contour) for contour in significant]
    solidity = min(s[0] for s in shapes)
    extent = max(s[1] for s in shapes)  # the *worst* extent is the largest one
    aspect = min(s[2] for s in shapes)
    circularity = min(s[3] for s in shapes)
    fill = min(s[4] for s in shapes)

    # Reported in the order the numbers separate best, so a log line names
    # the actual disqualifier rather than a generic "not a coin".
    if extent > MAX_EXTENT:
        reason = "boxy object in frame"
    elif aspect < MIN_ASPECT:
        reason = "elongated object in frame"
    elif solidity < MIN_SOLIDITY:
        reason = "scattered object in frame"
    else:
        reason = "ok"

    return Verdict(
        reason == "ok", len(significant), solidity, extent, aspect, circularity, fill, reason
    )


def combine(verdicts: list[Verdict]) -> Verdict:
    """The worst of several verdicts, as one — a record with several stored
    sides is only as good as its worst side (scripts/scan_coin_photo_packaging.py,
    app/ukraine_pipeline/photo_upgrade.py)."""
    if not verdicts:
        return Verdict(False, 0, 0.0, 0.0, 0.0, 0.0, 0.0, "no photo")
    is_coin = all(v.is_coin for v in verdicts)
    return Verdict(
        is_coin,
        sum(v.objects for v in verdicts),
        min(v.worst_solidity for v in verdicts),
        max(v.worst_extent for v in verdicts),
        min(v.worst_aspect for v in verdicts),
        min(v.worst_circularity for v in verdicts),
        min(v.worst_fill for v in verdicts),
        "ok" if is_coin else next(v.reason for v in verdicts if not v.is_coin),
    )


def score(verdict: Verdict) -> float:
    """One continuous number for ranking; the pass/fail bar stays separate.

    Ranking only ever compares photos of the *same* coin, so the shape
    terms (extent, aspect) are near-constant across the pool and carry
    little weight. What does vary between two shots of one coin is how
    cleanly the object separates from the background, and that is what
    solidity and circularity measure -- a highlight-shredded outline scores
    low and loses to the same coin photographed with a cleaner rim.
    """
    return (
        0.45 * verdict.worst_solidity
        + 0.30 * verdict.worst_circularity
        + 0.15 * verdict.worst_fill
        + 0.10 * verdict.worst_aspect
    )


@dataclass
class Pick:
    chosen: list[Path]
    fallback: bool
    scores: dict[Path, float]


def pick_best(paths: list[Path], count: int = 2) -> Pick:
    """The best `count` images of a page, in page order among equals.

    Every image that classifies as a coin beats every one that does not;
    within each group the score decides and the page order breaks ties, so a
    page of nothing but packaging returns exactly its first images — the
    same choice a parser without this module makes — flagged as a fallback.
    """
    verdicts = {path: classify(path) for path in paths}
    scores = {path: score(verdicts[path]) for path in paths}
    ranked = sorted(
        paths,
        key=lambda p: (not verdicts[p].is_coin, -scores[p], paths.index(p)),
    )
    chosen = ranked[:count]
    fallback = not any(verdicts[p].is_coin for p in chosen)
    if fallback:
        chosen = paths[:count]
    return Pick(chosen=chosen, fallback=fallback, scores=scores)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument(
        "--move-rejected",
        type=Path,
        default=None,
        help="move packaging shots into this directory instead of only reporting",
    )
    parser.add_argument(
        "--pick",
        type=int,
        default=None,
        metavar="N",
        help="rank the given images as one page and print the N chosen ones",
    )
    args = parser.parse_args()
    if args.pick:
        pick = pick_best(args.images, args.pick)
        for path in args.images:
            marker = "*" if path in pick.chosen else " "
            print(f"{marker} {pick.scores[path]:.2f}  {path.name}")
        suffix = "  (fallback: page head, nothing passed)" if pick.fallback else ""
        print("chosen: " + " ".join(p.name for p in pick.chosen) + suffix)
        return 0
    if args.move_rejected:
        args.move_rejected.mkdir(parents=True, exist_ok=True)
    for path in args.images:
        verdict = classify(path)
        label = "coin" if verdict.is_coin else "packaging"
        print(
            f"{label:9s} {path.name}  objects={verdict.objects}"
            f" solidity={verdict.worst_solidity:.2f} extent={verdict.worst_extent:.2f}"
            f" aspect={verdict.worst_aspect:.2f} ({verdict.reason})"
        )
        if not verdict.is_coin and args.move_rejected:
            shutil.move(str(path), args.move_rejected / path.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
