"""Regression tests over the real photo corpus, with the numbers written
down.

Everything here needs images that are not in the repository (staging/ and
current_ref/ are both gitignored -- they hold scraped NBU and ua-coins
material), so every test skips when its inputs are missing. On a machine
that has them, this is the run that catches the failures the synthetic
tests cannot: they are the actual photos the pipeline got wrong.

Two failures are pinned here.

nbu:161's obverse went missing. Both of its obverse candidates -- a
correctly labelled "Аверс" from ua-coins and NBU's own avers.jpg -- were
thrown away by a hard shape gate, because the coin is a bright nickel-silver
5 hryvnia whose blown-out field shreds the foreground mask and drags
perimeter-based circularity to 0.281 (the reverse of the same coin: 0.870).
The card shipped with one side.

Every cut coin came out with its rim chewed. The white flood tolerance of
28 let everything from 227 up read as background, and a polished proof rim
sits at 230-250, so the fill walked the rim and shaved 2-5% of the disc --
spread evenly, never one bite deep enough to notice, and visible only by
compositing the alpha over a colour by hand.

The bars below are set against the measurements taken before the fix, so a
regression trips them rather than merely looking worse in a log.
"""

import json
import math
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from collector.core import bg_removal, coin_classifier
from collector.countries.ua import photos

REPO = Path(__file__).resolve().parent.parent
SERIES = REPO / "staging" / "ua" / "2000-littia-rizdva-khrystovoho"
# A second series, kept only for nbu:482 -- the one card in the corpus whose
# best source (1120px) falls between two output tiers.
# The slug NBU's own spelling produces -- with the grave accent in
# "пам`ятки" transliterated. An earlier hand-typed series name gave
# "antychni-pamiatky-ukrainy" instead, and when that stray directory
# was cleaned up these cases silently began skipping rather than failing.
STRANDED = REPO / "staging" / "ua" / "antychni-pam-iatky-ukrainy"
ODD_SHAPES = REPO / "current_ref"

# Every source photo of the series, obverse and reverse, NBU and ua-coins.
CARDS = ["nbu_88", "nbu_89", "nbu_95", "nbu_96", "nbu_161", "nbu_163"]

# Measured after the fix, across all 24 source photos plus the two
# odd-shaped references. The gate constants are set outside these.
CORPUS_MIN_SOLIDITY = 0.915
CORPUS_MAX_EXTENT = 0.786
CORPUS_MIN_ASPECT = 0.526

# Before the fix the worst file lost 5.42% of its disc to rim erosion and
# had 79.7% of its outline sitting inside the true radius. After, the worst
# is 1.91% and 9.7%. The bars sit between the two.
MAX_AREA_LOST_FRACTION = 0.025
MAX_OUTLINE_DIP_FRACTION = 0.15


def _require(path: Path):
    if not path.exists():
        pytest.skip(f"corpus not present: {path.relative_to(REPO)}")


def _source_photos() -> list[Path]:
    return sorted(
        p
        for card in CARDS
        for p in (SERIES / "media" / "src" / card).glob("*")
        if p.suffix != ".json"
    )


def _rim_erosion(mask: np.ndarray) -> tuple[float, float]:
    """(share of the outline dipping >2% inside the true radius, share of
    the disc's area missing). The radius is the outline's own 95th
    percentile, so this measures the mask against the coin it should have
    been rather than against any assumed size."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    largest = max(contours, key=cv2.contourArea)
    moments = cv2.moments(largest)
    cx, cy = moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]
    points = largest.reshape(-1, 2).astype(float)
    radius = np.hypot(points[:, 0] - cx, points[:, 1] - cy)
    angle = (np.degrees(np.arctan2(points[:, 1] - cy, points[:, 0] - cx)) + 360.0) % 360.0
    profile = np.full(360, -1.0)
    np.maximum.at(profile, np.minimum(angle.astype(int), 359), radius)
    known = profile[profile >= 0]
    true_radius = np.percentile(known, 95)
    disc_area = math.pi * true_radius * true_radius
    return (
        float(((true_radius - known) / true_radius > 0.02).mean()),
        float((disc_area - (mask > 0).sum()) / disc_area),
    )


# ---------------------------------------------------------------------- #
# The shape gate over the real corpus
# ---------------------------------------------------------------------- #


def test_every_real_photo_passes_the_shape_gate():
    _require(SERIES)
    failures = {
        p.parent.name + "/" + p.name: coin_classifier.classify(p).reason
        for p in _source_photos()
        if not coin_classifier.classify(p).is_coin
    }
    assert failures == {}


def test_corpus_measurements_leave_headroom_over_the_gate_constants():
    # If a future corpus pushes any of these past its constant, the gate
    # starts rejecting real coins and the constant -- not the coin -- is
    # what needs looking at.
    _require(SERIES)
    verdicts = [coin_classifier.classify(p) for p in _source_photos()]
    assert min(v.worst_solidity for v in verdicts) >= coin_classifier.MIN_SOLIDITY
    assert max(v.worst_extent for v in verdicts) <= coin_classifier.MAX_EXTENT
    assert min(v.worst_aspect for v in verdicts) >= coin_classifier.MIN_ASPECT


def test_the_photo_that_lost_nbu_161_its_obverse_now_passes():
    path = SERIES / "media" / "src" / "nbu_161" / "uacoins_01.webp"
    _require(path)
    verdict = coin_classifier.classify(path)
    assert verdict.is_coin
    # Still a badly shredded outline -- the point is that the gate no longer
    # judges on the metric that shredding destroys.
    assert verdict.worst_circularity < 0.4
    assert verdict.worst_solidity >= coin_classifier.MIN_SOLIDITY


# ---------------------------------------------------------------------- #
# The odd-shaped references
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["img.png", "img_1.png"])
def test_odd_shaped_coins_pass(name):
    """img.png is the UA/PL heart -- two half-heart coins in one frame,
    aspect 0.528. img_1.png is the pysanka, aspect 0.733. Both arrive from
    NBU with their background already removed, and both were rejected
    outright by the roundness gate."""
    path = ODD_SHAPES / name
    _require(path)
    assert coin_classifier.classify(path).is_coin


def test_the_heart_is_seen_as_two_coins_not_one_broken_object():
    path = ODD_SHAPES / "img.png"
    _require(path)
    assert coin_classifier.classify(path).objects == 2


@pytest.mark.parametrize("name", ["img.png", "img_1.png"])
def test_odd_shaped_coins_are_left_uncut(name):
    # They already carry alpha, so the cutter must recognise that and keep
    # its hands off -- the guard that a re-cut would destroy real
    # transparency (see ALREADY_TRANSPARENT_FRACTION_MIN).
    path = ODD_SHAPES / name
    _require(path)
    with Image.open(path) as img:
        img.load()
        assert bg_removal.classify(img).reason == "skip:already_transparent"


@pytest.mark.parametrize("name", ["img.png", "img_1.png"])
def test_no_rim_erosion_number_is_reported_for_a_non_round_coin(name):
    path = ODD_SHAPES / name
    _require(path)
    with Image.open(path) as img:
        assert photos._outline_dip_fraction(img) is None


# ---------------------------------------------------------------------- #
# Rim erosion on the real cuts
# ---------------------------------------------------------------------- #


def test_no_real_cut_chews_its_rim():
    _require(SERIES)
    worst = {}
    for path in _source_photos():
        with Image.open(path) as img:
            img.load()
            verdict = bg_removal.classify(img)
            if not verdict.cut:
                continue
            mask = (np.array(verdict.mask.resize(img.size)) > 127).astype(np.uint8) * 255
        dip, lost = _rim_erosion(mask)
        if dip > MAX_OUTLINE_DIP_FRACTION or lost > MAX_AREA_LOST_FRACTION:
            worst[path.parent.name + "/" + path.name] = (round(dip, 3), round(lost, 4))
    assert worst == {}


def test_the_worst_pre_fix_file_is_no_longer_the_worst_kind_of_bad():
    # nbu:88's obverse lost 4.26% of its disc with 47.8% of the outline
    # dipping inside the true radius; it is the file the tolerance change
    # and the outline repair were tuned against.
    path = SERIES / "media" / "src" / "nbu_88" / "uacoins_01.webp"
    _require(path)
    with Image.open(path) as img:
        img.load()
        verdict = bg_removal.classify(img)
        mask = (np.array(verdict.mask.resize(img.size)) > 127).astype(np.uint8) * 255
    dip, lost = _rim_erosion(mask)
    assert dip < 0.01
    assert lost < 0.01


# ---------------------------------------------------------------------- #
# End to end
# ---------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """The pipeline run over a throwaway copy of the corpus, plus the
    directory it wrote into -- several tests need to open the files it
    produced, not only the summary."""
    _require(SERIES / "parsed" / "cards.json")
    import shutil

    work = tmp_path_factory.mktemp("series")
    shutil.copytree(SERIES / "media" / "src", work / "media" / "src")
    shutil.copytree(SERIES / "parsed", work / "parsed")
    cards = json.loads((work / "parsed" / "cards.json").read_text(encoding="utf-8"))["cards"]
    return photos.process_photos(cards, work, series="regression"), work


@pytest.fixture(scope="module")
def processed(run):
    return run[0]


def test_every_card_gets_both_sides(processed):
    missing = {
        c.source_id: [r for r in ("obverse", "reverse") if r not in c.roles]
        for c in processed.cards
    }
    assert {k: v for k, v in missing.items() if v} == {}


def test_nbu_161_has_its_obverse_back(processed):
    card = next(c for c in processed.cards if c.source_id == "nbu:161")
    assert card.roles["obverse"]["winner"]["src_file"] == "uacoins_01.webp"
    assert card.anomalies == []


def test_no_card_reports_an_anomaly(processed):
    assert {c.source_id: c.anomalies for c in processed.cards if c.anomalies} == {}


def test_both_sides_of_a_card_get_the_same_output_tiers(processed):
    for card in processed.cards:
        obverse = [f["file"].split("_")[-1] for f in card.roles["obverse"]["files"]]
        reverse = [f["file"].split("_")[-1] for f in card.roles["reverse"]["files"]]
        assert obverse == reverse, card.source_id


def test_the_top_file_carries_the_full_resolution_of_its_source(processed):
    # The whole point of capping tiers at the source instead of skipping
    # them: nothing the source offers is thrown away short of the 1200 cap.
    for card in processed.cards:
        for role, r in card.roles.items():
            source = max(r["winner"]["width"], r["winner"]["height"])
            top = max(max(f["width"], f["height"]) for f in r["files"])
            assert top >= min(source, max(photos.OUTPUT_SIZES)) * 0.99, (
                card.source_id,
                role,
            )


def test_no_output_file_is_upscaled_past_its_source(processed):
    for card in processed.cards:
        for role, r in card.roles.items():
            source = max(r["winner"]["width"], r["winner"]["height"])
            for f in r["files"]:
                assert max(f["width"], f["height"]) <= source, (card.source_id, role, f)


def test_reported_dimensions_match_the_files_on_disk(run):
    # The contract that replaces "parse the number out of the filename":
    # obverse_1200.webp is 1200px wide only if the source was.
    summary, work = run
    for card in summary.cards:
        out = photos.out_dir(work, card.source_id)
        for r in card.roles.values():
            for f in r["files"]:
                with Image.open(out / f["file"]) as saved:
                    assert saved.size == (f["width"], f["height"]), f


def test_pair_silhouettes_agree_on_every_card(processed):
    for card in processed.cards:
        assert card.pair_silhouette_iou is not None, card.source_id
        assert card.pair_silhouette_iou >= photos.PAIR_SILHOUETTE_MIN, card.source_id


def test_low_res_is_not_flagged_for_600px_sources(processed):
    # nbu:161's reverse was flagged low_res because trimming left it at
    # 600x598 -- two pixels of cropped margin, not a low-resolution photo.
    card = next(c for c in processed.cards if c.source_id == "nbu:161")
    assert card.roles["reverse"]["low_res"] is False


# ---------------------------------------------------------------------- #
# A source stranded between two tiers
# ---------------------------------------------------------------------- #


def test_nbu_482_keeps_its_full_1120px():
    """The best source for this coin is 1120px, from either ua-coins or
    NBU's own /files/coins_images/482a.png -- the two are the same file, and
    nothing larger exists anywhere. 1120 is 7% short of the 1200 tier label
    and 87% above the 600 one; the never-upscale rule used to drop it to 600
    and serve half the available resolution on a large view."""
    card_dir = STRANDED / "media" / "src" / "nbu_482"
    _require(card_dir)
    source = card_dir / "uacoins_01.webp"
    with Image.open(source) as img:
        assert max(img.size) == 1120, "corpus changed -- this test's premise is gone"

    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        shutil.copytree(STRANDED / "media" / "src", work / "media" / "src")
        shutil.copytree(STRANDED / "parsed", work / "parsed")
        cards = json.loads((work / "parsed" / "cards.json").read_text(encoding="utf-8"))["cards"]
        summary = photos.process_photos(cards, work, series="stranded")
        card = next(c for c in summary.cards if c.source_id == "nbu:482")
        for role in ("obverse", "reverse"):
            files = card.roles[role]["files"]
            top = max(files, key=lambda f: max(f["width"], f["height"]))
            assert top["file"] == f"{role}_1200.webp"
            assert (top["width"], top["height"]) == (1120, 1120)
            with Image.open(photos.out_dir(work, "nbu:482") / top["file"]) as saved:
                assert saved.size == (1120, 1120)


def test_nbu_482_offers_the_full_size_original_as_a_candidate():
    """NBU links a full-resolution original for this card
    (/files/coins_images/482a.png, 1120x1120); the listing thumbnail alone
    is 200x200. The link exists on some cards only -- nbu:438 on the same
    page has none -- so this is checked where it is known to be there."""
    card_dir = STRANDED / "media" / "src" / "nbu_482"
    _require(card_dir / "nbu_big_obverse.png")
    with Image.open(card_dir / "nbu_big_obverse.png") as img:
        assert img.size == (1120, 1120)
        assert max(img.size) > 200  # not the thumbnail


def test_the_full_size_nbu_original_is_recognised_as_already_cut():
    """It is an 8-bit palette PNG with tRNS transparency over a black
    matte. Pillow reports no "A" band for that, so the already-transparent
    guard used to miss it, flatten it to RGB -- painting the transparent
    area pure black -- and hand the dark-background branch a backdrop to
    flood-fill and re-cut."""
    path = STRANDED / "media" / "src" / "nbu_482" / "nbu_big_obverse.png"
    _require(path)
    with Image.open(path) as img:
        img.load()
        assert img.mode == "P"
        assert "A" not in img.getbands()
        assert bg_removal.transparent_pixel_fraction(img) > 0.2
        assert bg_removal.classify(img).reason == "skip:already_transparent"


def test_the_paletted_nbu_original_loses_to_the_truecolour_copy():
    """Both files are 1120x1120 with pixel-identical alpha, so resolution
    and silhouette cannot separate them -- but NBU's palette PNG holds 230
    distinct colours against ua-coins' 48015. The extra candidate must not
    win a tie it is worse in."""
    card_dir = STRANDED / "media" / "src" / "nbu_482"
    _require(card_dir / "nbu_big_obverse.png")
    with Image.open(card_dir / "nbu_big_obverse.png") as nbu:
        nbu.load()
        assert not photos._is_truecolour(nbu)
    with Image.open(card_dir / "uacoins_01.webp") as ua:
        ua.load()
        assert photos._is_truecolour(ua)
        assert ua.size == nbu.size  # the tie this test is about
