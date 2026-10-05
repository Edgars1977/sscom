"""Jaunas preces cena Latvijā no cenueksperts.lv (meklēšanas lapa, bez /click/ saitēm).
Katru vaicājumu atceramies CACHE_DAYS dienas, lai nepieprasītu vienu un to pašu atkārtoti."""
from __future__ import annotations

import json
import logging
import re
import statistics
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Optional, Tuple
from urllib.parse import quote

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from sqlalchemy import Column, DateTime, Float, Integer, String, Table, Text, select

from . import db
from .http import Client

log = logging.getLogger(__name__)

BASE = "https://www.cenueksperts.lv"
CACHE_DAYS = 7

new_prices = Table(
    "new_prices", db.metadata,
    Column("query", String(200), primary_key=True),
    Column("fetched_at", db.DateTime, nullable=False),
    Column("min_price", Float),
    Column("median_price", Float),
    Column("n", Integer, nullable=False, default=0),
    Column("names_json", Text),
)

# Piederumi, kas nav pati prece (ja neprasa tieši tos)
ACCESSORY = re.compile(
    r"akumulator|baterij|battery|lādētāj|charger|adapter|soma|bag|case for|vāciņ|"
    r"klaviatūr|keyboard|ekrāns priekš|screen for|matrica|dzesētājs priekš|thermal pad|"
    r"kabel|cable|stiprinā|bracket|skin|uzlīm|sticker|\blcd\b|displej|display|panel|"
    r"bateria|\d+\s*mah\b|cameron sino|green cell|touchpad|eņģ|hinge|replacement|rezerves",
    re.I,
)
# Modeļa paveidi: "RTX 3060" ≠ "RTX 3060 Ti", "RX 7900" ≠ "RX 7900 XTX"
VARIANTS = {"ti", "super", "xt", "xtx", "max", "plus", "ultra"}


@dataclass
class NewPrice:
    query: str
    min_price: Optional[float]
    median_price: Optional[float]
    n: int
    names: List[str]


def _tokens(q: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]+", q.lower()) if len(t) >= 2 or t.isdigit()]


def _price(text: str) -> Optional[float]:
    m = re.search(r"(\d[\d\s]*)[,.](\d{2})\s*€", text) or re.search(r"(\d[\d\s]*)\s*€", text)
    if not m:
        return None
    whole = re.sub(r"\s", "", m.group(1))
    frac = m.group(2) if m.lastindex and m.lastindex >= 2 else "0"
    return float(f"{whole}.{frac}")


def parse_search(html: str, query: str) -> List[Tuple[str, float]]:
    """(nosaukums, lētākā cena) produktiem, kuru nosaukumā ir visi vaicājuma vārdi."""
    tree = HTMLParser(html)
    want = _tokens(query)
    accessory_query = bool(ACCESSORY.search(query))
    out = []
    for name_node in tree.css(".productName"):
        name = name_node.attributes.get("title") or name_node.text(strip=True)
        box = name_node
        for _ in range(8):
            box = box.parent
            if box is None or box.css_first(".lowestPrice"):
                break
        if box is None or box.css_first(".lowestPrice") is None:
            continue
        price = _price(box.css_first(".lowestPrice").text())
        name_tokens = set(_tokens(name))
        if price is None or not all(t in name_tokens for t in want):
            continue
        if not accessory_query and ACCESSORY.search(name):
            continue
        if (name_tokens & VARIANTS) - set(want):
            continue
        out.append((name, price))
    return out


class PriceLookup:
    def __init__(self, store: db.Store, client: Optional[Client] = None, ai=None):
        self.store = store
        self.client = client or Client(base_url=BASE)
        self.ai = ai  # ja ir: GPT atlasa tikai pašu preci (bez rezerves daļām)

    def get(self, query: str) -> Optional[NewPrice]:
        query = re.sub(r"\s+", " ", query).strip()
        if not query:
            return None
        key = query.lower()
        with self.store.engine.connect() as c:
            row = c.execute(select(new_prices).where(new_prices.c.query == key)).first()
        if row and row.fetched_at > db.now() - timedelta(days=CACHE_DAYS):
            return NewPrice(query, row.min_price, row.median_price, row.n, json.loads(row.names_json or "[]"))

        html = self.client.get(f"{BASE}/mekleet/{quote(query)}.html")
        if html is None:
            return None
        found = sorted(parse_search(html, query), key=lambda x: x[1])[:12]
        if found and self.ai is not None and self.ai.enabled:
            keep = self.ai.match_products(query, [n for n, _ in found])
            if keep is not None:
                found = [found[i] for i in sorted(set(keep))]
        prices = sorted(p for _, p in found)
        # mediāna no lētākajiem 5 — dārgie "multipack"/eksotiski veikali izkropļo
        result = NewPrice(
            query=query,
            min_price=prices[0] if prices else None,
            median_price=statistics.median(prices[:5]) if prices else None,
            n=len(prices),
            names=[n for n, _ in sorted(found, key=lambda x: x[1])[:5]],
        )
        values = dict(fetched_at=db.now(), min_price=result.min_price, median_price=result.median_price,
                      n=result.n, names_json=json.dumps(result.names, ensure_ascii=False))
        with self.store.engine.begin() as c:
            if row:
                c.execute(new_prices.update().where(new_prices.c.query == key).values(**values))
            else:
                c.execute(new_prices.insert().values(query=key, **values))
        log.info("cenueksperts '%s': %d atbilst, min %s €", query, result.n, result.min_price)
        return result
