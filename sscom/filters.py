"""Filtri: kurus jaunos sludinājumus sūtīt uz Telegram. Konfigurācija ir filters.json."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .parser import Listing

log = logging.getLogger(__name__)

FILTERS_FILE = Path(__file__).resolve().parent.parent / "filters.json"


@dataclass
class Filter:
    name: str
    categories: List[str] = field(default_factory=list)      # tukšs = visas
    subcategories: List[str] = field(default_factory=list)   # completing-pc: video, cpu, ram, motherboards, ssd...
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    ram_min: Optional[int] = None
    keywords_any: List[str] = field(default_factory=list)    # vismaz viens jāatrod tekstā
    keywords_none: List[str] = field(default_factory=list)   # neviens nedrīkst būt tekstā
    include_dealers: bool = False                            # firmu sludinājumus pēc noklusējuma izlaižam
    enabled: bool = True

    def matches(self, l: Listing) -> bool:
        if not self.enabled or l.price is None:
            return False
        if l.is_dealer and not self.include_dealers:
            return False
        if self.categories and l.category not in self.categories:
            return False
        if self.subcategories and l.subcategory not in self.subcategories:
            return False
        if self.price_min is not None and l.price < self.price_min:
            return False
        if self.price_max is not None and l.price > self.price_max:
            return False
        text = _haystack(l)
        if self.keywords_any and not any(_has(text, k) for k in self.keywords_any):
            return False
        if any(_has(text, k) for k in self.keywords_none):
            return False
        if self.ram_min is not None:
            ram = ram_gb(l)
            if ram is None or ram < self.ram_min:
                return False
        return True


def _haystack(l: Listing) -> str:
    parts = [l.title, l.description or ""] + [f"{k} {v}" for k, v in l.params.items()]
    return " ".join(parts).lower()


def _has(text: str, keyword: str) -> bool:
    # vārda sākumā, lai "i5" neatrastu "mi50", bet "thinkpad" atrastu "thinkpad t480"
    return re.search(r"(?<![a-z0-9])" + re.escape(keyword.lower()), text) is not None


def ram_gb(l: Listing) -> Optional[int]:
    value = l.key("ram")
    if value:
        m = re.search(r"\d+", value)
        if m:
            return int(m.group())
    m = re.search(r"(\d{1,3})\s*gb\s*(?:ddr\d\s*)?ram|ram[:\s]*(?:ddr\d\s*)?(\d{1,3})\s*gb", _haystack(l))
    if m:
        return int(m.group(1) or m.group(2))
    return None


def load(path: Path = FILTERS_FILE) -> List[Filter]:
    if not path.exists():
        log.warning("Nav %s — paziņojumi netiks sūtīti", path.name)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.error("Nevar nolasīt %s: %s", path.name, e)
        return []
    out = []
    known = set(Filter.__dataclass_fields__)
    for raw in data.get("filters", []):
        unknown = set(raw) - known
        if unknown:
            log.warning("Filtrā '%s' nezināmi lauki: %s", raw.get("name"), ", ".join(sorted(unknown)))
        out.append(Filter(**{k: v for k, v in raw.items() if k in known}))
    return out


def first_match(filters: List[Filter], l: Listing) -> Optional[Filter]:
    return next((f for f in filters if f.matches(l)), None)


# ---------------------------------------------------------------- "vienmēr rādīt"

@dataclass
class Watch:
    """Ko rādīt VIENMĒR (neatkarīgi no budžeta, vietas, peļņas), ar novērtējumu.
    filters.json -> "always": [{"name": ..., "text_any": [...], "model_regex": ..., "ram_min": 32}]"""
    name: str
    text_any: List[str] = field(default_factory=list)   # lēta pirmspārbaude tekstā (pirms AI)
    model_regex: Optional[str] = None                  # AI modelim / virsrakstam
    ram_min: Optional[int] = None
    categories: List[str] = field(default_factory=list)
    include_dealers: bool = False
    enabled: bool = True

    def text_matches(self, l: Listing) -> bool:
        if not self.enabled or (l.is_dealer and not self.include_dealers):
            return False
        if self.categories and l.category not in self.categories:
            return False
        text = _haystack(l)
        return not self.text_any or any(_has(text, k) for k in self.text_any)

    def matches(self, l: Listing, facts: dict) -> bool:
        if not self.text_matches(l):
            return False
        if self.model_regex and not re.search(self.model_regex, f"{facts.get('model_key') or ''} {l.title}", re.I):
            return False
        if self.ram_min is not None:
            ram = facts.get("ram_gb") or ram_gb(l)
            if not ram or ram < self.ram_min:
                return False
        return True


def load_watches(path: Path = FILTERS_FILE) -> List[Watch]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    known = set(Watch.__dataclass_fields__)
    return [Watch(**{k: v for k, v in w.items() if k in known}) for w in data.get("always", [])]
