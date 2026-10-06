"""Automātiska darījumu vērtēšana: vai šo var nopirkt un pārdot tālāk ar peļņu.

1. analīze (GPT-nano): precīzs modelis, stāvoklis, steidzamība, vai firma, vai atved uz Rīgu
2. tirgus cena: mūsu ss.com privāto sludinājumu mediāna tam pašam modelim
                + jaunas preces cena no cenueksperts.lv
3. ja salīdzinājumu par maz — GPT-mini novērtē tālākpārdošanas cenu (zema/vidēja pārliecība)
4. peļņa >= DEAL_MIN_MARGIN un >= DEAL_MIN_PROFIT -> Telegram
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import statistics
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update

from . import db, telegram
from .ai import AI
from .cenueksperts import PriceLookup
from .parser import Listing

log = logging.getLogger(__name__)

MIN_MARGIN = float(os.getenv("DEAL_MIN_MARGIN", "0.20"))
MIN_PROFIT = float(os.getenv("DEAL_MIN_PROFIT", "15"))
MAX_PRICE = float(os.getenv("DEAL_MAX_PRICE", "500"))
# Edgara noteikums: mazlietotu vērts pirkt tikai par ~pusi no jaunas cenas (ja jaunā cena zināma)
MAX_NEW_RATIO = float(os.getenv("DEAL_MAX_NEW_RATIO", "0.5"))
ANALYSE_MAX_PRICE = float(os.getenv("DEAL_ANALYSE_MAX_PRICE", "750"))  # salīdzinājumiem analizējam plašāk
LOCATIONS = [x.strip().lower() for x in os.getenv("DEAL_LOCATIONS", "rīga").split(",") if x.strip()]
# Izjaukšana prasa vairāk darba (6–8 sludinājumi, nedēļas) — tāpēc augstāks slieksnis
PARTOUT_MIN_MARGIN = float(os.getenv("DEAL_PARTOUT_MIN_MARGIN", "0.30"))
PARTOUT_MIN_PROFIT = float(os.getenv("DEAL_PARTOUT_MIN_PROFIT", "60"))
USED_FLOOR_OF_NEW = 0.55  # lietota RAM/SSD vismaz tik no jaunas cenas
COMPS_MIN = 3
COMPS_DAYS = 120
# ja lietota prece maksā >= 85% no jaunas lētākās cenas, peļņas praktiski nevar būt — GPT nesaucam
NEW_PRICE_CEILING = 0.85

CONDITION_LV = {"new": "jauns", "like_new": "kā jauns", "used": "lietots",
                "broken": "bojāts", "for_parts": "uz detaļām"}
CONF_LV = {"low": "zema", "medium": "vidēja", "high": "augsta"}


def normalize_key(key: str) -> str:
    """'NVIDIA GeForce RTX 3070 8GB' un 'nvidia rtx 3070 8gb' -> viens un tas pats."""
    k = (key or "").lower()
    k = re.sub(r"\b(geforce|graphics card|videokarte|processor|procesors)\b", " ", k)
    return re.sub(r"\s+", " ", k).strip()


@dataclass
class PartOut:
    price: float
    total: float
    parts: List[Dict[str, Any]]  # {"type","key","price","comps"}
    verdict: Optional[str] = None

    @property
    def profit(self) -> float:
        return self.total - self.price

    @property
    def margin(self) -> float:
        return self.profit / self.price if self.price else 0

    @property
    def is_good(self) -> bool:
        return self.margin >= PARTOUT_MIN_MARGIN and self.profit >= PARTOUT_MIN_PROFIT


@dataclass
class Deal:
    price: float
    resale: float
    profit: float
    margin: float
    confidence: str
    comps: List[float] = field(default_factory=list)
    new_min: Optional[float] = None
    new_median: Optional[float] = None
    verdict: Optional[str] = None

    @property
    def is_good(self) -> bool:
        if self.new_min and self.price > self.new_min * MAX_NEW_RATIO:
            return False
        return self.margin >= MIN_MARGIN and self.profit >= MIN_PROFIT


class DealEngine:
    def __init__(self, store: db.Store, ai: Optional[AI] = None, prices: Optional[PriceLookup] = None):
        self.store = store
        self.ai = ai or AI(store)
        self.prices = prices or PriceLookup(store, ai=self.ai)
        self.last_eval = None
        self.last_skip = None

    @property
    def enabled(self) -> bool:
        return self.ai.enabled

    # ---------------------------------------------------------------- analīze

    def analyse(self, l: Listing) -> Optional[Dict[str, Any]]:
        if l.price is None or l.price > ANALYSE_MAX_PRICE or l.is_dealer:
            return None
        facts = self.ai.extract(l.category, l.subcategory, l.title, l.description or "",
                                l.params, l.price, l.city)
        if facts is None:
            return None
        values = dict(model_key=normalize_key(facts["model_key"]) or None,
                      item_type=facts["item_type"], ai_json=json.dumps(facts, ensure_ascii=False),
                      ai_at=db.now())
        if facts["seller_is_business"] and not l.is_dealer:
            # reklāmas stila veikali, ko noteikumi nenoķēra
            l.dealer_reasons = l.dealer_reasons + ["GPT: firma"]
            l.dealer_score = max(l.dealer_score, 3)
            values.update(is_dealer=1, dealer_score=l.dealer_score,
                          dealer_reasons="; ".join(l.dealer_reasons))
        with self.store.engine.begin() as c:
            c.execute(update(db.listings).where(db.listings.c.ss_id == l.ss_id).values(**values))
        return facts

    # ---------------------------------------------------------------- tirgus

    def comparables(self, model_key: str, exclude_id: str) -> List[float]:
        since = db.now() - timedelta(days=COMPS_DAYS)
        L = db.listings
        with self.store.engine.connect() as c:
            rows = c.execute(
                select(L.c.price, L.c.ai_json)
                .where(L.c.model_key == model_key, L.c.ss_id != exclude_id, L.c.is_dealer == 0,
                       L.c.price.isnot(None), L.c.first_seen >= since,
                       L.c.status.in_(["active", "disappeared"]))
            ).all()
        prices = []
        for price, ai_json in rows:
            cond = json.loads(ai_json or "{}").get("condition")
            if cond not in ("broken", "for_parts"):
                prices.append(price)
        if len(prices) >= COMPS_MIN:  # izmetam acīmredzamas kļūdas (1 € / 9999 €)
            med = statistics.median(prices)
            prices = [p for p in prices if med * 0.3 <= p <= med * 3]
        return sorted(prices)

    def evaluate(self, l: Listing, facts: Dict[str, Any]) -> Optional[Deal]:
        price = l.price
        broken = facts["condition"] in ("broken", "for_parts")
        comps = [] if broken else self.comparables(normalize_key(facts["model_key"]), l.ss_id)

        # saliktam datoram/komplektam "jaunas cenas" nav — GPT reizēm dod tikai CPU nosaukumu
        query = facts.get("search_query") if facts["item_type"] not in ("desktop", "bundle") else None
        new = self.prices.get(query) if query else None
        new_min = new.min_price if new and new.n else None
        new_median = new.median_price if new and new.n else None

        verdict = None
        if len(comps) >= COMPS_MIN:
            resale = statistics.median(comps)
            if facts["condition"] == "new" and new_min:
                resale = min(resale, new_min * 0.95)
            confidence = "high" if len(comps) >= 8 else "medium"
        else:
            if new_min and not broken and price >= new_min * NEW_PRICE_CEILING:
                resale, confidence = min(price, new_min * NEW_PRICE_CEILING), "medium"
            else:
                est = self.ai.estimate_resale(facts, price, (new_min, new_median) if new_min else None, comps)
                if not est or not est.get("resale_price_eur"):
                    return None
                resale = float(est["resale_price_eur"])
                confidence = "low" if est["confidence"] == "low" else "medium"
                verdict = est.get("verdict_lv")

        profit = resale - price
        deal = Deal(price=price, resale=resale, profit=profit, margin=profit / price if price else 0,
                    confidence=confidence, comps=comps, new_min=new_min, new_median=new_median,
                    verdict=verdict)
        with self.store.engine.begin() as c:
            c.execute(update(db.listings).where(db.listings.c.ss_id == l.ss_id).values(
                resale_est=round(resale, 2), margin=round(deal.margin, 3), deal_conf=confidence,
                deal_note=verdict))
        return deal

    # ---------------------------------------------------------------- izjaukšana

    def evaluate_partout(self, l: Listing, facts: Dict[str, Any]) -> Optional[PartOut]:
        comps_in = []
        for c in facts.get("components") or []:
            key = normalize_key(c["key"])
            if not key or re.search(r"\b(none|nav|нет)\b", key):
                continue  # "dvd drive none" u.tml.
            known = "unknown" not in key
            comps = self.comparables(key, l.ss_id) if known else []
            item = {"type": c["type"], "key": key, "ss_com_prices": comps[:15]}
            # maz salīdzinājumu -> jaunā cena LV kā atskaites punkts (kešots 7 dienas)
            if known and len(comps) < COMPS_MIN and c["type"] in ("cpu", "gpu", "motherboard", "ram", "storage", "psu"):
                new = self.prices.get(key)
                if new and new.n:
                    item["new_price_latvia_min_eur"] = new.min_price
            comps_in.append(item)
        if len(comps_in) < 3:
            return None
        est = self.ai.estimate_parts(comps_in, l.price)
        if not est:
            return None
        by_key = {p["key"]: max(0.0, float(p["used_price_eur"])) for p in est["parts"]}
        parts = []
        for c in comps_in:
            price = by_key.get(c["key"], 0.0)
            # RAM/SSD vēl ražo, un 2026. gadā tie ir dārgi; GPT tos mēdz novērtēt par zemu
            if c["type"] in ("ram", "storage") and not c["ss_com_prices"] and c.get("new_price_latvia_min_eur"):
                price = max(price, c["new_price_latvia_min_eur"] * USED_FLOOR_OF_NEW)
            parts.append({"type": c["type"], "key": c["key"], "price": round(price),
                          "comps": len(c["ss_com_prices"])})
        po = PartOut(price=l.price, total=sum(p["price"] for p in parts), parts=parts,
                     verdict=est.get("verdict_lv"))
        with self.store.engine.begin() as c:
            c.execute(update(db.listings).where(db.listings.c.ss_id == l.ss_id).values(
                partout_est=round(po.total, 2), partout_json=json.dumps(parts, ensure_ascii=False)))
        return po

    # ---------------------------------------------------------------- jauns sludinājums

    def location_ok(self, l: Listing, facts: Dict[str, Any]) -> bool:
        city = (l.city or "").lower()
        return any(loc in city for loc in LOCATIONS) or bool(facts.get("delivers_to_riga"))

    def handle_new(self, l: Listing) -> bool:
        """Atgriež True, ja aizsūtīja paziņojumu par darījumu.
        Vērtējumu (arī negatīvu) atstāj self.last_eval, lai filtra ziņa var to parādīt."""
        self.last_eval = None
        self.last_skip = None  # "location" -> arī filtra ziņu nesūtām (Edgars pērk tikai Rīgā)
        if not self.enabled or l.price is None or l.is_dealer:
            return False
        facts = self.analyse(l)
        if facts is None or l.is_dealer:
            return False
        if not self.location_ok(l, facts):
            self.last_skip = "location"
            return False
        if l.price > MAX_PRICE or l.price < 5:
            return False
        deal = self.evaluate(l, facts)
        if deal is not None:
            self.last_eval = (facts, deal, None)
        if (deal is None or not deal.is_good) and facts["item_type"] in ("desktop", "bundle"):
            po = self.evaluate_partout(l, facts)
            if po is not None:
                self.last_eval = (facts, deal, po) if deal else None
                if po.is_good and telegram.send_listing(l, "", text=format_partout(l, facts, po)):
                    with self.store.engine.begin() as c:
                        c.execute(update(db.listings).where(db.listings.c.ss_id == l.ss_id).values(
                            deal_sent_at=db.now(), notified_at=db.now(), matched_filter="AI pa daļām"))
                    log.info("Pa daļām: %s %.0f € -> ~%.0f €", l.url, po.price, po.total)
                    return True
        if deal is None or not deal.is_good:
            if deal:
                log.info("Nav darījums: %s %.0f € -> ~%.0f € (%+.0f%%)", facts["model_key"], deal.price,
                         deal.resale, deal.margin * 100)
            return False
        if telegram.send_listing(l, "", extra=None, text=format_deal(l, facts, deal)):
            with self.store.engine.begin() as c:
                c.execute(update(db.listings).where(db.listings.c.ss_id == l.ss_id)
                          .values(deal_sent_at=db.now(), notified_at=db.now(), matched_filter="AI darījums"))
            log.info("Darījums: %s %.0f € -> ~%.0f € (%+.0f%%)", facts["model_key"], deal.price,
                     deal.resale, deal.margin * 100)
            return True
        return False


PART_LV = {"cpu": "CPU", "gpu": "Video", "motherboard": "Plate", "ram": "RAM", "storage": "Disks",
           "psu": "Barošana", "cooler": "Dzesēšana", "case": "Korpuss", "other": "Cits"}


def format_partout(l: Listing, facts: Dict[str, Any], po: PartOut) -> str:
    e = html.escape
    lines = [
        f"🔧 <b>Pa daļām +{po.profit:.0f} € ({po.margin * 100:.0f}%)</b> · prasa {po.price:.0f} € → detaļas ~{po.total:.0f} €",
        f"<b>{e(l.title[:120])}</b>",
    ]
    for p in sorted(po.parts, key=lambda x: -x["price"]):
        src = f" · ss.com {p['comps']}×" if p["comps"] else ""
        lines.append(f"  {PART_LV.get(p['type'], p['type'])}: {e(p['key'][:45])} — ~{p['price']:.0f} €{src}")
    lines.append(f"📍 {e(l.city or '?')}")
    if po.verdict:
        lines.append("🤖 " + e(po.verdict[:300]))
    lines.append(f'<a href="{e(l.url)}">Atvērt ss.com</a>')
    return "\n".join(lines)


def format_eval_line(facts: Dict[str, Any], d: Deal, po: Optional[PartOut] = None) -> str:
    """Īss vērtējums filtra ziņai, ja tas nav darījums."""
    e = html.escape
    why = []
    if d.new_min and d.price > d.new_min * MAX_NEW_RATIO:
        why.append(f"{d.price / d.new_min * 100:.0f}% no jaunas cenas ({d.new_min:.0f} €)")
    if d.margin < MIN_MARGIN or d.profit < MIN_PROFIT:
        why.append(f"peļņa {d.profit:+.0f} € ({d.margin * 100:+.0f}%)")
    line = (f"📉 <b>Nav darījums</b>: pārdot ~{d.resale:.0f} € · " + e(", ".join(why)) +
            f" · pārliecība: {CONF_LV.get(d.confidence, d.confidence)}")
    if d.verdict:
        line += "\n🤖 " + e(d.verdict[:250])
    if po is not None:
        line += f"\n🔧 pa daļām ~{po.total:.0f} € ({po.margin * 100:+.0f}%)"
    return line


def format_deal(l: Listing, facts: Dict[str, Any], d: Deal) -> str:
    e = html.escape
    # saliktam datoram modelis = "cpu + gpu", tāpēc rādām komponentes; citām precēm — modeli
    composite = facts["item_type"] in ("desktop", "bundle") or "+" in (facts.get("model_key") or "")
    specs = [] if composite else [facts.get("model_key") or ""]
    if facts.get("release_year"):
        specs.append(str(facts["release_year"]))
    keys = (("cpu", ""), ("gpu", "")) if composite else ()
    for key, suffix in keys + (("ram_gb", " GB RAM"), ("storage_gb", " GB")):
        v = facts.get(key)
        if v:
            specs.append(f"{v}{suffix}")
    specs.append(CONDITION_LV.get(facts["condition"], facts["condition"]))

    market = []
    if d.comps:
        market.append(f"ss.com {len(d.comps)} līdzīgi: {min(d.comps):.0f}–{max(d.comps):.0f} €")
    if d.new_min:
        market.append(f"jauns LV no {d.new_min:.0f} €")

    lines = [
        f"💰 <b>+{d.profit:.0f} € ({d.margin * 100:.0f}%)</b> · prasa {d.price:.0f} € → pārdot ~{d.resale:.0f} €",
        f"<b>{e(l.title[:120])}</b>",
        e(" · ".join(s for s in specs if s)),
    ]
    if market:
        lines.append("📊 " + e(" · ".join(market)))
    if facts.get("defects"):
        lines.append("⚠️ " + e(facts["defects"][:150]))
    if facts.get("urgent"):
        lines.append("⚡ " + e(facts.get("urgency_note") or "steidzami"))
    loc = l.city or "?"
    if facts.get("delivers_to_riga") and "rīg" not in loc.lower():
        loc += " (atved uz Rīgu)"
    lines.append(f"📍 {e(loc)} · pārliecība: {CONF_LV.get(d.confidence, d.confidence)}")
    if d.verdict:
        lines.append("🤖 " + e(d.verdict[:300]))
    lines.append(f'<a href="{e(l.url)}">Atvērt ss.com</a>')
    return "\n".join(lines)
