"""HTTP constants and the one low-level scraping primitive shared by every
NBU-talking module in this adapter: the per-series fetch/parse pipeline
(parser.py) and the series-dictionary builder (series.py). Keeping this in
one place means both talk to bank.gov.ua the same way, and neither has to
import the other for it (parser.py -> series.py is one-directional; this
module has no dependency on either).
"""

from __future__ import annotations

import re

BASE_URL = "https://bank.gov.ua"
SEARCH_PATH = "/{locale_path}/component/source/searchSouvenierCoinResult"
LISTING_PATH = "/{locale_path}/uah/numismatic-products/souvenier-coins"
LOCALE_URL_SEGMENT = {"uk": "ua", "en": "en"}
USER_AGENT = "coin-collector/0.1 (personal project)"
PER_PAGE = 100
REQUEST_DELAY_RANGE = (1.0, 2.0)

_CARD_ID_RE = re.compile(r"/media/coins/(\d+)/")


def extract_card_id(node) -> str | None:
    """Card id from a div.search-result node: parsed from the obverse
    image URL (/media/coins/{id}/...), falling back to a data-fancybox
    gallery-id attribute if present. None if neither yields an id."""
    imgs = node.css("div.img-container img")
    if imgs:
        src = imgs[0].attributes.get("src") or ""
        m = _CARD_ID_RE.search(src)
        if m:
            return m.group(1)
    fancy = node.css_first("[data-fancybox]")
    if fancy:
        m = _CARD_ID_RE.search(fancy.attributes.get("data-fancybox", ""))
        if m:
            return m.group(1)
    return None
