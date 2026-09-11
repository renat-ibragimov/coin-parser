"""ua-coins.info price history -- the fetch half (network).

ua-coins renders a price chart on every coin page, but the chart's data
comes from a SEPARATE, SIGNED endpoint:

    GET /coin/prices/{ua_coins_id}?_hash=...&expires=...

The signature is minted by the server while it renders the coin page and
is good for roughly two days, so the URL can be neither constructed nor
cached between runs -- it has to be re-read out of the coin page's HTML
on every collection pass. That is the whole reason this step downloads
two documents per coin instead of one.

Prices do not depend on the NBU series a coin happens to belong to, so
the staging cache is shared, not per-series. Files are named after the
NBU card rather than the ua-coins row, because ua-coins ids look exactly
like years (43 and 1998 are both coins of 1999/2000 in the pilot series)
and because the card is what the cache actually has to answer for:

    staging/ua/_ua_coins/pages/nbu_161.html        the coin page it came from
    staging/ua/_ua_coins/prices/nbu_161.json       the endpoint's response, verbatim
    staging/ua/_ua_coins/prices/nbu_161.meta.json  ua_coins_id, control point, stats

Known properties of the data, which the checks here are written around
(see docs/01_findings.md):
  * gaps in the dates are NORMAL -- a missing day, a missing three weeks;
    there is deliberately no continuity check;
  * single-day spikes (520 -> 650 -> 520) are NORMAL -- the market is
    thin, outliers are legitimate, nothing is flagged or smoothed;
  * dates are strictly increasing. Losing that order, or a date/price
    that does not parse, means the file is not what we think it is --
    the whole coin becomes an anomaly and none of its points load.
"""

from __future__ import annotations

import html as html_module
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
from selectolax.parser import HTMLParser

from collector.core.pacing import Pacer
from collector.countries.ua import ua_coins
from collector.countries.ua.nbu_client import USER_AGENT
from collector.countries.ua.parsing import to_decimal

PRICES_PATH = "/coin/prices/{id}"

# Numeric(14,2) in coin_keeper -- 12 digits before the point. A price
# past that would abort the whole load transaction on overflow, so it is
# caught here as a structural "not a number this column can hold"
# rather than left to blow up mid-COPY.
MAX_PRICE = Decimal("10") ** 12


def prices_dir(staging_root: Path) -> Path:
    return staging_root / "ua" / "_ua_coins" / "prices"


def pages_dir(staging_root: Path) -> Path:
    return staging_root / "ua" / "_ua_coins" / "pages"


def file_stem(source_id: str) -> str:
    """"nbu:161" -> "nbu_161". Same convention photos.py uses for its
    per-card directories -- a colon is not a portable filename."""
    return source_id.replace(":", "_")


def prices_path(staging_root: Path, source_id: str) -> Path:
    return prices_dir(staging_root) / f"{file_stem(source_id)}.json"


def meta_path(staging_root: Path, source_id: str) -> Path:
    return prices_dir(staging_root) / f"{file_stem(source_id)}.meta.json"


def page_path(staging_root: Path, source_id: str) -> Path:
    return pages_dir(staging_root) / f"{file_stem(source_id)}.html"


def cached_ua_coins_id(staging_root: Path, source_id: str) -> int | None:
    """The ua-coins row a cached price file was fetched from, or None if
    there is no readable meta beside it. See _fetch_one for why the
    cache has to ask."""
    path = meta_path(staging_root, source_id)
    if not path.exists():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = meta.get("ua_coins_id")
    return int(value) if isinstance(value, int) else None


# ---------------------------------------------------------------------- #
# extracting the signed chart URL out of a coin page (pure)
# ---------------------------------------------------------------------- #


@dataclass
class PricesEndpoint:
    path: str  # "/coin/prices/2449?_hash=...&expires=..." -- as found, query kept
    coin_id: int
    signed: bool  # False = found without a query string, almost certainly useless


def _prices_pattern(id_part: str) -> re.Pattern:
    # The href may be absolute or root-relative, and may sit in an
    # attribute or an inline script, so the host part is optional and the
    # query runs to the first character that cannot appear inside a URL
    # in either context (quote, angle bracket, whitespace, brace).
    return re.compile(
        r"(?:https?://[A-Za-z0-9.\-]*ua-coins\.info)?"
        r"(/coin/prices/(" + id_part + r")(?:\?[^\s\"'<>\\`{}]+)?)"
    )


def extract_prices_url(page_html: str, ua_coins_id: int) -> PricesEndpoint | None:
    """The signed chart endpoint for `ua_coins_id`, as found in the coin
    page's markup. Returns None if the page has no such link at all.

    The search is over the whole document rather than one selector,
    because the link can legitimately live in an attribute, in an inline
    script, or inside a JSON blob in a data- attribute -- three places
    with three different escapings. Both HTML entities (&amp;, &quot;)
    and JavaScript's escaped slashes (\\/) are undone first so a single
    pattern covers all of them.

    A page carrying the endpoint for a DIFFERENT coin id is accepted as a
    last resort (with signed/coin_id reported so the caller can warn):
    an id mismatch is worth seeing in the log, not worth losing the
    history over.
    """
    text = html_module.unescape(page_html).replace("\\/", "/")

    for id_part in (re.escape(str(ua_coins_id)), r"\d+"):
        matches = list(_prices_pattern(id_part).finditer(text))
        if not matches:
            continue
        # Prefer a signed hit: an unsigned /coin/prices/{id} with no query
        # is what a template stub or a stray link looks like.
        signed = [m for m in matches if "?" in m.group(1)]
        chosen = (signed or matches)[0]
        return PricesEndpoint(
            path=chosen.group(1),
            coin_id=int(chosen.group(2)),
            signed="?" in chosen.group(1),
        )
    return None


# ---------------------------------------------------------------------- #
# the page's own "current price" -- the control point (pure)
# ---------------------------------------------------------------------- #

# "12 500,50 грн" and "12500 грн" are the same price written two ways --
# the alternation is there because a run of digits with no thousands
# separator is the commoner form, and a pattern built only around
# separated groups matches its first three digits and then fails.
_PRICE_TEXT_RE = re.compile(
    r"(?<![\d.,])"
    r"((?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?)"
    r"\s*(?:грн|₴|UAH)",
    re.IGNORECASE,
)
_DATE_DMY_RE = re.compile(r"\b(\d{2})[.\-/](\d{2})[.\-/](\d{4})\b")
_DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _find_date(text: str) -> date | None:
    m = _DATE_ISO_RE.search(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _DATE_DMY_RE.search(text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


@dataclass
class CurrentPrice:
    price: Decimal
    date: date | None
    text: str  # the text it was read out of, for the log/meta
    found_by: str  # "price-note" | "as-of-sentence" | "scan"


# ua-coins prints the control point in a section of its own:
#
#   <p class="coin-price-note__text">
#       Орієнтовна ринкова ціна станом на 09.09.2026 — 568 грн.
#       Від 18.07.2016 ринкова ціна зросла з 188 до 568 грн. — на 202.1%.
#   </p>
#
# Note that the same paragraph also carries the price and date the coin
# started from, so the "станом на" clause -- not the paragraph -- is what
# actually identifies the current price. Anchoring on the sentence rather
# than on the element is also what keeps this working if the class is
# renamed: the selector is only a shortcut to the right paragraph.
_PRICE_NOTE_SELECTOR = "p.coin-price-note__text, .coin-price-note__text"
_NUMBER = r"(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?"
_AS_OF_RE = re.compile(
    r"станом\s+на\s+(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4}|\d{4}-\d{2}-\d{2})"
    r"[^\d]{0,10}?"
    r"(" + _NUMBER + r")\s*(?:грн|₴|UAH)",
    re.IGNORECASE,
)


def _as_of(text: str, found_by: str) -> CurrentPrice | None:
    m = _AS_OF_RE.search(text)
    if m is None:
        return None
    value = to_decimal(m.group(2))
    if value is None or value <= 0:
        return None
    return CurrentPrice(
        price=value,
        date=_find_date(m.group(1)),
        text=" ".join(text.split())[:200],
        found_by=found_by,
    )


def _scan_for_prices(page_html: str) -> list[CurrentPrice]:
    """Every "<number> грн/₴/UAH" on the page, dated from its own element
    or its parent. Diagnostics, and the last-resort guess: the page is
    full of these -- the coin's face value, what it cost at the NBU, the
    range dealers ask -- so a hit here is never trusted on its own."""
    tree = HTMLParser(page_html)
    candidates: list[CurrentPrice] = []
    seen: set[tuple] = set()

    for node in tree.css("body *") or tree.css("*"):
        text = (node.text(deep=False) or "").strip()
        if not text:
            continue
        m = _PRICE_TEXT_RE.search(text)
        if m is None:
            continue
        value = to_decimal(m.group(1))
        if value is None or value <= 0:
            continue
        when = _find_date(text)
        if when is None:
            parent = node.parent
            if parent is not None:
                when = _find_date(parent.text() or "")
        key = (value, when)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            CurrentPrice(
                price=value, date=when, text=" ".join(text.split())[:120], found_by="scan"
            )
        )
    return candidates


def extract_current_price(page_html: str) -> tuple[CurrentPrice | None, list[CurrentPrice]]:
    """(the page's own current price, every price-shaped string on the page).

    Read from the "станом на <date> — <price> грн" sentence, looked for
    first inside the price-note section and then across the whole page.
    An earlier version instead took the first dated "<number> грн" on the
    page, and that is the coin's FACE VALUE: the h1 reads
    "Монета «Різдво Христове» 5 грн., 1999, нейзильбер", so the control
    point compared 5 against a chart point of 568 and every coin came
    back mismatched.

    The generic scan survives as the last resort and, more usefully, as
    the candidate list written into the meta file -- when the sentence
    stops matching, the meta shows what the page actually says instead of
    reporting a bare "not found".
    """
    tree = HTMLParser(page_html)

    for node in tree.css(_PRICE_NOTE_SELECTOR):
        found = _as_of(node.text() or "", "price-note")
        if found is not None:
            return found, _scan_for_prices(page_html)

    body = tree.css_first("body")
    found = _as_of((body.text() if body else "") or "", "as-of-sentence")
    candidates = _scan_for_prices(page_html)
    if found is not None:
        return found, candidates

    dated = [c for c in candidates if c.date is not None]
    return (dated[0] if dated else (candidates[0] if candidates else None)), candidates


# ua-coins shows this widget in place of a market price while a brand-new
# coin has no confirmed resale yet:
#
#   <div class="market-pending">
#     <div class="market-pending__lead">
#       <span class="market-pending__price">7 836 грн</span>
#       <span class="market-pending__price-label">офіційна ціна НБУ на 11.09.2026</span>
#       <span class="market-pending__status">ринкова ціна ще не сформована</span>
#     </div> ...
#   </div>
#
# This is the mint's own issue price, not a market quote -- kept out of
# extract_current_price() on purpose, so a caller has to ask for it by
# name rather than get it back silently mixed in with real ua-coins
# prices (see load_prices.py's NBU_ISSUE_SOURCE).
_NBU_PENDING_SELECTOR = ".market-pending"
_NBU_PENDING_PRICE_SELECTOR = ".market-pending__price"
_NBU_PENDING_LABEL_SELECTOR = ".market-pending__price-label"


def extract_nbu_issue_price(page_html: str) -> CurrentPrice | None:
    """The NBU's own issue price for a coin ua-coins has not priced yet.

    Read only from the market-pending widget's own markup -- never the
    generic "<number> грн" scan extract_current_price() falls back to,
    because this page always has at least one OTHER "<number> грн" on
    it (the face value in the h1) that a blind scan cannot tell apart
    from the real thing. ua-coins stops rendering the widget the moment
    a real market price exists, so its mere presence is the signal.
    """
    tree = HTMLParser(page_html)
    block = tree.css_first(_NBU_PENDING_SELECTOR)
    if block is None:
        return None
    price_node = block.css_first(_NBU_PENDING_PRICE_SELECTOR)
    if price_node is None:
        return None
    # This widget spaces its thousands with a thin space (U+2009), not
    # the  /  the rest of the site's price text uses -- folded
    # to a plain space here rather than in the shared _PRICE_TEXT_RE,
    # which every other price reading on the page still has to match
    # as the site actually writes it.
    price_text = (price_node.text() or "").replace(" ", " ")
    m = _PRICE_TEXT_RE.search(price_text)
    if m is None:
        return None
    price = to_decimal(m.group(1))
    if price is None or price <= 0:
        return None
    label_node = block.css_first(_NBU_PENDING_LABEL_SELECTOR)
    label_text = (label_node.text() if label_node else "") or ""
    return CurrentPrice(
        price=price,
        date=_find_date(label_text),
        text=" ".join(label_text.split())[:200] or price_text.strip(),
        found_by="nbu-issue-pending",
    )


# ---------------------------------------------------------------------- #
# the chart response itself (pure)
# ---------------------------------------------------------------------- #


class PriceSeriesAnomaly(ValueError):
    """The response is not a price series we are willing to load. Every
    raise here takes the WHOLE coin out of the load -- never a single
    point -- so that a file we have misread can't dribble half-truths
    into the production history."""


@dataclass
class PricePoint:
    day: date
    price: Decimal
    raw: dict


def _coerce_price(value: object, index: int) -> Decimal:
    # bool is an int in Python; a JSON `true` here would silently become 1.
    if isinstance(value, bool):
        raise PriceSeriesAnomaly(f"point {index}: price is a boolean, not a number")
    if isinstance(value, (int, float)):
        try:
            price = Decimal(str(value))
        except InvalidOperation:
            raise PriceSeriesAnomaly(f"point {index}: price {value!r} is not a finite number")
        if not price.is_finite():
            raise PriceSeriesAnomaly(f"point {index}: price {value!r} is not a finite number")
    elif isinstance(value, str):
        # Not seen in the wild, but a server that starts quoting its
        # numbers should not cost us the whole history.
        price = to_decimal(value.strip())
        if price is None:
            raise PriceSeriesAnomaly(f"point {index}: price {value!r} does not parse as a number")
    else:
        raise PriceSeriesAnomaly(f"point {index}: price {value!r} is not a number")

    if price <= 0:
        raise PriceSeriesAnomaly(f"point {index}: price {value!r} is not > 0")
    if price >= MAX_PRICE:
        raise PriceSeriesAnomaly(f"point {index}: price {value!r} does not fit numeric(14,2)")
    return price


def parse_price_series(payload: object) -> list[PricePoint]:
    """The endpoint's decoded JSON -> points, or PriceSeriesAnomaly.

    Structural checks only, and that is a decision, not an omission: the
    market this data comes from is thin enough that a coin doubling for
    one day and coming straight back is a real trade, not noise. Nothing
    here looks at the SHAPE of the curve -- no outlier flagging, no gap
    filling, no smoothing. Only "is this a list of dated, positive,
    chronologically ordered prices".
    """
    if not isinstance(payload, list):
        raise PriceSeriesAnomaly(f"response is a {type(payload).__name__}, expected a JSON array")

    points: list[PricePoint] = []
    previous: date | None = None
    for i, item in enumerate(payload):
        if not isinstance(item, dict):
            raise PriceSeriesAnomaly(f"point {i}: {type(item).__name__}, expected an object")
        if "date" not in item or "price" not in item:
            raise PriceSeriesAnomaly(f"point {i}: missing 'date' or 'price' key")

        raw_date = item["date"]
        if not isinstance(raw_date, str):
            raise PriceSeriesAnomaly(f"point {i}: date {raw_date!r} is not a string")
        try:
            day = date.fromisoformat(raw_date.strip())
        except ValueError:
            raise PriceSeriesAnomaly(f"point {i}: date {raw_date!r} is not an ISO date")

        if previous is not None and day <= previous:
            raise PriceSeriesAnomaly(
                f"point {i}: date {day.isoformat()} does not come after {previous.isoformat()} "
                "(dates must be strictly increasing)"
            )
        previous = day

        points.append(PricePoint(day=day, price=_coerce_price(item["price"], i), raw=item))

    return points


def load_price_file(path: Path) -> list[PricePoint]:
    """Read one staged {id}.json and validate it. JSON that does not decode
    is an anomaly like any other -- same rule, whole coin out."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PriceSeriesAnomaly(f"{path.name} is not valid JSON: {exc}")
    return parse_price_series(payload)


# ---------------------------------------------------------------------- #
# fetch-prices -- network
# ---------------------------------------------------------------------- #


@dataclass
class FetchPriceResult:
    source_id: str
    ua_coins_id: int | None
    status: str  # "fetched" | "cached" | "skipped:unmatched" | "error"
    points: int = 0
    date_min: str | None = None
    date_max: str | None = None
    note: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class FetchPricesSummary:
    series: str
    results: list[FetchPriceResult] = field(default_factory=list)

    def print_report(self) -> None:
        print(f"[fetch-prices] series: {self.series}")
        if self.results:
            print(
                f"[fetch-prices]   {'source_id':<10} {'ua_coins':<9} {'status':<18} "
                f"{'points':>6}  date range"
            )
        for r in self.results:
            span = (
                f"{r.date_min} .. {r.date_max}" if r.date_min and r.date_max else (r.note or "")
            )
            print(
                f"[fetch-prices]   {r.source_id:<10} {str(r.ua_coins_id or '-'):<9} "
                f"{r.status:<18} {r.points:>6}  {span}"
            )
            if r.note and r.date_min:
                print(f"[fetch-prices]     note: {r.note}")
            for w in r.warnings:
                print(f"[fetch-prices]     WARNING: {w}")

        fetched = sum(1 for r in self.results if r.status == "fetched")
        cached = sum(1 for r in self.results if r.status == "cached")
        unmatched = sum(1 for r in self.results if r.status == "skipped:unmatched")
        errors = sum(1 for r in self.results if r.status == "error")
        total_points = sum(r.points for r in self.results)
        days = [r.date_min for r in self.results if r.date_min] + [
            r.date_max for r in self.results if r.date_max
        ]
        span = f"{min(days)} .. {max(days)}" if days else "-"
        print(
            f"[fetch-prices] fetched {fetched}, cached {cached}, "
            f"skipped:unmatched {unmatched}, errors {errors}"
        )
        print(f"[fetch-prices] {total_points} point(s) on disk for this series, dates {span}")
        if errors:
            print("[fetch-prices] rerun to retry the failures -- coins already on disk are skipped")


def _stats(points: list[PricePoint]) -> dict:
    if not points:
        return {"points": 0, "date_min": None, "date_max": None, "price_min": None, "price_max": None}
    return {
        "points": len(points),
        "date_min": points[0].day.isoformat(),
        "date_max": points[-1].day.isoformat(),
        "price_min": str(min(p.price for p in points)),
        "price_max": str(max(p.price for p in points)),
    }


def check_control_point(
    points: list[PricePoint], current: CurrentPrice | None
) -> tuple[str, str | None]:
    """(verdict, warning) comparing the page's printed price against the
    last point of the chart. They are two renderings of the same fact, so
    a disagreement means one of the two readings is wrong -- but the
    chart is the source we load, so this only ever warns."""
    if current is None:
        return "not_found", "no current price found on the coin page -- control point not checked"
    if not points:
        return "no_points", "chart returned no points, nothing to check the page price against"

    last = points[-1]
    problems = []
    if current.price != last.price:
        problems.append(f"page says {current.price}, last chart point is {last.price}")
    if current.date is not None and current.date != last.day:
        problems.append(
            f"page price dated {current.date.isoformat()}, last chart point is {last.day.isoformat()}"
        )
    if not problems:
        return "ok", None
    return "mismatch", "control point mismatch: " + "; ".join(problems)


def _fetch_one(
    client: httpx.Client,
    pacer: Pacer,
    staging_root: Path,
    source_id: str,
    coin_id: int,
    page_url: str,
    result: FetchPriceResult,
) -> None:
    saved_page = page_path(staging_root, source_id)

    page_resp = ua_coins.get_with_retry(
        client, page_url.removeprefix(ua_coins.BASE_URL), pacer, log_label=source_id
    )
    page_html = page_resp.text
    pages_dir(staging_root).mkdir(parents=True, exist_ok=True)
    saved_page.write_text(page_html, encoding="utf-8")
    print(f"[fetch-prices]     page: {page_url} ({len(page_html)} bytes -> {saved_page})")

    endpoint = extract_prices_url(page_html, coin_id)
    if endpoint is None:
        # A brand-new coin has no chart at all yet -- ua-coins does not
        # even embed the /coin/prices/ link until there is a first point
        # to plot -- and that is not the markup-changed error below, it
        # is the market-pending widget's whole reason to exist. Anything
        # else missing the link (an old coin, a real markup change) has
        # neither the link nor the widget, so it still raises.
        nbu_issue = extract_nbu_issue_price(page_html)
        if nbu_issue is None:
            raise RuntimeError(
                f"no /coin/prices/ link in the coin page (saved at {saved_page}) -- "
                "ua-coins may have changed its markup"
            )
        prices_dir(staging_root).mkdir(parents=True, exist_ok=True)
        prices_path(staging_root, source_id).write_text("[]", encoding="utf-8")
        meta = {
            "ua_coins_id": coin_id,
            "source_id": source_id,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "page_url": page_url,
            "prices_url_used": None,
            "points": 0,
            "date_min": None,
            "date_max": None,
            "price_min": None,
            "price_max": None,
            "series_error": None,
            "current_price": None,
            "current_price_check": "no chart yet",
            "current_price_candidates": [],
            "nbu_issue_price": {
                "price": str(nbu_issue.price),
                "date": nbu_issue.date.isoformat() if nbu_issue.date else None,
                "text": nbu_issue.text,
            },
        }
        meta_path(staging_root, source_id).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result.status = "fetched"
        result.points = 0
        result.note = (
            f"no chart yet -- NBU issue price {nbu_issue.price} грн "
            f"({nbu_issue.date.isoformat() if nbu_issue.date else '?'}) recorded instead"
        )
        print(f"[fetch-prices]     {result.note}")
        return
    if endpoint.coin_id != coin_id:
        result.warnings.append(
            f"chart link on the page is for coin {endpoint.coin_id}, not {coin_id} -- using it anyway"
        )
    if not endpoint.signed:
        result.warnings.append(
            "chart link carries no _hash/expires query -- the endpoint will probably reject it"
        )
    print(f"[fetch-prices]     chart: {endpoint.path}")

    # The signature is bound to the session that rendered the page, so
    # this must go out on the same client (cookies) and say where it came
    # from -- signed endpoints are routinely picky about the Referer.
    chart_resp = ua_coins.get_with_retry(
        client,
        endpoint.path,
        pacer,
        log_label=f"{source_id} prices",
        headers={"Referer": page_url, "X-Requested-With": "XMLHttpRequest"},
    )
    body = chart_resp.text

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        error_path = pages_dir(staging_root) / f"{file_stem(source_id)}.prices-error.txt"
        error_path.write_text(body[:20000], encoding="utf-8")
        raise RuntimeError(
            f"chart endpoint did not return JSON ({exc}); first bytes saved to {error_path}"
        )
    if not isinstance(payload, list):
        error_path = pages_dir(staging_root) / f"{file_stem(source_id)}.prices-error.txt"
        error_path.write_text(body[:20000], encoding="utf-8")
        raise RuntimeError(
            f"chart endpoint returned a {type(payload).__name__}, expected an array; "
            f"body saved to {error_path}"
        )

    # Written byte-for-byte as the server sent it: this file is the
    # evidence, and load-prices re-validates it from scratch.
    prices_dir(staging_root).mkdir(parents=True, exist_ok=True)
    prices_path(staging_root, source_id).write_text(body, encoding="utf-8")

    try:
        points = parse_price_series(payload)
        stats = _stats(points)
        series_note = None
    except PriceSeriesAnomaly as exc:
        # Saved anyway -- the response is what it is. load-prices is the
        # gate; this is an early warning so it is visible at fetch time.
        points = []
        stats = {"points": len(payload), "date_min": None, "date_max": None,
                 "price_min": None, "price_max": None}
        series_note = str(exc)
        result.warnings.append(f"series will NOT load: {exc}")

    current, candidates = extract_current_price(page_html)
    verdict, warning = check_control_point(points, current)
    if warning:
        result.warnings.append(warning)

    # Only meaningful while the chart is empty -- ua-coins stops
    # rendering the widget the moment a real market price exists, so
    # this is None on every coin that already has history. Recorded
    # unconditionally anyway: load-prices is the one that decides
    # whether to use it, not this step.
    nbu_issue = extract_nbu_issue_price(page_html)

    meta = {
        "ua_coins_id": coin_id,
        "source_id": source_id,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "page_url": page_url,
        # Kept for debugging only: it expires in about two days and must
        # never be reused from here -- fetch re-reads it from the page.
        "prices_url_used": endpoint.path,
        **stats,
        "series_error": series_note,
        "current_price": (
            {
                "price": str(current.price),
                "date": current.date.isoformat() if current.date else None,
                "found_by": current.found_by,
                "text": current.text,
            }
            if current
            else None
        ),
        "current_price_check": verdict,
        "current_price_candidates": [
            {
                "price": str(c.price),
                "date": c.date.isoformat() if c.date else None,
                "text": c.text,
            }
            for c in candidates[:10]
        ],
        "nbu_issue_price": (
            {
                "price": str(nbu_issue.price),
                "date": nbu_issue.date.isoformat() if nbu_issue.date else None,
                "text": nbu_issue.text,
            }
            if nbu_issue
            else None
        ),
    }
    meta_path(staging_root, source_id).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    result.status = "fetched"
    result.points = stats["points"]
    result.date_min = stats["date_min"]
    result.date_max = stats["date_max"]
    result.note = series_note
    print(
        f"[fetch-prices]     {stats['points']} point(s) "
        f"{stats['date_min']}..{stats['date_max']}, "
        f"price {stats['price_min']}..{stats['price_max']}, control point: {verdict}"
    )


def _recheck_cached_control_point(
    staging_root: Path, source_id: str, points: list[PricePoint], result: FetchPriceResult
) -> str:
    """Re-run the control point for a cached coin off the coin page saved
    beside it, and refresh that verdict in the meta file. No network.

    It exists because the page is already on disk: when the way the page
    is read gets fixed (and it has been -- see extract_current_price), a
    plain rerun should show the corrected verdict instead of leaving the
    old one frozen in the meta until someone spends a --refresh-prices on
    re-downloading history that has not changed.
    """
    saved_page = page_path(staging_root, source_id)
    meta_file = meta_path(staging_root, source_id)
    if not saved_page.exists():
        return "no saved page"

    page_html = saved_page.read_text(encoding="utf-8")
    current, candidates = extract_current_price(page_html)
    verdict, warning = check_control_point(points, current)
    if warning:
        result.warnings.append(warning)
    # Same page, already on disk -- refreshed alongside the control
    # point rather than left stale until a --refresh-prices, the same
    # reasoning this function's docstring gives for current_price.
    nbu_issue = extract_nbu_issue_price(page_html)

    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return verdict
        meta["current_price"] = (
            {
                "price": str(current.price),
                "date": current.date.isoformat() if current.date else None,
                "found_by": current.found_by,
                "text": current.text,
            }
            if current
            else None
        )
        meta["current_price_check"] = verdict
        meta["current_price_candidates"] = [
            {
                "price": str(c.price),
                "date": c.date.isoformat() if c.date else None,
                "text": c.text,
            }
            for c in candidates[:10]
        ]
        meta["current_price_rechecked_at"] = datetime.now(timezone.utc).isoformat()
        meta["nbu_issue_price"] = (
            {
                "price": str(nbu_issue.price),
                "date": nbu_issue.date.isoformat() if nbu_issue.date else None,
                "text": nbu_issue.text,
            }
            if nbu_issue
            else None
        )
        meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return verdict


def fetch_prices(
    cards: list[dict], staging_root: Path, series: str = "", refresh: bool = False
) -> FetchPricesSummary:
    summary = FetchPricesSummary(series=series)
    pacer = Pacer(ua_coins.REQUEST_DELAY_RANGE)

    print(f"[fetch-prices] series: {series} -- {len(cards)} card(s)")
    print(f"[fetch-prices] cache: {prices_dir(staging_root)} (shared across series)")

    with httpx.Client(
        base_url=ua_coins.BASE_URL, headers={"User-Agent": USER_AGENT}, timeout=30.0
    ) as client:
        for i, card in enumerate(cards, 1):
            source_id = card["source_id"]
            title = (card.get("titles") or {}).get("uk") or "?"
            ua = card.get("ua_coins")

            if not ua:
                print(f"[fetch-prices]   ({i}/{len(cards)}) {source_id} {title!r}: no ua-coins match, skipped")
                summary.results.append(
                    FetchPriceResult(
                        source_id=source_id,
                        ua_coins_id=None,
                        status="skipped:unmatched",
                        note="card has no ua_coins match -- run --step match",
                    )
                )
                continue

            coin_id = int(ua["id"])
            page_url = ua["url"]
            result = FetchPriceResult(source_id=source_id, ua_coins_id=coin_id, status="error")
            print(f"[fetch-prices]   ({i}/{len(cards)}) {source_id} {title!r} -> ua-coins {coin_id}")

            path = prices_path(staging_root, source_id)
            # Keyed by the NBU card, not by the ua-coins row, so the cache
            # can answer the question that matters: is what is on disk the
            # history of THIS coin? A card re-matched to a different
            # ua-coins row (it happens -- see the packaging-tail fix in
            # docs/01_findings.md) leaves a file that is stale rather than
            # merely old, and under the old ua-coins-id naming there was
            # nowhere to notice: the new id simply wrote a second file
            # next to the first and both looked current.
            stale_from = None
            if path.exists() and not refresh:
                cached_id = cached_ua_coins_id(staging_root, source_id)
                if cached_id is not None and cached_id != coin_id:
                    stale_from = cached_id

            if path.exists() and not refresh and stale_from is None:
                # History is append-only and the daily updater (a separate,
                # future step) is what keeps it current -- so an existing
                # file is a finished job, not a stale one.
                points: list[PricePoint] = []
                try:
                    points = load_price_file(path)
                    stats = _stats(points)
                    result.points = stats["points"]
                    result.date_min = stats["date_min"]
                    result.date_max = stats["date_max"]
                except PriceSeriesAnomaly as exc:
                    result.note = str(exc)
                    result.warnings.append(f"cached file is unusable: {exc} (use --refresh-prices)")
                result.status = "cached"
                verdict = _recheck_cached_control_point(staging_root, source_id, points, result)
                summary.results.append(result)
                print(
                    f"[fetch-prices]     cached: {result.points} point(s) "
                    f"{result.date_min}..{result.date_max}, control point: {verdict} ({path})"
                )
                continue

            if stale_from is not None:
                message = (
                    f"cached history was fetched from ua-coins {stale_from}, "
                    f"but this card now matches {coin_id} -- refetching"
                )
                result.warnings.append(message)
                print(f"[fetch-prices]     {message}")

            try:
                _fetch_one(client, pacer, staging_root, source_id, coin_id, page_url, result)
            except Exception as exc:
                result.status = "error"
                result.note = f"{type(exc).__name__}: {exc}"
                print(f"[fetch-prices]     ERROR: {result.note}")

            summary.results.append(result)

    summary.print_report()
    return summary
