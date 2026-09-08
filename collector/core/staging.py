"""Layout and IO helpers for the local staging cache.

staging/<country>/<series-slug>/{raw,parsed}/
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

DEFAULT_STAGING_ROOT = Path("staging")

# NFKD + ascii-encode drops Cyrillic entirely (it has no ascii-compatible
# decomposition), so series slugs need an explicit transliteration table.
_CYRILLIC_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e",
    "є": "ie", "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "shch", "ь": "", "ю": "iu", "я": "ia", "'": "",
}


def slugify(text: str) -> str:
    """Turn a series name (Ukrainian or English) into a filesystem-safe slug."""
    transliterated = "".join(_CYRILLIC_TRANSLIT.get(ch, ch) for ch in text.lower())
    normalized = unicodedata.normalize("NFKD", transliterated)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return slug or "series"


class SeriesStaging:
    """Paths for one country/series staging directory."""

    def __init__(self, country: str, series: str, root: Path = DEFAULT_STAGING_ROOT):
        self.country = country
        self.series = series
        self.slug = slugify(series)
        self.dir = root / country / self.slug
        self.raw_dir = self.dir / "raw"
        self.parsed_dir = self.dir / "parsed"

    def ensure_dirs(self) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.parsed_dir.mkdir(parents=True, exist_ok=True)

    def raw_path(self, name: str) -> Path:
        return self.raw_dir / name

    def write_raw(self, name: str, content: str) -> Path:
        self.ensure_dirs()
        path = self.raw_path(name)
        path.write_text(content, encoding="utf-8")
        return path

    def read_raw(self, name: str) -> str:
        return self.raw_path(name).read_text(encoding="utf-8")

    def raw_files(self, pattern: str) -> list[Path]:
        return sorted(self.raw_dir.glob(pattern))

    @property
    def cards_json_path(self) -> Path:
        return self.parsed_dir / "cards.json"

    def write_parsed(self, data: dict) -> Path:
        self.ensure_dirs()
        path = self.cards_json_path
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False),
            encoding="utf-8",
        )
        return path

    def read_parsed(self) -> dict:
        return json.loads(self.cards_json_path.read_text(encoding="utf-8"))

    @property
    def anomalies_json_path(self) -> Path:
        return self.parsed_dir / "anomalies.json"

    def write_anomalies(self, anomalies: list[dict]) -> Path:
        self.ensure_dirs()
        path = self.anomalies_json_path
        path.write_text(
            json.dumps(anomalies, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    @property
    def unmatched_json_path(self) -> Path:
        return self.parsed_dir / "unmatched.json"

    def write_unmatched(self, unmatched: list[dict]) -> Path:
        self.ensure_dirs()
        path = self.unmatched_json_path
        path.write_text(
            json.dumps(unmatched, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path
