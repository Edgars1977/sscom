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
COMPS_MIN = 3
COMPS_DAYS = 120
# ja lietota prece maksā >= 85% no jaunas lētākās cenas, peļņas praktiski nevar būt — GPT nesaucam
NEW_PRICE_CEILING = 0.85

CONDITION_LV = {"new": "jauns", "like_new": "kā jauns", "used": "lietots",
                "broken": "bojāts", "for_parts": "uz detaļām"}
CONF_LV = {"low": "zema", "medium": "vidēja", "high": "augsta"}


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
        values = dict(model_key=(facts["model_key"] or "").strip().lower() or None,
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
        comps = [] if broken else self.comparables(facts["model_key"].strip().lower(), l.ss_id)

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

    # ---------------------------------------------------------------- jauns sludinājums

    def location_ok(self, l: Listing, facts: Dict[str, Any]) -> bool:
        city = (l.city or "").lower()
        return any(loc in city for loc in LOCATIONS) or bool(facts.get("delivers_to_riga"))

    def handle_new(self, l: Listing) -> bool:
        """Atgriež True, ja aizsūtīja paziņojumu par darījumu.
        Vērtējumu (arī negatīvu) atstāj self.last_eval, lai filtra ziņa var to parādīt."""
        self.last_eval = None
        if not self.enabled or l.price is None or l.is_dealer:
            return False
        facts = self.analyse(l)
        if facts is None or l.is_dealer:
            return False
        if l.price > MAX_PRICE or l.price < 5 or not self.location_ok(l, facts):
            return False
        deal = self.evaluate(l, facts)
        if deal is not None:
            self.last_eval = (facts, deal)
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


def format_eval_line(facts: Dict[str, Any], d: Deal) -> str:
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
