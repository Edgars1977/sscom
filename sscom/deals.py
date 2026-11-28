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

from . import db, filters, telegram
from .ai import AI
from .cenueksperts import PriceLookup
from .parser import Listing

log = logging.getLogger(__name__)

MIN_MARGIN = float(os.getenv("DEAL_MIN_MARGIN", "0.20"))
MIN_PROFIT = float(os.getenv("DEAL_MIN_PROFIT", "15"))
MAX_PRICE = float(os.getenv("DEAL_MAX_PRICE", "700"))
# Edgara noteikums: mazlietotu vērts pirkt tikai par ~pusi no jaunas cenas (ja jaunā cena zināma)
MAX_NEW_RATIO = float(os.getenv("DEAL_MAX_NEW_RATIO", "0.5"))
ANALYSE_MAX_PRICE = float(os.getenv("DEAL_ANALYSE_MAX_PRICE", "1000"))  # salīdzinājumiem analizējam plašāk
LOCATIONS = [x.strip().lower() for x in os.getenv("DEAL_LOCATIONS", "rīga").split(",") if x.strip()]
# Izjaukšana prasa vairāk darba (6–8 sludinājumi, nedēļas) — tāpēc augstāks slieksnis
PARTOUT_MIN_MARGIN = float(os.getenv("DEAL_PARTOUT_MIN_MARGIN", "0.30"))
PARTOUT_MIN_PROFIT = float(os.getenv("DEAL_PARTOUT_MIN_PROFIT", "60"))
USED_FLOOR_OF_NEW = 0.55  # lietota DDR4/DDR5 RAM vismaz tik no jaunas cenas
COMPS_MIN = 3
COMPS_DAYS = 120
# ja lietota prece maksā >= 85% no jaunas lētākās cenas, peļņas praktiski nevar būt — GPT nesaucam
NEW_PRICE_CEILING = 0.85
# Portatīvie bez precīzā modeļa salīdzinājumiem: hibrīds GPT + ML (laptop_ml.py), vidējais.
# Izpēte: ar ≥30% slieksni hibrīda karogi pazūd ≤3 d 39% (GPT vien 36%, vidēji 24%).
HYBRID_MIN_MARGIN = float(os.getenv("DEAL_HYBRID_MIN_MARGIN", "0.30"))
# Klases salīdzinājumi tiek GPT kā konteksts (paši par sevi kā cena bija slikti — 17%)
# Ātrā aprite (cenu ceļvedis): grupa, kurā ≥50% sludinājumu pazūd 7 dienās un mediāna ≤4 d
FAST_MIN_KNOWN = 5
FAST7_MIN = float(os.getenv("DEAL_FAST7_MIN", "0.5"))
FAST_DAYS_MAX = float(os.getenv("DEAL_FAST_DAYS_MAX", "4"))
SLOW_DAYS = 12
CLASS_MIN = {"fine": 5, "mid": 4, "broad": 4}
CLASS_PERCENTILE = 0.4      # klase ir plašāka par modeli — vērtējam piesardzīgāk par mediānu
BATTERY_COST = float(os.getenv("DEAL_BATTERY_COST", "25"))  # neoriģināla baterija LV ~22–30 €
BATTERY_RE = re.compile(r"baterij|akumulator|battery|аккумулятор|батаре", re.I)

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
    new_query: Optional[str] = None      # ar ko atradām jauno cenu (saitei)
    alt_query: Optional[str] = None      # līdzvērtīgs jauns, ja šo vairs nepārdod
    alt_name: Optional[str] = None
    alt_min: Optional[float] = None
    comps_label: Optional[str] = None    # "intel 6.paaudze H · dGPU vāja" — ja salīdzināts ar klasi
    battery_deducted: bool = False
    gpt_est: Optional[float] = None      # hibrīdam: abi viedokļi atsevišķi
    ml_est: Optional[float] = None
    repairs: List[Any] = field(default_factory=list)   # [("baterija", 25.0), ...]
    min_margin: float = MIN_MARGIN
    speed: Optional[str] = None          # ⚡/⏱/🐢 rinda no cenu ceļveža

    @property
    def is_good(self) -> bool:
        if self.new_min and self.price > self.new_min * MAX_NEW_RATIO:
            return False
        if self.ml_est is not None and self.gpt_est is None:
            return False  # ML viens pats nav uzticams (izpēte: 16% pret vidējo 24%)
        return self.margin >= self.min_margin and self.profit >= MIN_PROFIT

    @property
    def hot(self) -> bool:
        """🔥 abi (GPT un ML) katrs atsevišķi saka ≥20% un ≥15 €"""
        return all(v is not None and v - self.price >= max(MIN_PROFIT, self.price * MIN_MARGIN)
                   for v in (self.gpt_est, self.ml_est))


class DealEngine:
    def __init__(self, store: db.Store, ai: Optional[AI] = None, prices: Optional[PriceLookup] = None):
        self.store = store
        self.ai = ai or AI(store)
        self.prices = prices or PriceLookup(store, ai=self.ai)
        self.last_eval = None
        self.last_skip = None
        from .laptop_ml import LaptopModel
        self.ml = LaptopModel(store)

    @property
    def enabled(self) -> bool:
        return self.ai.enabled

    # ---------------------------------------------------------------- analīze

    def analyse(self, l: Listing, force: bool = False) -> Optional[Dict[str, Any]]:
        """force: analizē arī virs ANALYSE_MAX_PRICE ("vienmēr rādīt" noteikumiem)."""
        if l.price is None or l.is_dealer or (l.price > ANALYSE_MAX_PRICE and not force):
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

    def comparables(self, model_key: str, exclude_id: str, family: Optional[str] = None) -> List[float]:
        """Privāto līdzīgo cenas. family (portatīvajiem 'dell latitude 5520') paplašina salīdzinājumu."""
        since = db.now() - timedelta(days=COMPS_DAYS)
        L = db.listings
        same = (L.c.model_key == model_key) | (L.c.guide_key == family) if family else (L.c.model_key == model_key)
        with self.store.engine.connect() as c:
            rows = c.execute(
                select(L.c.price, L.c.ai_json)
                .where(same, L.c.ss_id != exclude_id, L.c.is_dealer == 0,
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

    def class_comparables(self, facts: Dict[str, Any], exclude_id: str) -> Optional[tuple]:
        """Portatīvajiem: tās pašas klases privātie sludinājumi -> (cenas, klase, tālākpārdošanas cena).
        Precīzā klase -> plašākā (CPU + GPU jauda) -> ±1 paaudze. None, ja nekur nav pietiekami."""
        from . import guide
        cls = guide.laptop_class(facts)
        if not cls:
            return None
        since = db.now() - timedelta(days=COMPS_DAYS)
        L = db.listings
        with self.store.engine.connect() as c:
            rows = c.execute(
                select(L.c.price, L.c.status, L.c.ai_json)
                .where(L.c.item_type == "laptop", L.c.ss_id != exclude_id, L.c.is_dealer == 0,
                       L.c.price.isnot(None), L.c.ai_json.isnot(None), L.c.first_seen >= since,
                       L.c.status.in_(["active", "disappeared"]))
            ).all()
        levels = {"fine": [], "mid": [], "broad": []}
        for price, status, ai_json in rows:
            f = json.loads(ai_json)
            if f.get("condition") in ("broken", "for_parts"):
                continue
            other = guide.laptop_class(f)
            if not other:
                continue
            if other["fine"] == cls["fine"]:
                levels["fine"].append(price)
            if other["mid"] == cls["mid"]:
                levels["mid"].append(price)
            if guide.same_broad_class(other["spec"], cls["spec"]):
                levels["broad"].append(price)
        labels = {"fine": cls["fine"], "mid": cls["mid"], "broad": guide.broad_label(cls["spec"])}
        for level in ("fine", "mid", "broad"):
            prices = levels[level]
            if len(prices) < CLASS_MIN[level]:
                continue
            med = statistics.median(prices)
            prices = sorted(p for p in prices if med * 0.3 <= p <= med * 3)
            resale = prices[int(CLASS_PERCENTILE * (len(prices) - 1))]
            return prices, labels[level], resale
        return None

    def evaluate(self, l: Listing, facts: Dict[str, Any]) -> Optional[Deal]:
        price = l.price
        broken = facts["condition"] in ("broken", "for_parts")
        from . import guide
        family = guide.laptop_family(facts["model_key"].lower()) if facts["item_type"] == "laptop" else None
        comps = [] if broken else self.comparables(normalize_key(facts["model_key"]), l.ss_id, family)
        laptop = facts["item_type"] == "laptop"
        comps_label = None
        class_comps: List[float] = []
        if laptop and not broken and len(comps) < COMPS_MIN:
            cc = self.class_comparables(facts, l.ss_id)
            if cc:
                class_comps, comps_label, _ = cc

        # saliktam datoram/komplektam ("cpu + gpu") jaunas cenas nav — GPT reizēm dod tikai CPU nosaukumu.
        # Zīmolu datori (Mac mini, OptiPlex, NUC) gan ir pārdošanā kā viena prece.
        custom = facts["item_type"] == "bundle" or (facts["item_type"] == "desktop" and "+" in (facts.get("model_key") or ""))
        query = facts.get("search_query") if not custom else None
        new = self.prices.get(query) if query else None
        new_min = new.min_price if new and new.n else None
        new_median = new.median_price if new and new.n else None
        if new_min and new_min < price * 0.4:
            # "jauns" lētāks par 40% no lietotā = piederums (lādētājs, baterija), nevis prece
            log.info("Jaunā cena %.0f € neticama pret %.0f € (%s) — ignorēju", new_min, price, query)
            new_min = new_median = None

        # Ja tieši šo jaunu vairs nepārdod — līdzvērtīgs mūsdienu modelis (tikai informācijai un saitei)
        alt_query = alt_name = alt_min = None
        alt = (facts.get("new_alternative_query") or "").strip()
        if not custom and not new_min and alt and alt.lower() != (query or "").lower():
            a = self.prices.get(alt)
            if a and a.n and a.min_price and a.min_price >= price * 0.4:
                alt_query, alt_name, alt_min = alt, (a.names[0] if a.names else alt), a.min_price

        verdict = None
        battery = False
        deal_extra: Dict[str, Any] = {}
        if len(comps) >= COMPS_MIN:
            resale = statistics.median(comps)
            # salīdzinājumi ir strādājoši; beigtu bateriju nomainīsim (~25 €), nevis norakstīsim pusi cenas
            if facts["item_type"] == "laptop" and BATTERY_RE.search(facts.get("defects") or ""):
                resale -= BATTERY_COST
                battery = True
            if facts["condition"] == "new" and new_min:
                resale = min(resale, new_min * 0.95)
            confidence = "high" if len(comps) >= 8 else "medium"
        else:
            if new_min and not broken and price >= new_min * NEW_PRICE_CEILING:
                resale, confidence = min(price, new_min * NEW_PRICE_CEILING), "medium"
            else:
                est = self.ai.estimate_resale(facts, price, (new_min, new_median) if new_min else None,
                                              comps or class_comps,
                                              comps_note=f"same laptop class ({comps_label}), not exact model"
                                              if class_comps and not comps else None)
                gpt = float(est["resale_price_eur"]) if est and est.get("resale_price_eur") else None
                ml = None
                if laptop and not broken:
                    try:
                        ml = self.ml.value(facts, l.title, l.description, l.params)
                    except Exception:
                        log.exception("Portatīvo ML neizdevās: %s", l.url)
                if gpt is None and ml is None:
                    return None
                verdict = est.get("verdict_lv") if est else None
                if ml is not None:
                    ml_val, repairs, _ = ml
                    resale = (gpt + ml_val) / 2 if gpt is not None else ml_val
                    confidence = "medium" if gpt is not None else "low"
                    if est and est["confidence"] == "low" and gpt is not None:
                        confidence = "low"
                    deal_extra = dict(gpt_est=gpt, ml_est=ml_val, repairs=repairs, min_margin=HYBRID_MIN_MARGIN)
                else:
                    resale = gpt
                    confidence = "low" if est["confidence"] == "low" else "medium"

        profit = resale - price
        deal = Deal(price=price, resale=resale, profit=profit, margin=profit / price if price else 0,
                    confidence=confidence, comps=comps, new_min=new_min, new_median=new_median,
                    verdict=verdict, new_query=query if new_min else None,
                    alt_query=alt_query, alt_name=alt_name, alt_min=alt_min,
                    battery_deducted=battery, **deal_extra)
        if class_comps and not comps:
            deal.comps, deal.comps_label = class_comps, comps_label
        try:
            deal.speed = speed_line(guide_row(self.store, facts))
        except Exception:
            log.exception("Ātruma rinda neizdevās")
        with self.store.engine.begin() as c:
            c.execute(update(db.listings).where(db.listings.c.ss_id == l.ss_id).values(
                resale_est=round(resale, 2), margin=round(deal.margin, 3), deal_conf=confidence,
                deal_note=verdict, gpt_est=deal.gpt_est, ml_est=round(deal.ml_est, 2) if deal.ml_est else None))
        return deal

    # ---------------------------------------------------------------- izjaukšana

    def evaluate_partout(self, l: Listing, facts: Dict[str, Any]) -> Optional[PartOut]:
        # Pielodēti (Apple, monobloki, mini PC) — pa daļām nepārdod
        if re.search(r"\b(apple|mac\s?mini|imac|mac\s?studio|all[\s-]?in[\s-]?one|aio|proone|nuc|"
                     r"tiny|micro|mini\s?pc|monoblok)", (facts.get("model_key") or "") + " " + (l.title or ""), re.I):
            return None
        comps_in = []
        for c in facts.get("components") or []:
            key = normalize_key(c["key"])
            if not key or re.search(r"\b(none|nav|нет)\b", key):
                continue  # "dvd drive none" u.tml.
            known = "unknown" not in key
            comps = self.comparables(key, l.ss_id) if known else []
            item = {"type": c["type"], "key": key, "ss_com_prices": comps[:15]}
            # maz salīdzinājumu -> jaunā cena LV kā atskaites punkts (kešots 7 dienas)
            # diskiem NĒ: "hdd 480gb" ir pārāk vispārīgs, cenueksperts atrod servera diskus par 700 €
            if known and len(comps) < COMPS_MIN and c["type"] in ("cpu", "gpu", "motherboard", "ram", "psu"):
                new = self.prices.get(key)
                if new and new.n >= 3:
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
            # DDR4/DDR5 vēl ražo, un 2026. gadā tā ir dārga; GPT to mēdz novērtēt par zemu
            if (c["type"] == "ram" and re.search(r"ddr[45]", c["key"]) and not c["ss_com_prices"]
                    and c.get("new_price_latvia_min_eur")):
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

    def _target_alert(self, l: Listing, facts: Dict[str, Any]) -> bool:
        """🎯 Ja Edgars ceļvedī ir ierakstījis savu cenu šai grupai un sludinājums ir zem tās."""
        from . import guide
        key = guide.guide_key(facts)
        if not key:
            return False
        with self.store.engine.begin() as c:
            c.execute(update(db.listings).where(db.listings.c.ss_id == l.ss_id).values(guide_key=key))
        target = guide.target_for(self.store, key)
        if target is None or l.price > target or facts["condition"] in ("broken", "for_parts"):
            return False
        with self.store.engine.connect() as c:
            g = c.execute(select(guide.price_guide).where(guide.price_guide.c.guide_key == key)).first()
        e = html.escape
        lines = [f"🎯 <b>{l.price:.0f} € — zem tavas cenas {target:.0f} €</b> · {e(key)}",
                 f"<b>{e(l.title[:120])}</b>"]
        if gen_line(facts):
            lines.append(gen_line(facts))
        if g:
            lines.append(f"📊 ss.com {g.n}×, mediāna {g.price_median:.0f} €"
                         + (f" · pārdodas ~{g.days_to_sell:.0f} d" if g.days_to_sell else "")
                         + (f" · jauns LV {g.new_min:.0f} €" if g.new_min else ""))
            if speed_line(g):
                lines.append(speed_line(g))
        if facts.get("defects"):
            lines.append("⚠️ " + e(facts["defects"][:150]))
        lines.append(f"📍 {e(l.city or '?')}")
        lines.append(f'<a href="{e(l.url)}">Atvērt ss.com</a>')
        if telegram.send_listing(l, "", text="\n".join(lines)):
            with self.store.engine.begin() as c:
                c.execute(update(db.listings).where(db.listings.c.ss_id == l.ss_id).values(
                    notified_at=db.now(), notified_price=l.price, deal_sent_at=db.now(), matched_filter="AI mana cena"))
            return True
        return False

    def _fast_flip_alert(self, l: Listing, facts: Dict[str, Any], deal: Optional[Deal]) -> bool:
        """⚡ Ātrā aprite: grupa pārdodas ātri un šis ir ≤ mediāna/1.2 (Edgara 20%) — pat ja AI vērtējums
        to nepacēla. Tikai, ja ceļvedī ir pietiekami datu."""
        if facts["condition"] in ("broken", "for_parts"):
            return False
        g = guide_row(self.store, facts)
        if not is_fast(g) or g.n < COMPS_MIN or l.price > g.buy_max or g.price_median - l.price < MIN_PROFIT:
            return False
        if deal is not None and deal.new_min and l.price > deal.new_min * MAX_NEW_RATIO:
            return False  # Edgara noteikums: ne dārgāk par pusi no jaunas cenas
        e = html.escape
        profit = g.price_median - l.price
        lines = [f"⚡ <b>Ātrais flips: {l.price:.0f} € → tirgū ~{g.price_median:.0f} € "
                 f"(+{profit:.0f} €, {profit / l.price * 100:.0f}%)</b>",
                 f"<b>{e(l.title[:120])}</b>", e(g.guide_key)]
        if gen_line(facts):
            lines.append(gen_line(facts))
        lines.append(speed_line(g))
        lines.append(f"📊 ss.com {g.n}×: {g.price_min:.0f}–{g.price_max:.0f} € · pirkt līdz {g.buy_max:.0f} €"
                     + (f" · jauns LV {g.new_min:.0f} €" if g.new_min else ""))
        if deal is not None:
            lines.append(f"🤖 AI vērtējums: ~{deal.resale:.0f} € ({deal.margin * 100:+.0f}%)"
                         + (" — " + e(deal.verdict[:200]) if deal.verdict else ""))
        if facts.get("defects"):
            lines.append("⚠️ " + e(facts["defects"][:150]))
        lines.append(f"📍 {e(l.city or '?')}")
        lines.append(f'<a href="{e(l.url)}">Atvērt ss.com</a>')
        if telegram.send_listing(l, "", text="\n".join(lines)):
            with self.store.engine.begin() as c:
                c.execute(update(db.listings).where(db.listings.c.ss_id == l.ss_id).values(
                    notified_at=db.now(), notified_price=l.price, deal_sent_at=db.now(), matched_filter="AI ātrais flips"))
            log.info("Ātrais flips: %s %.0f € (mediāna %.0f €)", l.url, l.price, g.price_median)
            return True
        return False

    def handle_price_drop(self, ss_id: str, old_price: float, new_price: float) -> bool:
        """📉 Zināmam sludinājumam samazināta cena — vai tagad tas ir darījums?
        Atgriež True, ja aizsūtīja ziņu."""
        from . import guide
        L = db.listings
        with self.store.engine.connect() as c:
            r = c.execute(select(L).where(L.c.ss_id == ss_id)).first()
        if r is None or r.is_dealer or r.status == "no_price" or not self.enabled:
            return False
        if r.notified_price is not None and new_price >= r.notified_price:
            return False  # par šo (vai zemāku) cenu jau ziņots
        l = db.row_to_listing(r)
        l.price = new_price
        facts = json.loads(r.ai_json) if r.ai_json else self.analyse(l)
        if not facts or l.is_dealer:
            return False
        if not self.location_ok(l, facts) or new_price > MAX_PRICE or new_price < 5:
            return False

        key = guide.guide_key(facts)
        target = guide.target_for(self.store, key)
        with self.store.engine.connect() as c:
            g = c.execute(select(guide.price_guide).where(guide.price_guide.c.guide_key == key)).first() if key else None
        hit_target = target is not None and new_price <= target
        hit_guide = g is not None and g.n >= COMPS_MIN and new_price <= g.buy_max

        deal = None
        # GPT novērtējumu tērējam tikai, ja jaunā cena vispār var būt izdevīga
        if not (g is not None and g.n >= COMPS_MIN and new_price > g.price_median):
            deal = self.evaluate(l, facts)
        good = deal is not None and deal.is_good
        if not (good or hit_target or hit_guide):
            log.info("Cena samazināta, bet nav darījums: %s %.0f→%.0f €", l.url, old_price, new_price)
            return False

        e = html.escape
        drop = (old_price - new_price) / old_price * 100 if old_price else 0
        head = f"📉 <b>Cena samazināta: {old_price:.0f} → {new_price:.0f} €</b> (−{drop:.0f}%)"
        stale = age_line(self.store, ss_id)
        if stale:
            head += "\n" + stale
        if good:
            body = format_deal(l, facts, deal)
        else:
            lines = [f"<b>{e(l.title[:120])}</b>", e(key or facts.get("model_key") or "")]
            if gen_line(facts):
                lines.append(gen_line(facts))
            if hit_target:
                lines.append(f"🎯 zem tavas cenas {target:.0f} €")
            if g is not None:
                lines.append(f"📊 ss.com {g.n}×, mediāna {g.price_median:.0f} € · pirkt līdz {g.buy_max:.0f} €"
                             + (f" · pārdodas ~{g.days_to_sell:.0f} d" if g.days_to_sell else ""))
            if speed_line(g):
                lines.append(speed_line(g))
            if facts.get("defects"):
                lines.append("⚠️ " + e(facts["defects"][:150]))
            lines.append(f"📍 {e(l.city or '?')}")
            lines.append(f'<a href="{e(l.url)}">Atvērt ss.com</a>')
            body = "\n".join(lines)
        if telegram.send_listing(l, "", text=head + "\n" + body):
            with self.store.engine.begin() as c:
                c.execute(update(L).where(L.c.ss_id == ss_id).values(
                    notified_at=db.now(), notified_price=new_price, deal_sent_at=db.now(),
                    matched_filter="AI cena samazināta"))
            log.info("Cena samazināta → ziņa: %s %.0f→%.0f €", l.url, old_price, new_price)
            return True
        return False

    def _watch_alert(self, l: Listing, facts: Dict[str, Any], w) -> bool:
        """👀 "Vienmēr rādīt" — neatkarīgi no budžeta, vietas un peļņas, bet ar novērtējumu."""
        e = html.escape
        deal = None
        try:
            deal = self.evaluate(l, facts)
        except Exception:
            log.exception("Novērtējums neizdevās: %s", l.url)
        specs = [facts.get("model_key") or "", facts.get("cpu") or ""]
        if facts.get("ram_gb"):
            specs.append(f"{facts['ram_gb']} GB RAM")
        if facts.get("storage_gb"):
            specs.append(f"{facts['storage_gb']} GB SSD")
        specs.append(CONDITION_LV.get(facts["condition"], facts["condition"]))
        lines = [f"👀 <b>{e(w.name)}</b> · {l.price:.0f} € · {e(l.city or '?')}",
                 f"<b>{e(l.title[:120])}</b>", e(" · ".join(s for s in specs if s))]
        if gen_line(facts):
            lines.append(gen_line(facts))
        if deal is not None:
            market = []
            if deal.comps:
                market.append(comps_text(deal))
            if deal.new_min:
                market.append(f"jauns LV no {deal.new_min:.0f} € ({l.price / deal.new_min * 100:.0f}%)")
            if market:
                lines.append("📊 " + e(" · ".join(market)))
            if deal.speed:
                lines.append(deal.speed)
            lines.extend(hybrid_lines(deal))
            if shop_line(deal):
                lines.append(shop_line(deal))
            verdict = "💰 <b>izdevīgi</b>" if deal.is_good else "📉 nav izdevīgi"
            lines.append(f"{verdict}: pārdot ~{deal.resale:.0f} € ({deal.profit:+.0f} €, {deal.margin * 100:+.0f}%)"
                         f" · pārliecība: {CONF_LV.get(deal.confidence, deal.confidence)}")
            if deal.verdict:
                lines.append("🤖 " + e(deal.verdict[:300]))
        if facts.get("defects"):
            lines.append("⚠️ " + e(facts["defects"][:150]))
        lines.append(f'<a href="{e(l.url)}">Atvērt ss.com</a>')
        if telegram.send_listing(l, "", text="\n".join(lines)):
            with self.store.engine.begin() as c:
                c.execute(update(db.listings).where(db.listings.c.ss_id == l.ss_id).values(
                    notified_at=db.now(), notified_price=l.price, matched_filter=f"vienmēr: {w.name}"))
            log.info("Vienmēr rādīt (%s): %s", w.name, l.url)
            return True
        return False

    def handle_new(self, l: Listing) -> bool:
        """Atgriež True, ja aizsūtīja paziņojumu par darījumu.
        Vērtējumu (arī negatīvu) atstāj self.last_eval, lai filtra ziņa var to parādīt."""
        self.last_eval = None
        self.last_skip = None  # "location" -> arī filtra ziņu nesūtām (Edgars pērk tikai Rīgā)
        if not self.enabled or l.price is None or l.is_dealer:
            return False
        watches = filters.load_watches()
        maybe_watch = [w for w in watches if w.text_matches(l)]
        facts = self.analyse(l, force=bool(maybe_watch))
        if facts is None or l.is_dealer:
            return False
        for w in maybe_watch:
            if w.matches(l, facts):
                return self._watch_alert(l, facts, w)
        if not self.location_ok(l, facts):
            self.last_skip = "location"
            return False
        if self._target_alert(l, facts):
            return True
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
                            deal_sent_at=db.now(), notified_at=db.now(), notified_price=l.price, matched_filter="AI pa daļām"))
                    log.info("Pa daļām: %s %.0f € -> ~%.0f €", l.url, po.price, po.total)
                    return True
        if (deal is None or not deal.is_good) and self._fast_flip_alert(l, facts, deal):
            return True
        if deal is None or not deal.is_good:
            if deal:
                log.info("Nav darījums: %s %.0f € -> ~%.0f € (%+.0f%%)", facts["model_key"], deal.price,
                         deal.resale, deal.margin * 100)
            return False
        if telegram.send_listing(l, "", extra=None, text=format_deal(l, facts, deal)):
            with self.store.engine.begin() as c:
                c.execute(update(db.listings).where(db.listings.c.ss_id == l.ss_id)
                          .values(deal_sent_at=db.now(), notified_at=db.now(), notified_price=l.price, matched_filter="AI darījums"))
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
    if gen_line(facts):
        lines.append(gen_line(facts))
    for p in sorted(po.parts, key=lambda x: -x["price"]):
        src = f" · ss.com {p['comps']}×" if p["comps"] else ""
        lines.append(f"  {PART_LV.get(p['type'], p['type'])}: {e(p['key'][:45])} — ~{p['price']:.0f} €{src}")
    lines.append(f"📍 {e(l.city or '?')}")
    if po.verdict:
        lines.append("🤖 " + e(po.verdict[:300]))
    lines.append(f'<a href="{e(l.url)}">Atvērt ss.com</a>')
    return "\n".join(lines)


STALE_DAYS = 21


def age_line(store: db.Store, ss_id: str) -> Optional[str]:
    """⏳ Ilgi nepārdots sludinājums: "ievietots 49 d · 480 → 300 → 270 € — tirgus to neņem, pajautā kāpēc"."""
    L = db.listings
    with store.engine.connect() as c:
        first = c.execute(select(L.c.first_seen).where(L.c.ss_id == ss_id)).scalar()
        hist = [p for p, in c.execute(select(db.price_history.c.price).where(db.price_history.c.ss_id == ss_id)
                                      .order_by(db.price_history.c.seen_at))]
    if first is None:
        return None
    days = (db.now() - first).days
    if days < STALE_DAYS:
        return None
    prices = [p for i, p in enumerate(hist) if i == 0 or p != hist[i - 1]]
    chain = " → ".join(f"{p:.0f}" for p in prices[-4:]) + " €" if len(prices) > 1 else ""
    return (f"⏳ <b>ievietots jau {days} d</b>" + (f" · {chain}" if chain else "") +
            " — neviens nav nopircis; pajautā pārdevējam, kāpēc (defekts? apraksts neatbilst?)")


def guide_row(store: db.Store, facts: Dict[str, Any]):
    from . import guide
    key = guide.guide_key(facts)
    if not key:
        return None
    with store.engine.connect() as c:
        return c.execute(select(guide.price_guide).where(guide.price_guide.c.guide_key == key)).first()


def is_fast(g) -> bool:
    return (g is not None and (g.n_known or 0) >= FAST_MIN_KNOWN and (g.fast7 or 0) >= FAST7_MIN
            and g.days_to_sell is not None and g.days_to_sell <= FAST_DAYS_MAX)


def speed_line(g) -> Optional[str]:
    """⚡ ātri pārdodas: 80% 7 dienās, mediāna 1.6 d (9×) · 🐢 lēni … · ⏱ vidēji …"""
    if g is None or (g.n_known or 0) < FAST_MIN_KNOWN or g.fast7 is None:
        return None
    stats = f"{g.fast7 * 100:.0f}% 7 dienās" + (f", mediāna {g.days_to_sell:.1f} d" if g.days_to_sell else "") \
        + f" ({g.n_known}× {html.escape(g.guide_key)})"
    if is_fast(g):
        return "⚡ ātri pārdodas: " + stats
    if g.fast7 < 0.3 or (g.days_to_sell or 0) >= SLOW_DAYS:
        return "🐢 lēni pārdodas: " + stats
    return "⏱ vidēji: " + stats


def hybrid_lines(d: Optional["Deal"]) -> List[str]:
    """🧮 GPT ~150 € · ML ~170 €  /  🔧 iekļauts remonts: baterija 25 € + SSD 22 €"""
    if d is None or d.ml_est is None:
        return []
    parts = [f"GPT ~{d.gpt_est:.0f} €" if d.gpt_est is not None else "GPT —", f"ML ~{d.ml_est:.0f} €"]
    out = ["🧮 " + " · ".join(parts) + (" · 🔥 abi piekrīt" if d.hot else "")]
    if d.repairs:
        out.append("🔧 ML pēc remonta, atskaitīts: " + " + ".join(f"{n} {c:.0f} €" for n, c in d.repairs))
    return out


def comps_text(d: "Deal") -> str:
    t = f"ss.com {len(d.comps)} līdzīgi"
    if d.comps_label:
        t += f" (klase: {d.comps_label})"
    t += f": {min(d.comps):.0f}–{max(d.comps):.0f} €"
    if d.battery_deducted:
        t += f" · −{BATTERY_COST:.0f} € baterijai"
    return t


def shop_line(d: Optional["Deal"]) -> Optional[str]:
    """🛒 Jauns veikalā (cenueksperts meklēšanas lapa — rāda visus veikalus, saite nenoveco)."""
    from urllib.parse import quote
    from .cenueksperts import BASE
    if d is None:
        return None
    if d.new_min and d.new_query:
        url = f"{BASE}/mekleet/{quote(d.new_query)}.html"
        return f'🛒 Jauns veikalā no {d.new_min:.0f} € → <a href="{html.escape(url)}">cenueksperts</a>'
    if d.alt_min and d.alt_query:
        url = f"{BASE}/mekleet/{quote(d.alt_query)}.html"
        return (f"🛒 Jauns vairs nav · līdzīgs jauns: {html.escape(d.alt_query)} no {d.alt_min:.0f} € → "
                f'<a href="{html.escape(url)}">cenueksperts</a>')
    return None


def gen_line(facts: Dict[str, Any]) -> Optional[str]:
    """📅 2021 · Intel 11. paaudze (Tiger Lake) · DDR4 — orientējoša gradācija."""
    parts = [str(facts["release_year"]) if facts.get("release_year") else None,
             facts.get("cpu_generation"), facts.get("ram_type")]
    parts = [re.sub(r"(\d+)\s*(?:st|nd|rd|th)\.?\s*(?:gen(?:eration)?|paaudze)", r"\1. paaudze", p, flags=re.I)
             for p in parts if p]
    return "📅 " + html.escape(" · ".join(parts)) if parts else None


def format_eval_line(facts: Dict[str, Any], d: Deal, po: Optional[PartOut] = None) -> str:
    """Īss vērtējums filtra ziņai, ja tas nav darījums."""
    e = html.escape
    why = []
    if d.new_min and d.price > d.new_min * MAX_NEW_RATIO:
        why.append(f"{d.price / d.new_min * 100:.0f}% no jaunas cenas ({d.new_min:.0f} €)")
    if d.margin < d.min_margin or d.profit < MIN_PROFIT:
        why.append(f"peļņa {d.profit:+.0f} € ({d.margin * 100:+.0f}%)")
    line = (f"📉 <b>Nav darījums</b>: pārdot ~{d.resale:.0f} € · " + e(", ".join(why)) +
            f" · pārliecība: {CONF_LV.get(d.confidence, d.confidence)}")
    if gen_line(facts):
        line = gen_line(facts) + "\n" + line
    if d.speed:
        line += "\n" + d.speed
    for h in hybrid_lines(d):
        line += "\n" + h
    if shop_line(d):
        line += "\n" + shop_line(d)
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
    keys = (("cpu", ""), ("gpu", "")) if composite else (("cpu", ""),)
    for key, suffix in keys + (("ram_gb", " GB RAM"), ("storage_gb", " GB")):
        v = facts.get(key)
        if v:
            specs.append(f"{v}{suffix}")
    specs.append(CONDITION_LV.get(facts["condition"], facts["condition"]))

    market = []
    if d.comps:
        market.append(comps_text(d))
    if d.new_min:
        market.append(f"jauns LV no {d.new_min:.0f} €")

    lines = [
        ("🔥" if d.hot else "💰") +
        f" <b>+{d.profit:.0f} € ({d.margin * 100:.0f}%)</b> · prasa {d.price:.0f} € → pārdot ~{d.resale:.0f} €",
        f"<b>{e(l.title[:120])}</b>",
        e(" · ".join(s for s in specs if s)),
    ]
    if gen_line(facts):
        lines.append(gen_line(facts))
    if market:
        lines.append("📊 " + e(" · ".join(market)))
    if d.speed:
        lines.append(d.speed)
    lines.extend(hybrid_lines(d))
    if shop_line(d):
        lines.append(shop_line(d))
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
