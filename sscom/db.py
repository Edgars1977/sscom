"""Datubāze. Tā pati shēma strādā gan SQLite (lokāli), gan Postgres (Railway)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from sqlalchemy import (
    Column, DateTime, Float, Integer, MetaData, String, Table, Text, create_engine,
    inspect, insert, select, text, update,
)
from sqlalchemy.engine import Engine

from . import config
from .parser import Listing

metadata = MetaData()

listings = Table(
    "listings", metadata,
    Column("ss_id", String(20), primary_key=True),
    Column("category", String(40), nullable=False, index=True),
    Column("subcategory", String(40)),
    Column("url", String(300), nullable=False, unique=True),
    Column("title", Text),
    Column("description", Text),
    Column("city", String(100)),
    Column("price", Float),
    Column("brand", String(100)),
    Column("model", String(100)),
    Column("cpu", String(100)),
    Column("ram", String(50)),
    Column("disk", String(50)),
    Column("gpu", String(150)),
    Column("screen", String(50)),
    Column("condition", String(50)),
    Column("params_json", Text),
    Column("photos_json", Text),
    Column("posted_at", DateTime),
    Column("first_seen", DateTime, nullable=False),
    Column("last_seen", DateTime, nullable=False),
    # active | disappeared | no_price (bez cenas — neanalizējam, bet atceramies)
    Column("status", String(20), nullable=False, default="active", index=True),
    Column("disappeared_at", DateTime),
    Column("missed_cycles", Integer, nullable=False, default=0),
    Column("raw_block", Text),
    Column("notified_at", DateTime),
    Column("matched_filter", String(100)),
    Column("is_dealer", Integer, nullable=False, default=0, index=True),
    Column("dealer_score", Integer, nullable=False, default=0),
    Column("dealer_reasons", Text),
    # AI analīze un darījuma vērtējums (deals.py)
    Column("model_key", String(150), index=True),
    Column("item_type", String(30)),
    Column("ai_json", Text),
    Column("ai_at", DateTime),
    Column("resale_est", Float),
    Column("margin", Float),
    Column("deal_conf", String(10)),
    Column("deal_note", Text),
    Column("deal_sent_at", DateTime),
)

price_history = Table(
    "price_history", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ss_id", String(20), nullable=False, index=True),
    Column("price", Float, nullable=False),
    Column("seen_at", DateTime, nullable=False),
)

scrape_log = Table(
    "scrape_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False),
    Column("category", String(40), nullable=False),
    Column("kind", String(10), nullable=False),  # rss | full
    Column("found", Integer, default=0),
    Column("new", Integer, default=0),
    Column("disappeared", Integer, default=0),
    Column("errors", Integer, default=0),
)


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # glabājam UTC bez tz


def get_engine(url: Optional[str] = None) -> Engine:
    url = (url or config.DATABASE_URL).strip()
    # Railway dod postgres://..., SQLAlchemy grib postgresql+psycopg://
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    engine = create_engine(url, pool_pre_ping=True)
    metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


def _add_missing_columns(engine: Engine) -> None:
    """create_all neveido jaunas kolonnas esošās tabulās — pievienojam pašas."""
    insp = inspect(engine)
    for table in metadata.sorted_tables:
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            ddl = col.type.compile(dialect=engine.dialect)
            default = ""
            if col.default is not None and not callable(col.default.arg):
                default = f" DEFAULT {col.default.arg!r}"
            with engine.begin() as c:
                c.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}{default}'))


def row_to_listing(r) -> Listing:
    from .parser import Listing as L
    return L(
        ss_id=r.ss_id, category=r.category, url=r.url, title=r.title or "", city=r.city,
        price=r.price, params=json.loads(r.params_json or "{}"), description=r.description,
        photos=json.loads(r.photos_json or "[]"), posted_at=r.posted_at,
        dealer_score=r.dealer_score or 0,
        dealer_reasons=[x for x in (r.dealer_reasons or "").split("; ") if x],
    )


class Store:
    def __init__(self, engine: Engine):
        self.engine = engine

    # ---- uzmeklēšana

    def known_urls(self, category: str) -> Set[str]:
        with self.engine.connect() as c:
            return set(c.execute(select(listings.c.url).where(listings.c.category == category)).scalars())

    def known_ids(self, category: str) -> Dict[str, Optional[float]]:
        """ss_id -> pēdējā zināmā cena."""
        with self.engine.connect() as c:
            rows = c.execute(select(listings.c.ss_id, listings.c.price).where(listings.c.category == category))
            return {r.ss_id: r.price for r in rows}

    def count(self, category: str) -> int:
        return len(self.known_ids(category))

    # ---- rakstīšana

    def insert_listing(self, l: Listing, status: str = "active") -> None:
        t = now()
        posted = l.posted_at.replace(tzinfo=None) if l.posted_at else None
        with self.engine.begin() as c:
            c.execute(insert(listings).values(
                ss_id=l.ss_id, category=l.category, subcategory=l.subcategory, url=l.url,
                title=l.title, description=l.description, city=l.city, price=l.price,
                brand=l.key("brand"), model=l.key("model"), cpu=l.key("cpu"), ram=l.key("ram"),
                disk=l.key("disk"), gpu=l.key("gpu"), screen=l.key("screen"),
                condition=l.key("condition"),
                params_json=json.dumps(l.params, ensure_ascii=False),
                photos_json=json.dumps(l.photos),
                posted_at=posted, first_seen=t, last_seen=t, status=status,
                missed_cycles=0, raw_block=l.raw_block,
                is_dealer=int(l.is_dealer), dealer_score=l.dealer_score,
                dealer_reasons="; ".join(l.dealer_reasons) or None,
            ))
            if l.price is not None:
                c.execute(insert(price_history).values(ss_id=l.ss_id, price=l.price, seen_at=t))

    def touch(self, ss_id: str, price: Optional[float], old_price: Optional[float],
              city: Optional[str] = None) -> bool:
        """Atjaunina last_seen; ja cena mainījusies, pieraksta vēsturē. Atgriež True, ja mainījās."""
        t = now()
        changed = price is not None and price != old_price
        with self.engine.begin() as c:
            values = dict(last_seen=t, missed_cycles=0, status="active", disappeared_at=None)
            if changed:
                values["price"] = price
                c.execute(insert(price_history).values(ss_id=ss_id, price=price, seen_at=t))
            c.execute(update(listings).where(listings.c.ss_id == ss_id)
                      .where(listings.c.status != "no_price").values(**values))
            if city:
                c.execute(update(listings).where(listings.c.ss_id == ss_id)
                          .where(listings.c.city.is_(None)).values(city=city))
        return changed

    def all_descriptions(self) -> List[Optional[str]]:
        with self.engine.connect() as c:
            return list(c.execute(select(listings.c.description)).scalars())

    def set_dealer(self, ss_id: str, score: int, reasons: List[str], dealer: bool) -> None:
        with self.engine.begin() as c:
            c.execute(update(listings).where(listings.c.ss_id == ss_id).values(
                is_dealer=int(dealer), dealer_score=score, dealer_reasons="; ".join(reasons) or None))

    def mark_notified(self, ss_id: str, filter_name: str) -> None:
        with self.engine.begin() as c:
            c.execute(update(listings).where(listings.c.ss_id == ss_id)
                      .values(notified_at=now(), matched_filter=filter_name))

    def log(self, category: str, kind: str, found: int = 0, new: int = 0,
            disappeared: int = 0, errors: int = 0) -> None:
        with self.engine.begin() as c:
            c.execute(insert(scrape_log).values(
                ts=now(), category=category, kind=kind, found=found, new=new,
                disappeared=disappeared, errors=errors,
            ))
