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
    Column("market_value", Float),                       # tirgus cena tagad (ceļvedis vai AI)
    Column("market_src", String(40)),                    # "ss.com 6×" / "AI"
    Column("market_at", DateTime),
)

# Detaļas tips no nosaukuma (ja ievadīts ar roku)
_TYPE_RES = [
    ("gpu", r"\b(rtx|gtx|gt\s?\d|rx\s?\d|radeon|quadro|geforce|arc\s?a\d)"),
    ("cpu", r"\b(i[3579][\s-]?\d{3,5}|ryzen|xeon|pentium|celeron|athlon|core\s?ultra|fx[\s-]?\d{4})"),
    ("ram", r"\bddr\s?[2-5]|\bram\b|operat[iī]v"),
    ("storage", r"\b(ssd|hdd|nvme|m\.2|\d+\s?tb\b|\d{3,4}\s?gb\s?(ssd|hdd))"),
    ("psu", r"\b(psu|barošan|блок пит|\d{3,4}\s?w\b)"),
    ("motherboard", r"\b(mātesplat|matesplat|motherboard|plate|[abhzxq]\d{2,3}[m]?\b)"),
    ("cooler", r"\b(dzes|cooler|aio|ventil|кулер)"),
    ("case", r"\b(korpus|case|корпус)"),
]


def infer_type(name: str) -> Optional[str]:
    low = (name or "").lower()
    for t, rx in _TYPE_RES:
        if re.search(rx, low):
            return t
    return None


def item_key(name: str, item_type: Optional[str]) -> Optional[str]:
    t = item_type or infer_type(name)
    return guide.guide_key({"item_type": t, "model_key": (name or "").lower()}, name or "") if t else None


def market_lookup(store: db.Store, key: Optional[str]):
    """Ceļveža rinda šai grupai: precīzi vai ar to sākas (GTX 750 TI -> GTX 750 TI 2GB). Lielākā n uzvar."""
    if not key:
        return None
    G = guide.price_guide
    with store.engine.connect() as c:
        rows = c.execute(select(G).where((G.c.guide_key == key) | G.c.guide_key.like(key.replace("%", "") + " %"))).all()
    return max(rows, key=lambda r: (r.guide_key == key, r.n)) if rows else None


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
    if values.get("name"):
        values["item_type"] = infer_type(values["name"])
        values["guide_key"] = item_key(values["name"], values["item_type"])
        values.update(market_value=None, market_src=None)
    if values.get("status") == "sold" and not values.get("sold_at"):
        values["sold_at"] = date.today()
    if values:
        with store.engine.begin() as c:
            c.execute(purchase_items.update().where(purchase_items.c.id == item_id).values(**values))
            pid = c.execute(select(purchase_items.c.purchase_id).where(purchase_items.c.id == item_id)).scalar()
        if pid and ("est_value" in values or "name" in values):
            reallocate(store, pid)


def reallocate(store: db.Store, purchase_id: int) -> None:
    """Pēc est_value labošanas — pārdala pirkuma cenu pa detaļām no jauna."""
    with store.engine.begin() as c:
        total = c.execute(select(purchases.c.total_price).where(purchases.c.id == purchase_id)).scalar()
        rows = c.execute(select(purchase_items.c.id, purchase_items.c.est_value, purchase_items.c.market_value)
                         .where(purchase_items.c.purchase_id == purchase_id)
                         .order_by(purchase_items.c.id)).all()
        values = [r.est_value if r.est_value else r.market_value for r in rows]
        for (iid, _, _), cost in zip(rows, allocate(total or 0, values)):
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


def update_purchase(store: db.Store, purchase_id: int, **fields) -> None:
    allowed = {"title", "total_price", "bought_at", "note", "source_url"}
    values = {k: v for k, v in fields.items() if k in allowed}
    if values:
        with store.engine.begin() as c:
            c.execute(purchases.update().where(purchases.c.id == purchase_id).values(**values))
            n = c.execute(select(purchase_items.c.id).where(purchase_items.c.purchase_id == purchase_id)).all()
            c.execute(purchases.update().where(purchases.c.id == purchase_id)
                      .values(kind="kit" if len(n) > 1 else "single"))
    reallocate(store, purchase_id)


def add_item(store: db.Store, purchase_id: int, name: str, est_value: Optional[float] = None) -> int:
    t = infer_type(name)
    with store.engine.begin() as c:
        iid = c.execute(purchase_items.insert().values(
            purchase_id=purchase_id, name=name[:200], item_type=t, guide_key=item_key(name, t),
            est_value=est_value, cost=0, status="stock")).inserted_primary_key[0]
    update_purchase(store, purchase_id)
    return iid


def delete_item(store: db.Store, item_id: int) -> None:
    with store.engine.begin() as c:
        pid = c.execute(select(purchase_items.c.purchase_id).where(purchase_items.c.id == item_id)).scalar()
        c.execute(purchase_items.delete().where(purchase_items.c.id == item_id))
    if pid:
        update_purchase(store, pid)


def appraise(store: db.Store, purchase_id: int, ai=None, only_missing: bool = False) -> int:
    """Tirgus cena katrai nepārdotai detaļai: ceļvedis (≥2 sludinājumi) vai GPT. Pēc tam pārdala.
    only_missing: vērtē tikai detaļas bez tirgus cenas (piem. tikko pievienotu)."""
    with store.engine.connect() as c:
        items = c.execute(select(purchase_items).where(purchase_items.c.purchase_id == purchase_id)
                          .order_by(purchase_items.c.id)).all()
    updates, ask = {}, []
    for i in items:
        if i.status == "sold" or (only_missing and i.market_value is not None):
            continue
        key = item_key(i.name, i.item_type) or i.guide_key  # no nosaukuma — grupēšana var būt uzlabota
        g = market_lookup(store, key)
        if g is not None and g.n >= 2:
            updates[i.id] = dict(market_value=round(g.price_median), market_src=f"ss.com {g.n}× ({g.guide_key})",
                                 guide_key=key)
        else:
            ask.append((i, key, g))
    if ask and ai is not None and ai.enabled:
        comps = [{"type": i.item_type or infer_type(i.name) or "other", "key": i.name,
                  "ss_com_prices": [g.price_median] if g is not None else []} for i, _, g in ask]
        with store.engine.connect() as c:
            total = c.execute(select(purchases.c.total_price).where(purchases.c.id == purchase_id)).scalar()
        est = ai.estimate_parts(comps, total or 0)
        if est:
            by_name = {p["key"]: float(p["used_price_eur"]) for p in est["parts"]}
            for i, key, _ in ask:
                if i.name in by_name:
                    updates[i.id] = dict(market_value=round(by_name[i.name]), market_src="AI", guide_key=key)
    now = db.now()
    with store.engine.begin() as c:
        for iid, v in updates.items():
            c.execute(purchase_items.update().where(purchase_items.c.id == iid).values(market_at=now, **v))
    reallocate(store, purchase_id)
    return len(updates)
