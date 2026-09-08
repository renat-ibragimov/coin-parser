"""Photo pipeline for NBU cards: fetch_photos() (network) collects every
candidate photo per card into staging/ua/<slug>/media/src/<source_id>/;
process_photos() (strictly offline, reads only what fetch_photos wrote)
picks a winner per role (obverse/reverse), cuts its background when the
photo warrants it, and writes resized WebP files to
staging/ua/<slug>/media/out/<source_id>/.

No DB, no MinIO, no prices, no ML background removal -- see
docs/00_spec.md and docs/01_findings.md for why (classic corner/flood-fill
classification via collector/core/bg_removal.py, geometric coin-vs-
packaging classification via collector/core/coin_classifier.py, both
vendored from coin_keeper).
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import cv2
import httpx
import numpy as np
from PIL import Image
from selectolax.parser import HTMLParser

from collector.core import bg_removal, coin_classifier
from collector.core.pacing import Pacer
from collector.countries.ua import nbu_client, ua_coins

OUTPUT_SIZES = (300, 600, 1200)
LOW_RES_THRESHOLD = 600
WEBP_QUALITY = 80

MASK_ALPHA_THRESHOLD = 128

# Above this many distinct colours a photo counts as truecolour. NBU's
# full-resolution originals are 8-bit palette PNGs -- nbu:482's obverse
# holds 230 distinct colours where ua-coins' WebP of the very same image,
# at the very same 1120x1120 and with a pixel-identical alpha channel,
# holds 48015. Same silhouette, same resolution, an eightieth of the
# colour, so the two must not rank as equals.
TRUECOLOUR_MIN = 256

# A tier is skipped when it would gain this little over the file already
# written. OUTPUT_SIZES are labels -- "the file to fetch when you want
# roughly this size" -- and each one is written at whatever resolution the
# source actually has, capped at the label. That is what stops a source
# stranded between two tiers from collapsing to the one below: nbu:482
# arrives at 1120px, which is 7% short of the 1200 label and 87% above the
# 600 one, and the old never-upscale rule threw away all 87% to avoid
# inventing 7%. Now it ships as obverse_1200.webp at its true 1120px.
#
# The gain check is what keeps that from producing duplicates: a 200x200
# NBU original would otherwise be written three times over, once per label.
TIER_MIN_GAIN = 0.03

# Obverse and reverse of one coin are the same piece of metal, so their
# silhouettes must agree; measured 0.970-0.999 across every healthy pair in
# the corpus. A drop below this bar means one of the two sides was cut
# wrong, and it is the only damage check here that does not assume a shape
# -- which matters, because an absolute one cannot exist: a coin cut clean
# in half and a genuine half-heart coin are the same geometry (aspect 0.50
# vs 0.53, circle fill 0.50 vs 0.48). Recorded as an anomaly rather than a
# rejection, since the number says the pair disagrees but not which side is
# at fault.
PAIR_SILHOUETTE_MIN = 0.93

# Rim erosion, for the run report: the share of angle buckets whose radius
# falls more than OUTLINE_DIP_TOLERANCE inside the outline's own 95th
# percentile radius. The flood fill used to shave 2-5% of a proof coin's
# area this way with no single dip deep enough to look like a bite, and the
# only way to see it was compositing the alpha over a colour by hand -- so
# the number goes in photos.json, where a regression is greppable.
OUTLINE_DIP_TOLERANCE = 0.02

_ROLE_KEYWORDS = {
    "obverse": ("обверс", "авер", "obverse"),  # "аверс"/"обверс" both seen in the wild
    "reverse": ("реверс", "ревер", "revers", "reverse"),
}


def _role_from_text(text: str | None) -> str | None:
    if not text:
        return None
    low = text.lower()
    # reverse checked first: "revers" is a substring-safe check, but a
    # stray "averse...reverse" combined caption should still resolve to
    # reverse only if that's genuinely the more specific hit -- in
    # practice NBU/ua-coins captions only ever name one side.
    for role, keywords in _ROLE_KEYWORDS.items():
        if any(kw in low for kw in keywords):
            return role
    return None


def source_dir(series_dir: Path, source_id: str) -> Path:
    return series_dir / "media" / "src" / source_id.replace(":", "_")


def out_dir(series_dir: Path, source_id: str) -> Path:
    return series_dir / "media" / "out" / source_id.replace(":", "_")


def _ext_from_url(url: str) -> str:
    return Path(urlparse(url).path).suffix or ".jpg"


# ---------------------------------------------------------------------- #
# fetch_photos -- network
# ---------------------------------------------------------------------- #


@dataclass
class FetchCandidate:
    file: str
    url: str
    alt: str | None
    source: str  # "nbu" | "ua_coins"
    width: int
    height: int
    bytes: int


@dataclass
class FetchCardResult:
    source_id: str
    candidates: list[FetchCandidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class FetchPhotosSummary:
    series: str
    cards: list[FetchCardResult] = field(default_factory=list)

    def print_report(self) -> None:
        print(f"[fetch-photos] series: {self.series}")
        for c in self.cards:
            nbu_n = sum(1 for cand in c.candidates if cand.source == "nbu")
            ua_n = sum(1 for cand in c.candidates if cand.source == "ua_coins")
            line = f"[fetch-photos]   {c.source_id}: nbu={nbu_n}, ua_coins={ua_n}"
            if c.errors:
                line += f", errors={len(c.errors)}"
            print(line)
            for e in c.errors:
                print(f"[fetch-photos]     WARNING: {e}")
        total_candidates = sum(len(c.candidates) for c in self.cards)
        total_errors = sum(len(c.errors) for c in self.cards)
        print(
            f"[fetch-photos] {len(self.cards)} card(s), {total_candidates} candidate(s) "
            f"on disk, {total_errors} error(s)"
        )


def _get_bytes(client: httpx.Client, pacer: Pacer, url: str) -> bytes:
    pacer.wait()
    resp = client.get(url)
    resp.raise_for_status()
    return resp.content


def _save_candidate(
    dir_: Path, filename: str, content: bytes, url: str, alt: str | None, source: str
) -> FetchCandidate:
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / filename
    path.write_bytes(content)
    with Image.open(path) as img:
        width, height = img.size
    return FetchCandidate(
        file=filename, url=url, alt=alt, source=source, width=width, height=height, bytes=len(content)
    )


def _candidate_from_existing_file(path: Path, url: str, alt: str | None, source: str) -> FetchCandidate:
    with Image.open(path) as img:
        width, height = img.size
    return FetchCandidate(
        file=path.name, url=url, alt=alt, source=source, width=width, height=height, bytes=path.stat().st_size
    )


def _fetch_nbu_candidates(
    client: httpx.Client, pacer: Pacer, card: dict, dir_: Path, refresh: bool, errors: list[str]
) -> list[FetchCandidate]:
    candidates = []
    images = card.get("images") or {}
    # Two NBU sources per side. The listing thumbnail is always there and
    # always 200x200; the full-resolution original behind the page's
    # lightbox link is far better when it exists (nbu:482: 1120x1120) but
    # is present on some cards only, and can 404 even when the page links
    # it (nbu:438). Both go in as ordinary candidates and are ranked on
    # their merits -- a missing or broken big image costs nothing, since
    # the thumbnail is already in the pool.
    wanted = [
        ("obverse", "nbu_obverse", images.get("obverse_url"), False),
        ("reverse", "nbu_reverse", images.get("reverse_url"), False),
        ("obverse", "nbu_big_obverse", images.get("obverse_big_url"), True),
        ("reverse", "nbu_big_reverse", images.get("reverse_big_url"), True),
    ]
    for role, stem, url, optional in wanted:
        if not url:
            continue
        label = f"nbu {'full-size ' if optional else ''}{role}"
        filename = f"{stem}{_ext_from_url(url)}"
        path = dir_ / filename
        if path.exists() and not refresh:
            c = _candidate_from_existing_file(path, url, None, "nbu")
            candidates.append(c)
            print(f"[fetch-photos]     {label}: {c.width}x{c.height} (cached)")
            continue
        try:
            content = _get_bytes(client, pacer, url)
            c = _save_candidate(dir_, filename, content, url, None, "nbu")
            candidates.append(c)
            print(f"[fetch-photos]     {label}: {c.width}x{c.height}, {c.bytes}B <- {url}")
        except Exception as exc:
            # A dead lightbox link is normal, not a fault of this run: NBU
            # links files it no longer serves. It is reported at the same
            # volume as any other skip but never counted as an error, or
            # every such card would show up as a failed fetch.
            msg = f"{label} download failed ({url}): {exc}"
            if optional:
                print(f"[fetch-photos]     skipped: {msg}")
            else:
                errors.append(msg)
                print(f"[fetch-photos]     ERROR: {msg}")
    return candidates


def _parse_ua_coins_gallery(html: str) -> list[dict]:
    """[{big, middle, alt}] for each of the coin's own gallery images, in
    page order. Only the coin's own gallery (data-fancybox="coin-media")
    -- unrelated "similar coin" thumbnails elsewhere on the page use a
    different marker and are not picked up here."""
    tree = HTMLParser(html)
    gallery = []
    for a in tree.css('a[data-fancybox="coin-media"]'):
        big = a.attributes.get("href")
        img = a.css_first("img")
        middle = img.attributes.get("src") if img else None
        alt = (img.attributes.get("alt") if img else None) or a.attributes.get("data-caption")
        if big or middle:
            gallery.append({"big": big, "middle": middle, "alt": alt})
    return gallery


def _fetch_ua_coins_candidates(
    client: httpx.Client, pacer: Pacer, card: dict, dir_: Path, refresh: bool, errors: list[str]
) -> list[FetchCandidate]:
    ua_coins_info = card.get("ua_coins")
    if not ua_coins_info:
        return []

    try:
        resp = ua_coins.get_with_retry(
            client, ua_coins_info["url"].removeprefix(ua_coins.BASE_URL), pacer, log_label=card["source_id"]
        )
        print(f"[fetch-photos]     ua_coins page: {ua_coins_info['url']}")
    except Exception as exc:
        msg = f"ua_coins detail page failed ({ua_coins_info['url']}): {exc}"
        errors.append(msg)
        print(f"[fetch-photos]     ERROR: {msg}")
        return []

    gallery = _parse_ua_coins_gallery(resp.text)
    print(f"[fetch-photos]     ua_coins gallery: {len(gallery)} image(s) found on page")
    candidates = []
    for i, entry in enumerate(gallery, 1):
        urls = [u for u in (entry.get("big"), entry.get("middle")) if u]
        urls = [urljoin(ua_coins.BASE_URL, u) for u in urls]
        urls = list(dict.fromkeys(urls))  # dedupe, keep order
        if not urls:
            continue

        # ua-coins' own "big" variant is sometimes smaller than "middle"
        # (see docs/01_findings.md) -- never trust the folder name, only
        # the real downloaded dimensions.
        ext = _ext_from_url(urls[0])
        filename = f"uacoins_{i:02d}{ext}"
        path = dir_ / filename
        if path.exists() and not refresh:
            c = _candidate_from_existing_file(path, urls[0], entry.get("alt"), "ua_coins")
            candidates.append(c)
            print(f"[fetch-photos]     {filename}: {c.width}x{c.height} (cached, alt={entry.get('alt')!r})")
            continue

        best_content: bytes | None = None
        best_area = -1
        best_url = urls[0]
        best_size = (0, 0)
        for url in urls:
            try:
                resp = ua_coins.get_with_retry(client, url, pacer, log_label=f"{card['source_id']} image")
                content = resp.content
                with Image.open(io.BytesIO(content)) as img:
                    area = img.width * img.height
                    size = img.size
            except Exception as exc:
                msg = f"ua_coins image download failed ({url}): {exc}"
                errors.append(msg)
                print(f"[fetch-photos]     ERROR: {msg}")
                continue
            print(f"[fetch-photos]       variant {size[0]}x{size[1]} <- {url}")
            if area > best_area:
                best_area = area
                best_content = content
                best_url = url
                best_size = size
        if best_content is None:
            msg = f"ua_coins gallery image {i} for {card['source_id']}: all variants failed"
            errors.append(msg)
            print(f"[fetch-photos]     ERROR: {msg}")
            continue
        c = _save_candidate(dir_, filename, best_content, best_url, entry.get("alt"), "ua_coins")
        candidates.append(c)
        print(
            f"[fetch-photos]     {filename}: chose {best_size[0]}x{best_size[1]} "
            f"(alt={entry.get('alt')!r}) <- {best_url}"
        )
    return candidates


def fetch_photos(
    cards: list[dict], series_dir: Path, series: str = "", refresh: bool = False
) -> FetchPhotosSummary:
    nbu_pacer = Pacer(nbu_client.REQUEST_DELAY_RANGE)
    ua_pacer = Pacer(ua_coins.REQUEST_DELAY_RANGE)
    summary = FetchPhotosSummary(series=series)

    print(f"[fetch-photos] series: {series} -- {len(cards)} card(s)")

    with httpx.Client(
        base_url=nbu_client.BASE_URL, headers={"User-Agent": nbu_client.USER_AGENT}, timeout=30.0
    ) as nbu_client_, httpx.Client(
        base_url=ua_coins.BASE_URL, headers={"User-Agent": nbu_client.USER_AGENT}, timeout=30.0
    ) as ua_client:
        for i, card in enumerate(cards, 1):
            source_id = card["source_id"]
            title = (card.get("titles") or {}).get("uk") or "?"
            print(f"[fetch-photos]   ({i}/{len(cards)}) {source_id} {title!r}")
            dir_ = source_dir(series_dir, source_id)
            errors: list[str] = []
            candidates = _fetch_nbu_candidates(nbu_client_, nbu_pacer, card, dir_, refresh, errors)
            candidates += _fetch_ua_coins_candidates(ua_client, ua_pacer, card, dir_, refresh, errors)

            dir_.mkdir(parents=True, exist_ok=True)
            meta = {
                "source_id": source_id,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "candidates": [vars(c) for c in candidates],
            }
            (dir_ / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            summary.cards.append(FetchCardResult(source_id=source_id, candidates=candidates, errors=errors))

    summary.print_report()
    return summary


# ---------------------------------------------------------------------- #
# process_photos -- offline
# ---------------------------------------------------------------------- #


@dataclass
class _Processed:
    candidate: FetchCandidate
    img: Image.Image
    klass: int  # 1 already_transparent, 2 cuttable, 3 kept_bg
    bg_verdict: bg_removal.Verdict
    coin_verdict: coin_classifier.Verdict
    truecolour: bool = True


def _is_truecolour(img: Image.Image) -> bool:
    """Whether `img` holds more than TRUECOLOUR_MIN distinct colours.

    Measured rather than read off the mode, because quantisation is a
    property of the pixels: an 8-bit palette PNG and an RGB file someone
    quantised to 256 colours before saving are the same loss, and only one
    of them says so in its mode. `getcolors` returns None once the ceiling
    is passed, which is exactly the question being asked.
    """
    return img.convert("RGB").getcolors(maxcolors=TRUECOLOUR_MIN) is None


def _classify_background(img: Image.Image) -> tuple[int, bg_removal.Verdict]:
    verdict = bg_removal.classify(img)
    if verdict.reason == "skip:already_transparent":
        return 1, verdict
    if verdict.cut:
        return 2, verdict
    return 3, verdict


def _alpha_mask(img: Image.Image) -> np.ndarray:
    alpha = np.array(img.convert("RGBA").split()[-1])
    return (alpha > MASK_ALPHA_THRESHOLD).astype(np.uint8) * 255


def _outline_dip_fraction(img: Image.Image) -> float | None:
    """Share of a round coin's outline sitting inside its own radial
    envelope -- see OUTLINE_DIP_TOLERANCE -- or None when the coin is not
    round.

    The measure is deviation from a circle, so on a pysanka or a half-heart
    it reports the shape itself and lands near 1.0. That is not a defect
    reading and must not be logged as one, so a non-disc outline returns no
    number at all rather than a number that means something else. "Disc" is
    decided exactly as bg_removal decides it, by the spread of the buckets
    that reach the envelope: measured 0.0001-0.0086 for real round coins
    against 0.050 for the pysanka and 0.111 for a half-heart.
    """
    mask = _alpha_mask(img)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    largest = max(contours, key=cv2.contourArea)
    moments = cv2.moments(largest)
    if moments["m00"] == 0:
        return None
    cx, cy = moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]
    points = largest.reshape(-1, 2).astype(float)
    radius = np.hypot(points[:, 0] - cx, points[:, 1] - cy)
    angle = (np.degrees(np.arctan2(points[:, 1] - cy, points[:, 0] - cx)) + 360.0) % 360.0
    profile = np.full(360, -1.0)
    np.maximum.at(profile, np.minimum(angle.astype(int), 359), radius)
    known = profile[profile >= 0]
    if known.size == 0:
        return None
    # Same envelope percentile as bg_removal's own disc fit, so "is this a
    # disc" gets the same answer in both modules. A higher percentile would
    # make the test meaningless: `outer` would then be a handful of
    # self-selected top buckets whose spread is small for any shape at all.
    envelope = float(np.percentile(known, bg_removal.OUTLINE_ENVELOPE_PERCENTILE))
    if envelope <= 0:
        return None
    outer = known[known >= envelope]
    if outer.size < 2 or float(np.std(outer) / envelope) > bg_removal.OUTLINE_DISC_SPREAD_MAX:
        return None  # not a disc -- the measure would not mean what it says
    return float(((envelope - known) / envelope > OUTLINE_DIP_TOLERANCE).mean())


def _silhouette(img: Image.Image, size: int = 256) -> np.ndarray | None:
    """The image's foreground, cropped to its bbox and scaled to a fixed
    square, so two photos of the same coin become directly comparable
    regardless of framing or resolution."""
    mask = _alpha_mask(img)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    xs = np.concatenate([c.reshape(-1, 2)[:, 0] for c in contours])
    ys = np.concatenate([c.reshape(-1, 2)[:, 1] for c in contours])
    cropped = mask[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    if cropped.size == 0:
        return None
    return cv2.resize(cropped, (size, size), interpolation=cv2.INTER_NEAREST) > 0


def _silhouette_iou(first: Image.Image, second: Image.Image) -> float | None:
    a, b = _silhouette(first), _silhouette(second)
    if a is None or b is None:
        return None
    union = (a | b).sum()
    return float((a & b).sum() / union) if union else None


def _write_sizes(
    img: Image.Image, dir_: Path, role: str, source_smaller_side: int | None = None
) -> tuple[list[dict], bool]:
    """One WebP per OUTPUT_SIZES tier, each at the largest resolution the
    source actually offers up to that tier's label. Never upscales.

    The tier numbers are labels, not promises: `{role}_1200.webp` is the
    file to fetch when you want the big one, and it is 1200px wide only if
    the source was. So every returned entry carries its real dimensions --
    a consumer must read those rather than parse the filename. (This was
    always true at the bottom end, where a 200x200 NBU original ships as
    `{role}_300.webp`; it is now true at the top end too, which is the
    point of the change -- see TIER_MIN_GAIN.)

    A tier that would gain almost nothing over the file already written is
    skipped, so a small source yields one file rather than three copies of
    itself, and the returned list is always strictly increasing in size.

    `source_smaller_side` is the *original* photo's short side. low_res is
    judged on that rather than on what arrives here: cut_background trims
    to the alpha bbox, and a 600x600 source that came out 600x598 would
    otherwise be flagged low-res over two pixels of cropped margin -- which
    is exactly what happened to nbu:161's reverse while its neighbours,
    trimmed a pixel less, were not.
    """
    dir_.mkdir(parents=True, exist_ok=True)
    larger_side = max(img.width, img.height)
    if source_smaller_side is None:
        source_smaller_side = min(img.width, img.height)

    files: list[dict] = []
    written_side = 0
    for size in OUTPUT_SIZES:
        target = min(size, larger_side)  # never upscale
        if target <= written_side * (1 + TIER_MIN_GAIN):
            continue  # nothing meaningful to gain over the previous tier
        scale = target / larger_side
        new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        resized = img if scale == 1.0 else img.resize(new_size, Image.Resampling.LANCZOS)
        filename = f"{role}_{size}.webp"
        resized.save(dir_ / filename, "WEBP", quality=WEBP_QUALITY)
        files.append({"file": filename, "width": resized.width, "height": resized.height})
        written_side = target

    return files, source_smaller_side < LOW_RES_THRESHOLD


def _rank_key(p: _Processed) -> tuple[int, int, int, int, int]:
    """(background class, shape verdict, -useful resolution, -colour depth,
    -actual resolution).

    Background class still dominates: a small already-transparent photo
    beats a large one that needs cutting. The shape verdict only breaks
    ties within a class, and it *demotes* rather than excludes -- see the
    ranking-not-a-gate contract in collector/core/coin_classifier.py. A
    photo the classifier dislikes is still shipped when it is the only one
    the card has, which is what nbu:161's obverse needed: a correctly
    labelled 600x600 "Аверс" was thrown away by the old hard gate and the
    card went out with one side.

    Resolution is capped at the largest tier that gets written before it is
    compared, because resolution past that point is discarded on the way
    out and must not outrank something that is not discarded. Colour depth
    is what it must not outrank: NBU's full-resolution originals are
    8-bit palette PNGs, so nbu:482 has two candidates at an identical
    1120x1120 with an identical alpha channel, one holding 230 colours and
    the other 48015. Without this term the tie fell to whichever source was
    fetched first, which is not a judgement about the photo at all.
    """
    smaller_side = min(p.img.width, p.img.height)
    return (
        p.klass,
        0 if p.coin_verdict.is_coin else 1,
        -min(smaller_side, max(OUTPUT_SIZES)),
        0 if p.truecolour else 1,
        -smaller_side,
    )


def _process_role(
    role: str,
    pool: list[_Processed],
    out_dir_: Path,
    source_id: str,
    warnings: list[str],
    disqualified: list[dict],
) -> tuple[dict, Image.Image] | None:
    """The best usable photo for `role`, written out, plus the finished
    image so the caller can cross-check it against the other side.

    The pool is walked in rank order with no pre-filtering. Every candidate
    stays reachable: if the best one's cut damages the coin, the next
    candidate is tried, and a photo that can only ship with its background
    is still shipped rather than dropped -- the old pre-filter narrowed the
    pool to cuttable/transparent candidates up front, so a card whose only
    cuttable photo failed lost the role entirely instead of falling back.
    """
    ranked = sorted(pool, key=_rank_key)

    while ranked:
        candidate = ranked.pop(0)
        if candidate.klass == 2:
            cut = bg_removal.cut_background(candidate.img, candidate.bg_verdict.mask)
            # The same shape bar the classifier applies to a photo, applied
            # to what the cut produced. It cannot detect a coin trimmed
            # evenly all round (no shape test can -- see PAIR_SILHOUETTE_MIN)
            # but it does catch a flood fill that leaked through the coin
            # and left a sliver, a ring or two disconnected pieces.
            shape = coin_classifier.classify_mask(_alpha_mask(cut))
            if not shape.is_coin:
                disqualified.append(
                    {
                        "src_file": candidate.candidate.file,
                        "reason": "cut damaged the shape",
                        "detail": shape.reason,
                        "solidity": round(shape.worst_solidity, 3),
                        "extent": round(shape.worst_extent, 3),
                        "aspect": round(shape.worst_aspect, 3),
                    }
                )
                print(
                    f"[process-photos]       {candidate.candidate.file}: DISQUALIFIED after cut, "
                    f"{shape.reason} (solidity={shape.worst_solidity:.3f} "
                    f"extent={shape.worst_extent:.3f} aspect={shape.worst_aspect:.3f})"
                    f" -- trying next candidate"
                )
                continue
            final_img, processing = cut, "cut"
        elif candidate.klass == 1:
            final_img, processing = bg_removal.trim_to_alpha(candidate.img.convert("RGBA")), "already_transparent"
        else:
            final_img, processing = candidate.img, "kept_bg"
            warnings.append(f"{source_id} {role}: photo kept with background ({candidate.candidate.file})")

        files, low_res = _write_sizes(
            final_img, out_dir_, role, min(candidate.img.width, candidate.img.height)
        )
        dip = _outline_dip_fraction(final_img) if processing != "kept_bg" else None
        # `dip is None` means "not measurable here" (kept background, or a
        # coin that is not a disc), never "measured zero".
        print(
            f"[process-photos]       {role} winner: {candidate.candidate.file} "
            f"({processing}, final {final_img.width}x{final_img.height}"
            f"{f', outline_dip={dip:.3f}' if dip is not None else ''}"
            f"{', low_res' if low_res else ''}) -> "
            + ", ".join(f"{f['file']}@{max(f['width'], f['height'])}" for f in files)
        )
        return (
            {
                "winner": {
                    "source": candidate.candidate.source,
                    "src_file": candidate.candidate.file,
                    "width": candidate.img.width,
                    "height": candidate.img.height,
                    "processing": processing,
                    "is_coin": candidate.coin_verdict.is_coin,
                    "shape_reason": candidate.coin_verdict.reason,
                },
                "files": files,
                "low_res": low_res,
                "outline_dip": None if dip is None else round(dip, 4),
            },
            final_img,
        )

    return None


@dataclass
class ProcessCardResult:
    source_id: str
    roles: dict = field(default_factory=dict)
    rejected: list[dict] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    pair_silhouette_iou: float | None = None


@dataclass
class ProcessPhotosSummary:
    series: str
    cards: list[ProcessCardResult] = field(default_factory=list)

    def print_report(self) -> None:
        print(f"[process-photos] series: {self.series}")
        complete = 0
        for c in self.cards:
            parts = []
            for role in ("obverse", "reverse"):
                r = c.roles.get(role)
                if r:
                    w = r["winner"]
                    tag = " low_res" if r["low_res"] else ""
                    if not w["is_coin"]:
                        tag += f" shape:{w['shape_reason']}"
                    if r["outline_dip"]:
                        tag += f" dip {r['outline_dip']:.3f}"
                    parts.append(f"{role}: {w['source']} {w['width']}x{w['height']} {w['processing']}{tag}")
                else:
                    parts.append(f"{role}: MISSING")
            if c.roles.get("obverse") and c.roles.get("reverse"):
                complete += 1
            disq_n = sum(1 for r in c.rejected if r["reason"] == "cut damaged the shape")
            if disq_n:
                parts.append(f"disqualified {disq_n}")
            if c.pair_silhouette_iou is not None:
                parts.append(f"pair IoU {c.pair_silhouette_iou:.3f}")
            line = f"[process-photos]   {c.source_id} | " + " | ".join(parts)
            print(line)
            for a in c.anomalies:
                print(f"[process-photos]     ANOMALY: {a}")
        total_anomalies = sum(len(c.anomalies) for c in self.cards)
        total_disqualified = sum(
            1 for c in self.cards for r in c.rejected if r["reason"] == "cut damaged the shape"
        )
        total_rejected = sum(len(c.rejected) for c in self.cards)
        print(
            f"[process-photos] complete pairs {complete}/{len(self.cards)}, "
            f"anomalies: {total_anomalies}, disqualified: {total_disqualified}, "
            f"rejected candidates (total): {total_rejected}"
        )


_KLASS_NAME = {1: "already_transparent", 2: "cuttable", 3: "kept_bg"}


def process_photos(cards: list[dict], series_dir: Path, series: str = "") -> ProcessPhotosSummary:
    summary = ProcessPhotosSummary(series=series)

    print(f"[process-photos] series: {series} -- {len(cards)} card(s)")

    for i, card in enumerate(cards, 1):
        source_id = card["source_id"]
        title = (card.get("titles") or {}).get("uk") or "?"
        print(f"[process-photos]   ({i}/{len(cards)}) {source_id} {title!r}")
        src_dir = source_dir(series_dir, source_id)
        meta_path = src_dir / "meta.json"
        result = ProcessCardResult(source_id=source_id)

        if not meta_path.exists():
            msg = f"no fetched photos for {source_id} -- run fetch-photos first"
            result.anomalies.append(msg)
            print(f"[process-photos]     ANOMALY: {msg}")
            summary.cards.append(result)
            continue

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        candidates = [FetchCandidate(**c) for c in meta.get("candidates", [])]

        godny: list[_Processed] = []
        for cand in candidates:
            path = src_dir / cand.file
            try:
                img = Image.open(path)
                img.load()
            except Exception as exc:
                reason = f"unreadable: {exc}"
                result.rejected.append({"src_file": cand.file, "reason": reason})
                print(f"[process-photos]     {cand.file}: REJECTED ({reason})")
                continue
            # A failing shape verdict demotes the candidate in _rank_key; it
            # never removes it. The classifier is a ranker (see its module
            # docstring) and treating it as a gate is what cost nbu:161 its
            # obverse -- a correctly labelled "Аверс" that the old
            # perimeter-based roundness test scored 0.281 on because the
            # coin's highlights shredded the mask outline.
            coin_verdict = coin_classifier.classify(path)
            klass, bg_verdict = _classify_background(img)
            repair_tag = ", outline-repaired" if (bg_verdict.metrics or {}).get("outlineRepaired") else ""
            print(
                f"[process-photos]     {cand.file}: {img.width}x{img.height} "
                f"shape={'ok' if coin_verdict.is_coin else coin_verdict.reason} "
                f"(solidity={coin_verdict.worst_solidity:.3f} extent={coin_verdict.worst_extent:.3f} "
                f"aspect={coin_verdict.worst_aspect:.3f}) "
                f"-> bg class {klass} ({_KLASS_NAME[klass]}, {bg_verdict.reason or 'cut'}{repair_tag})"
            )
            godny.append(
                _Processed(
                    candidate=cand,
                    img=img,
                    klass=klass,
                    bg_verdict=bg_verdict,
                    coin_verdict=coin_verdict,
                    truecolour=_is_truecolour(img),
                )
            )

        role_pools: dict[str, list[_Processed]] = {"obverse": [], "reverse": []}
        unassigned: list[_Processed] = []
        for p in godny:
            role = _role_from_text(p.candidate.file) or _role_from_text(p.candidate.alt)
            if role:
                role_pools[role].append(p)
            else:
                unassigned.append(p)
        if unassigned:
            print(
                f"[process-photos]     unassigned by metadata: "
                f"{[p.candidate.file for p in unassigned]}"
            )

        for role in ("obverse", "reverse"):
            if role_pools[role]:
                continue
            # Scored from the verdict computed above, not by re-classifying:
            # classify() re-decodes the file from disk, and calling it from
            # inside a sort key ran it O(n log n) times over work already done.
            eligible = sorted(
                unassigned,
                key=lambda p: coin_classifier.score(p.coin_verdict),
                reverse=True,
            )
            if eligible:
                best = eligible[0]
                role_pools[role].append(best)
                unassigned.remove(best)
                print(f"[process-photos]     {role}: filled by geometry tiebreak -> {best.candidate.file}")

        disqualified: list[dict] = []
        finished: dict[str, Image.Image] = {}
        for role in ("obverse", "reverse"):
            pool = role_pools[role]
            if not pool:
                result.anomalies.append(f"no usable photo for {role}")
                continue
            print(
                f"[process-photos]     {role} ranking: "
                + ", ".join(
                    f"{p.candidate.file}(class {p.klass}"
                    f"{'' if p.coin_verdict.is_coin else ', shape ' + p.coin_verdict.reason}"
                    f", {min(p.img.width, p.img.height)}px"
                    f"{'' if p.truecolour else ', paletted'})"
                    for p in sorted(pool, key=_rank_key)
                )
            )
            role_result = _process_role(
                role, pool, out_dir(series_dir, source_id), source_id, result.anomalies, disqualified
            )
            if role_result:
                result.roles[role], finished[role] = role_result
            else:
                result.anomalies.append(f"no usable photo for {role}")

        # Both sides of one coin share an outline, so they check each other:
        # a cut that quietly ate one side shows up as the pair disagreeing,
        # with no assumption about what the coin's shape should be.
        if len(finished) == 2:
            iou = _silhouette_iou(finished["obverse"], finished["reverse"])
            result.pair_silhouette_iou = None if iou is None else round(iou, 4)
            if iou is not None and iou < PAIR_SILHOUETTE_MIN:
                msg = (
                    f"obverse and reverse outlines disagree (IoU {iou:.3f} < "
                    f"{PAIR_SILHOUETTE_MIN}) -- one side was probably cut wrong"
                )
                result.anomalies.append(msg)
                print(f"[process-photos]     ANOMALY: {msg}")
            else:
                print(f"[process-photos]     pair silhouette IoU: {iou:.4f}")

        for d in disqualified:
            result.rejected.append(d)

        for p in godny:
            p.img.close()

        summary.cards.append(result)

    photos_json = {
        "cards": [
            {
                "source_id": c.source_id,
                "roles": c.roles,
                "rejected": c.rejected,
                "anomalies": c.anomalies,
                "pair_silhouette_iou": c.pair_silhouette_iou,
            }
            for c in summary.cards
        ]
    }
    (series_dir / "parsed" / "photos.json").write_text(
        json.dumps(photos_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summary.print_report()
    return summary
