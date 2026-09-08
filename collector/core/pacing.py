"""Generic, site-agnostic request pacer: sleeps a random delay between
calls, except before the very first one. Shared by every module that
scrapes a site politely (NBU's bank.gov.ua, ua-coins.info, ...) so the
pacing policy lives in one place instead of being reimplemented per site.
"""

from __future__ import annotations

import random
import time


class Pacer:
    def __init__(self, delay_range: tuple[float, float]):
        self._delay_range = delay_range
        self._first = True

    def wait(self) -> None:
        if not self._first:
            time.sleep(random.uniform(*self._delay_range))
        self._first = False
