"""Portatīvo cenu statistiskais modelis (ML "otrais viedoklis" blakus GPT).

Mācās no mūsu pašu datubāzes: privāti portatīvie, kas pazuduši (≈ pārdoti).
Pazīmes ir no jau izvilktā ai_json + sludinājuma teksta (regex), bez papildu GPT izsaukumiem.

Izpēte (2026-11-25, 503 svaigi sludinājumi): GPT vien atzīmēja 55, no tiem 36% pazuda ≤3 d;
GPT + ML vidējais ar ≥30% slieksni — 39%. Klase viena pati — tikai 17% (sliktāk par vidējo 24%).

Vērtēšana "pēc remonta": modelim prasām cenu standarta stāvoklī (SSD, strādājoša baterija,
lādētājs) un atskaitām remonta izmaksas — jo bateriju un disku Edgars nomaina pats.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from . import db

log = logging.getLogger(__name__)

RETRAIN_DAYS = 7
MIN_TRAIN = 150
# remonta izmaksas (Edgara cenas LV, 2026-11)
REPAIR_COST = {"baterija": 25.0, "SSD 256GB": 22.0, "lādētājs": 15.0}

_BAT = r"(baterij|akumulator|батаре|аккумулятор|battery|akku)"
_BAD = (r"(netur|ne tur|beigt|nestr|vāj|nedarb|не держ|дохл|мертв|не работ|слаб|dead|bad|jāmaina|замен|"
        r"изношен|min(ūt|ut)|мин|nav\b|нет\b|0%|uzbriedus|вздут|izņemt|снят)")
_BUSINESS = r"thinkpad|latitude|elitebook|probook|precision|zbook|travelmate|expertbook|vostro|dynabook|toughbook|lifebook"
_GAMING = (r"\brog\b|\btuf\b|legion|omen|predator|nitro|alienware|victus|\bloq\b|aorus|katana|sword|cyborg|\bgf6[35]|"
           r"\bgf75|\bgl6[235]|\bgl7[2-5]|\bge[67]\d|\bgp[67]\d|\bgs[67]\d|strix|scar|zephyrus|helios|odyssey|"
           r"\by[57]\d0|ideapad gaming|thin gf|bravo|delta|pulse|vector|raider|stealth")


def _has(rx: str, t: str) -> bool:
    return bool(re.search(rx, t, re.I))


def listing_text(title: str, description: Optional[str], params: Dict[str, Any]) -> str:
    # "HDD apjoms, Gb" ir ss.com lauka nosaukums visiem — nav pazīme, ka diskā ir HDD
    return " ".join([title or "", description or "",
                     " ".join(str(v) for k, v in (params or {}).items() if not k.startswith("HDD apjoms"))]).lower()


def condition_flags(facts: Dict[str, Any], text: str) -> Dict[str, Any]:
    """Maināmās detaļas: disks, baterija, lādētājs."""
    defects = (facts.get("defects") or "").lower()
    ssd = _has(r"\bssd\b|nvme|m\.2|ссд", text)
    hdd = _has(r"\bhdd\b|hard ?dis|жестк|винчест|cietais disks?|\b5400|\b7200", text)
    nodisk = _has(r"bez (diska|hdd|ssd)|без (диска|hdd|ssd|жест)|no (hdd|ssd|disk)|nav diska|диска нет", text)
    apple = _has(r"apple|macbook", (facts.get("model_key") or "") + " " + text[:200])
    return {
        "storage": "none" if nodisk else "ssd" if (ssd or apple) else "hdd" if hdd else "unk",
        "bat_bad": int(bool(re.search(_BAT + r".{0,40}" + _BAD, text + " " + defects, re.I)
                            or re.search(_BAD + r".{0,25}" + _BAT, text, re.I) or _has(_BAT, defects))),
        "no_charger": int(_has(r"bez (lādētāja|ladetaja|lādētājs|barošanas)|без (заряд|блока)|no charger|"
                               r"lādētājs? nav|зарядки нет|без зу", text)),
    }


def features(facts: Dict[str, Any], text: str, size_param: Optional[str] = None) -> Dict[str, Any]:
    from . import guide
    f: Dict[str, Any] = {}
    f.update(condition_flags(facts, text))
    t = re.sub(r"uhd\s*(graphics|\d{3})|intel uhd", " ", text)
    f["damaged"] = int(_has(r"plīs|plais|salauz|трещ|разбит|crack|eņģ|петл|битый|bojāt|ekrāns nestr",
                            t + " " + (facts.get("defects") or "").lower()))
    f["res"] = ("4k" if _has(r"\b(3840|4k|uhd)\b", t) else "qhd" if _has(r"\b(2560|1440p?|qhd|2k|2\.5k|1600p)\b", t)
                else "fhd" if _has(r"\b(1920|1080p?|fhd|full ?hd|1200p)\b", t)
                else "low" if _has(r"\b(1366|768|hd ready|1600x900|900p)\b", t) else "unk")
    f["hz_high"] = int(_has(r"\b(120|144|165|240|300|360)\s?(hz|гц|herc)", t))
    m = re.search(r"(1[0-8](?:[.,]\d)?)\s?(?:\"|''|”|″|inch|collu|col\b|дюйм|in\b)", t)
    try:
        size = float(m.group(1).replace(",", ".")) if m else float(re.sub(r"[^\d.]", "", size_param or "") or "nan")
    except ValueError:
        size = float("nan")
    f["size"] = min(max(size, 10), 18.5) if size == size else 15.6
    key = (facts.get("model_key") or "") + " " + text[:200]
    f["line"] = ("apple" if _has(r"apple|macbook", key) else "business" if _has(_BUSINESS, key)
                 else "gaming" if _has(_GAMING, key) else "consumer")
    cls = guide.laptop_class(facts)
    vendor, gen, seg, small, tier = cls["spec"] if cls else (None, 0, None, False, guide.gpu_tier(facts.get("gpu") or ""))
    f.update(vendor=vendor or "na", seg=seg or "na", gtier=tier, gen=float(gen or 0), small=float(bool(small)),
             gen_intel=float(gen or 0) if vendor == "intel" else 0.0,
             gen_ryzen=float(gen or 0) if vendor == "ryzen" else 0.0)
    f["ram_log"] = math.log2(min(max(facts.get("ram_gb") or 8, 2), 64))
    f["sgb_log"] = math.log2(min(max(facts.get("storage_gb") or 256, 16), 4000))
    f["year"] = float(facts.get("release_year") or 2018)
    f["year_unk"] = float(not facts.get("release_year"))
    mc = re.search(r"\bm([1-5])\b", ((facts.get("cpu") or "") + " " + key).lower())
    f["mac_chip"] = float(mc.group(1)) if mc else 0.0
    f["cond"] = facts.get("condition") or "used"
    return f


CATS = ("vendor", "seg", "gtier", "storage", "res", "line", "cond")


def _vec(f: Dict[str, Any]) -> Dict[str, float]:
    """kategorijas -> one-hot (DictVectorizer formāts)"""
    return {(f"{k}={v}" if k in CATS else k): (1.0 if k in CATS else float(v)) for k, v in f.items()}


class LaptopModel:
    def __init__(self, store: db.Store):
        self.store = store
        self.model = None
        self.vec = None
        self.trained_at = None
        self.n_train = 0
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        try:
            import sklearn  # noqa: F401
            return True
        except ImportError:
            return False

    def _stale(self) -> bool:
        return self.trained_at is None or db.now() - self.trained_at > timedelta(days=RETRAIN_DAYS)

    def train(self) -> bool:
        import numpy as np
        from sklearn.ensemble import HistGradientBoostingRegressor
        from sklearn.feature_extraction import DictVectorizer
        L = db.listings
        with self.store.engine.connect() as c:
            rows = c.execute(select(L.c.price, L.c.ai_json, L.c.title, L.c.description, L.c.params_json)
                             .where(L.c.item_type == "laptop", L.c.is_dealer == 0, L.c.status == "disappeared",
                                    L.c.price.between(30, 3000), L.c.ai_json.isnot(None))).all()
        X, y = [], []
        for r in rows:
            facts = json.loads(r.ai_json)
            if facts.get("condition") in ("broken", "for_parts"):
                continue
            params = json.loads(r.params_json or "{}")
            X.append(_vec(features(facts, listing_text(r.title, r.description, params), params.get("Displejs"))))
            y.append(math.log(r.price))
        if len(X) < MIN_TRAIN:
            log.info("Portatīvo ML: par maz datu (%d)", len(X))
            self.trained_at = db.now()  # nemēģinām katrā sludinājumā no jauna
            return False
        vec = DictVectorizer(sparse=False)
        model = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, min_samples_leaf=10, random_state=1)
        model.fit(vec.fit_transform(X), np.array(y))
        self.model, self.vec, self.n_train, self.trained_at = model, vec, len(X), db.now()
        log.info("Portatīvo ML apmācīts uz %d pārdotiem", len(X))
        return True

    def _predict(self, f: Dict[str, Any]) -> float:
        return float(math.exp(self.model.predict(self.vec.transform([_vec(f)]))[0]))

    def value(self, facts: Dict[str, Any], title: str, description: Optional[str],
              params: Dict[str, Any]) -> Optional[Tuple[float, List[Tuple[str, float]], float]]:
        """-> (vērtība, remonti [(nosaukums, €)], cena kā ir) vai None.
        vērtība = labākais no: pārdot kā ir / salabot (SSD, baterija, lādētājs) un pārdot."""
        if not self.available:
            return None
        with self._lock:
            if self._stale():
                try:
                    self.train()
                except Exception:
                    log.exception("Portatīvo ML apmācība neizdevās")
                    self.trained_at = db.now()
        if self.model is None:
            return None
        f = features(facts, listing_text(title, description, params), (params or {}).get("Displejs"))
        as_is = self._predict(f)
        repairs = []
        if f["bat_bad"]:
            repairs.append(("baterija", REPAIR_COST["baterija"]))
        if f["storage"] in ("hdd", "none"):
            repairs.append(("SSD 256GB", REPAIR_COST["SSD 256GB"]))
        if f["no_charger"]:
            repairs.append(("lādētājs", REPAIR_COST["lādētājs"]))
        if not repairs:
            return as_is, [], as_is
        fixed = self._predict(dict(f, bat_bad=0, no_charger=0,
                                   storage="ssd" if f["storage"] in ("hdd", "none") else f["storage"]))
        after = fixed - sum(c for _, c in repairs)
        return (after, repairs, as_is) if after > as_is else (as_is, [], as_is)
