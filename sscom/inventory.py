"""Mani pirkumi: komplekti (sadalīti detaļās) un atsevišķas detaļas, ar peļņas uzskaiti.

Komplekta cena sadalās pa detaļām proporcionāli to vērtībai (est_value),
lai katrai detaļai būtu sava "pašizmaksa" un peļņu varētu redzēt atsevišķi.
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Integer, String, Table, Text, select

from . import db, guide

STATUSES = ("stock", "listed", "sold")
STATUS_LV = {"stock": "noliktavā", "listed": "izlikts", "sold": "pārdots"}

purchases = Table(
    "purchases", db.metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime, nullable=False),
    Column("bought_at", Date, nullable=False),
    Column("kind", String(10), nullable=False),          # kit | single
    Column("title", String(200), nullable=False),
    Column("total_price", Float, nullable=False),
    Column("source_url", String(300)),
    Column("ss_id", String(20)),
    Column("note", Text),
)

purchase_items = Table(
    "purchase_items", db.metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("purchase_id", Integer, ForeignKey("purchases.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("name", String(200), nullable=False),
    Column("item_type", String(30)),
    Column("guide_key", String(120)),
    Column("est_value", Float),                          # cik varētu dabūt (sadales pamatam)
    Column("cost", Float, nullable=False, default=0),    # pašizmaksa (daļa no pirkuma cenas)
    Column("status", String(10), nullable=False, default="stock"),
    Column("listed_price", Float),
    Column("sold_price", Float),
    Column("sold_at", Date),
    Column("note", Text),
)


def allocate(total: float, values: List[Optional[float]]) -> List[float]:
    """Sadala `total` proporcionāli `values`. Ja vērtības nav zināmas — vienādi."""
    vals = [v if v and v > 0 else 0.0 for v in values]
    s = sum(vals)
    if s <= 0:
        vals, s = [1.0] * len(values), float(len(values))
    shares = [round(total * v / s, 2) for v in vals]
    if shares:
        shares[-1] = round(total - sum(shares[:-1]), 2)  # centu noapaļojums pēdējam
    return shares


def prefill_from_listing(store: db.Store, url: str) -> Optional[Dict[str, Any]]:
    """No ss.com saites (ja bots to sludinājumu zina) sagatavo pirkuma melnrakstu."""
    m = re.search(r"/msg/[^ ]+?\.html", url or "")
    if not m:
        return None
    L = db.listings
    with store.engine.connect() as c:
        r = c.execute(select(L).where(L.c.url.like(f"%{m.group(0)}"))).first()
    if r is None:
        return None
    facts = json.loads(r.ai_json or "{}")
    parts = json.loads(r.partout_json or "[]")
    items = []
    if parts:
        for p in parts:
            items.append({"name": p["key"], "item_type": p["type"], "est_value": p.get("price"),
                          "guide_key": guide.guide_key({"item_type": p["type"], "model_key": p["key"]})})
    elif facts.get("components"):
        for p in facts["components"]:
            items.append({"name": p["key"], "item_type": p["type"], "est_value": None,
                          "guide_key": guide.guide_key({"item_type": p["type"], "model_key": p["key"]})})
    else:
        items.append({"name": facts.get("model_key") or r.title, "item_type": facts.get("item_type"),
                      "est_value": r.resale_est, "guide_key": r.guide_key})
    # trūkstošās vērtības — no cenu ceļveža mediānas
    keys = [i["guide_key"] for i in items if i["guide_key"] and not i["est_value"]]
    if keys:
        with store.engine.connect() as c:
            med = dict(c.execute(select(guide.price_guide.c.guide_key, guide.price_guide.c.price_median)
                                 .where(guide.price_guide.c.guide_key.in_(keys))).all())
        for i in items:
            if not i["est_value"] and i["guide_key"] in med:
                i["est_value"] = med[i["guide_key"]]
    return {"title": (r.title or "")[:200], "total_price": r.price, "source_url": r.url, "ss_id": r.ss_id,
            "kind": "kit" if len(items) > 1 else "single", "items": items}


def add_purchase(store: db.Store, title: str, total_price: float, items: List[Dict[str, Any]],
                 bought_at: Optional[date] = None, source_url: Optional[str] = None,
                 ss_id: Optional[str] = None, note: Optional[str] = None) -> int:
    if not items:
        items = [{"name": title}]
    costs = allocate(total_price, [i.get("est_value") for i in items])
    with store.engine.begin() as c:
        pid = c.execute(purchases.insert().values(
            created_at=db.now(), bought_at=bought_at or date.today(),
            kind="kit" if len(items) > 1 else "single", title=title[:200], total_price=total_price,
            source_url=source_url, ss_id=ss_id, note=note)).inserted_primary_key[0]
        c.execute(purchase_items.insert(), [dict(
            purchase_id=pid, name=(i.get("name") or "?")[:200], item_type=i.get("item_type"),
            guide_key=i.get("guide_key"), est_value=i.get("est_value"), cost=cost, status="stock")
            for i, cost in zip(items, costs)])
    return pid


def update_item(store: db.Store, item_id: int, **fields) -> None:
    allowed = {"name", "status", "listed_price", "sold_price", "sold_at", "note", "est_value", "cost"}
    values = {k: v for k, v in fields.items() if k in allowed}
    if values.get("status") == "sold" and not values.get("sold_at"):
        values["sold_at"] = date.today()
    if values:
        with store.engine.begin() as c:
            c.execute(purchase_items.update().where(purchase_items.c.id == item_id).values(**values))


def reallocate(store: db.Store, purchase_id: int) -> None:
    """Pēc est_value labošanas — pārdala pirkuma cenu pa detaļām no jauna."""
    with store.engine.begin() as c:
        total = c.execute(select(purchases.c.total_price).where(purchases.c.id == purchase_id)).scalar()
        rows = c.execute(select(purchase_items.c.id, purchase_items.c.est_value)
                         .where(purchase_items.c.purchase_id == purchase_id)
                         .order_by(purchase_items.c.id)).all()
        for (iid, _), cost in zip(rows, allocate(total or 0, [r.est_value for r in rows])):
            c.execute(purchase_items.update().where(purchase_items.c.id == iid).values(cost=cost))


def delete_purchase(store: db.Store, purchase_id: int) -> None:
    with store.engine.begin() as c:
        c.execute(purchase_items.delete().where(purchase_items.c.purchase_id == purchase_id))
        c.execute(purchases.delete().where(purchases.c.id == purchase_id))


def overview(store: db.Store) -> Dict[str, Any]:
    with store.engine.connect() as c:
        ps = c.execute(select(purchases).order_by(purchases.c.bought_at.desc(), purchases.c.id.desc())).all()
        its = c.execute(select(purchase_items).order_by(purchase_items.c.id)).all()
    by_p: Dict[int, List] = {}
    for i in its:
        by_p.setdefault(i.purchase_id, []).append(dict(i._mapping))
    out, invested, in_stock, sold_rev, sold_cost = [], 0.0, 0.0, 0.0, 0.0
    for p in ps:
        items = by_p.get(p.id, [])
        for i in items:
            i["profit"] = (i["sold_price"] - i["cost"]) if i["status"] == "sold" and i["sold_price"] is not None else None
            if i["status"] == "sold" and i["sold_price"] is not None:
                sold_rev += i["sold_price"]
                sold_cost += i["cost"]
            else:
                in_stock += i["cost"]
        sold = [i for i in items if i["profit"] is not None]
        d = dict(p._mapping)
        d.update(items=items, profit=sum(i["profit"] for i in sold),
                 sold_count=len(sold), item_count=len(items),
                 revenue=sum(i["sold_price"] for i in sold))
        invested += p.total_price
        out.append(d)
    return {"purchases": out, "invested": invested, "in_stock": in_stock,
            "sold_revenue": sold_rev, "sold_profit": sold_rev - sold_cost}
