"""Cenu ceļvedis: katrai komponentei/modelim — ss.com tirgus cena, cik ātri pārdodas,
jauna cena LV, ieteicamā maksimālā pirkuma cena un Edgara paša mērķa cena.

Grupēšana (guide_key) ir parasts kods, bez AI: no GPT jau izvilktā ai_json.
"""
from __future__ import annotations

import json
import logging
import re
import statistics
from datetime import timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import Column, DateTime, Float, Integer, String, Table, Text, bindparam, select, update

from . import db

log = logging.getLogger(__name__)

GUIDE_DAYS = 60            # cik senus sludinājumus ņemam vērā
MIN_MARGIN = 0.20          # Edgara noteikums: ≥ 20% peļņa
MAX_NEW_RATIO = 0.5        # Edgara noteikums: ≤ 50% no jaunas cenas
NEW_PRICE_MIN_LISTINGS = 2  # cenueksperts meklējam tikai grupām ar vismaz tik sludinājumiem

price_guide = Table(
    "price_guide", db.metadata,
    Column("guide_key", String(120), primary_key=True),
    Column("item_type", String(30)),
    Column("n", Integer, nullable=False, default=0),          # privāti sludinājumi periodā
    Column("n_active", Integer, nullable=False, default=0),
    Column("n_gone", Integer, nullable=False, default=0),
    Column("price_min", Float),
    Column("price_median", Float),
    Column("price_max", Float),
    Column("days_to_sell", Float),                              # mediāna pazudušajiem
    Column("new_min", Float),                                   # cenueksperts.lv
    Column("buy_max", Float),                                   # ieteicamā maks. pirkuma cena
    Column("cheapest_url", String(300)),                        # lētākais aktīvais
    Column("cheapest_price", Float),
    Column("updated_at", DateTime, nullable=False),
)

price_targets = Table(
    "price_targets", db.metadata,
    Column("guide_key", String(120), primary_key=True),
    Column("target_price", Float),                              # "pērku tikai līdz X €"
    Column("note", Text),
    Column("updated_at", DateTime, nullable=False),
)

# ---------------------------------------------------------------- grupēšana

GPU_RE = re.compile(r"\b(rtx|gtx|gt|rx|arc|quadro)\s*-?\s*([a-z]?\d{3,4}m?)\s*(ti|super|xtx|xt|gre)?\b")
CPU_RES = [
    re.compile(r"\b(i[3579])\s*-?\s*(\d{4,5}[a-z]{0,2})\b"),
    re.compile(r"\b(ryzen)\s*([3579])\s*(pro\s*)?(\d{4}[a-z0-9]{0,3})\b"),
    re.compile(r"\b(xeon)\s*(e[357]-?\s*\d{4}[a-z]?\s*(v\d)?|[wegs]-?\d{4,5}[a-z]?)\b"),
    re.compile(r"\b(core)\s*(ultra\s*[3579]\s*\d{3}[a-z]{0,2})\b"),
]


def _size_bucket(gb: float) -> str:
    for lo, hi, label in ((100, 140, "128GB"), (200, 280, "256GB"), (440, 560, "512GB"),
                          (900, 1100, "1TB"), (1800, 2200, "2TB"), (3600, 4400, "4TB")):
        if lo <= gb <= hi:
            return label
    return f"{int(gb)}GB" if gb < 1000 else f"{gb / 1000:g}TB"


def guide_key(facts: Dict[str, Any], text: str = "") -> Optional[str]:
    """Grupas atslēga no ai_json (+ sludinājuma teksts rezervei).
    None = nav jēgas ceļvedī (salikts dators, nezināms)."""
    t = facts.get("item_type")
    key = (facts.get("model_key") or "").lower()
    if not key or "unknown" in key or t in ("desktop", "bundle", "other", None):
        return None

    if t == "gpu":
        m = GPU_RE.search(key.replace("geforce", ""))
        if not m:
            return None
        mem = re.search(r"(\d{1,2})\s*gb", key)
        name = " ".join(x for x in (m.group(1), m.group(2), m.group(3)) if x).upper()
        return f"{name} {mem.group(1)}GB" if mem else name

    if t == "cpu":
        for rx in CPU_RES:
            m = rx.search(key)
            if m:
                return re.sub(r"\s+", " ", " ".join(g for g in m.groups() if g)).strip().title() \
                    .replace("I3 ", "i3-").replace("I5 ", "i5-").replace("I7 ", "i7-").replace("I9 ", "i9-")
        return key[:60]

    if t == "ram":
        return ram_key(facts, key, text)

    if t == "storage":
        size = facts.get("storage_gb")
        if not size:
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*(tb|gb|t|g)\b", key)
            if m:
                size = float(m.group(1).replace(",", ".")) * (1000 if m.group(2).startswith("t") else 1)
        if not size:
            return None
        kind = "NVMe" if re.search(r"nvme|m\.?2|pcie|gen\s?[345]|nv[1-3]\b|sn\d{3,4}|\b9[789]0\b|crucial\s*p\d{1,3}\b|"
                                  r"kc3000|a2000|t500|t700|mp600|fury renegade|xpg", key) else \
            "SSD" if re.search(r"ssd|a400|mx500|870|860|sa400|bx500", key) else "HDD"
        return f"{kind} {_size_bucket(float(size))}"

    if t == "laptop":
        return laptop_family(key)

    # plates, barošanas bloki, korpusi, dzesētāji: GPT modelis kā ir
    return key[:80]


def laptop_family(key: str) -> Optional[str]:
    """'dell latitude 5520 + intel core i7-1165g7 + 16gb' / 'dell latitude 5520 i5-1135g7 8gb' -> 'dell latitude 5520'"""
    k = key.split("+")[0]
    k = re.split(r"\b(?:intel|amd|core|i[3579][\s-]?\d|ryzen|celeron|pentium|\d+\s?gb|\d+\s?tb|ssd|nvme|hdd|"
                 r"rtx|gtx|radeon|win(?:dows)?|\d{2}[.,]?\d?\s?(?:\"|''|inch|collu))", k)[0]
    k = re.sub(r"\bgen\s*(\d)", r"gen \1", k)                 # gen2 -> gen 2
    k = re.sub(r"\b\d{2}[a-z0-9]{6,}\b", "", k)              # Lenovo MTM kods 20wns1q70w
    k = re.sub(r"\s+", " ", re.sub(r"[\s,(/-]+$", "", k)).strip()
    return k[:80] or None


# ---------------------------------------------------------------- portatīvo klase
# Ja precīzā modeļa salīdzinājumu nav, salīdzinām ar līdzvērtīgiem portatīvajiem:
# CPU paaudze + tips (H jaudīgais / U ekonomiskais) + videokarte + RAM.
# Paaudze nosaka arī RAM tipu (DDR3/4/5) un mātesplates laikmetu, tāpēc atsevišķi to neliekam.

_GPU_HIGH = re.compile(r"\b(gtx\s*10[78]0|rtx\s*(20[6-8]0|30[6-8]0|40[5-9]0|50[5-9]0)|rx\s*[5-7]\d00m?)\b")
_GPU_MID = re.compile(r"\b(gtx\s*1050\s*ti|gtx\s*1060|gtx\s*16[56]0|rtx\s*(2050|3050)|"
                      r"rx\s*(560|570|580|5[35]00m)|quadro\s*(t|p)\s*(1000|2000))\b")
_DGPU = re.compile(r"\b(gtx|rtx|mx\s*\d{3}|quadro|radeon\s*(r[579]|rx|pro|\d{3})|rx\s*\d{3,4}|geforce\s*\d{3}m|"
                   r"9[2-8]0m|8[2-6]0m|arc\s*a\d{3})")


def _cpu_class(cpu: str) -> Optional[tuple]:
    """-> (ražotājs, paaudze, tips H/U, i3/r3?) vai ("low-end", 0, "", False) vai None"""
    c = cpu.lower()
    if re.search(r"\b(apple|m[1-5]\b)", c):
        return None  # MacBook — tiem pietiek modeļa salīdzinājumu
    if re.search(r"celeron|pentium|atom|athlon|\ba[4-9]-\d|e[12]-\d{3,4}|\bn\d{3,4}\b", c):
        return ("low-end", 0, "", False)
    m = re.search(r"(?:ultra|core)\s*([3579])\s*(\d)\d{2}([a-z]*)", c)
    if m:  # Core Ultra 7 155H (s1 ≈ 14. paaudze), Core 5 120U
        return ("intel", 13 + int(m.group(2)), "H" if "h" in m.group(3) else "U", False)
    m = re.search(r"\bi([3579])\s*-?\s*(\d{3,5})([a-z]{0,2}\d?)", c)
    if m:
        d = m.group(2)
        # i5-430M (1.), i7-6700HQ (6.), i5-1135G7 (11.), i7-10750H (10.)
        gen = 1 if len(d) == 3 else int(d[:2]) if len(d) == 5 or d[0] == "1" else int(d[0])
        return ("intel", gen, "H" if re.match(r"h", m.group(3)) else "U", m.group(1) == "3")
    m = re.search(r"ryzen\s*([3579])\s*(?:pro\s*)?(\d)\d{3}([a-z]*)", c)
    if m:
        return ("ryzen", int(m.group(2)), "H" if re.match(r"h", m.group(3)) else "U", m.group(1) == "3")
    return None


def gpu_tier(gpu: str) -> str:
    g = gpu.lower()
    if _GPU_HIGH.search(g):
        return "dGPU augsta"
    if _GPU_MID.search(g):
        return "dGPU vidēja"
    if _DGPU.search(g) and not re.search(r"\b(vega|radeon graphics|uhd|iris|hd graphics)\b", g.split("+")[0]):
        return "dGPU vāja"
    return "iGPU"


def laptop_class(facts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Portatīvā klase trijos līmeņos:
    fine  'intel 6.paaudze H · gtx 960m · 16GB'
    mid   'intel 6.paaudze H · dGPU vāja'
    spec  ("intel", 6, "H", False, "dGPU vāja") — plašākajam: ±1 paaudze"""
    cpu = _cpu_class(" ".join(str(facts.get(k) or "") for k in ("cpu", "cpu_generation", "model_key")))
    if not cpu:
        return None
    vendor, gen, seg, small = cpu
    if vendor == "low-end":
        name = "low-end cpu"
    else:
        name = f"{vendor} {gen}.paaudze {seg}" if vendor == "intel" else f"ryzen {gen}000 {seg}"
        name += (" i3" if vendor == "intel" else " r3") if small else ""
    gpu_raw = (facts.get("gpu") or "").lower()
    tier = gpu_tier(gpu_raw)
    m = GPU_RE.search(gpu_raw) or re.search(r"\b(mx)\s*(\d{3})\b", gpu_raw)
    gpu = tier
    if m and tier != "iGPU":
        gpu = f"{m.group(1)} {m.group(2)}" + (f" {m.group(3)}" if m.lastindex >= 3 and m.group(3) else "")
    ram = facts.get("ram_gb") or 0
    ram_b = "≤8GB" if ram <= 8 else "16GB" if ram <= 16 else "32GB+"
    return {"fine": f"{name} · {gpu} · {ram_b}", "mid": f"{name} · {tier}",
            "spec": (vendor, gen, seg, small, tier)}


def same_broad_class(a: tuple, b: tuple) -> bool:
    """Tas pats ražotājs, tips, videokartes jauda; paaudze ±1."""
    return (a[0] == b[0] != "low-end" and a[2] == b[2] and a[3] == b[3] and a[4] == b[4]
            and abs(a[1] - b[1]) <= 1)


def broad_label(spec: tuple) -> str:
    vendor, gen, seg, small, tier = spec
    gens = f"{gen - 1}.–{gen + 1}." if vendor == "intel" else f"{gen - 1}000–{gen + 1}000"
    return f"{vendor} {gens} paaudze {seg} · {tier}"


# RAM: "2x8gb", "8gbx2", "2 pa 8gb", "2 × 8 GB", "две планки по 8", "4gb x 4"
_KIT_RES = [
    re.compile(r"(\d)\s*[x×хх*]\s*(\d{1,3})\s*(?:gb|гб)"),
    re.compile(r"(\d{1,3})\s*(?:gb|гб)\s*[x×х*]\s*(\d)\b"),
    re.compile(r"(\d)\s*(?:pa|по)\s*(\d{1,3})\s*(?:gb|гб)"),
]
_WORD_COUNT = {"две": 2, "два": 2, "divi": 2, "divas": 2, "четыре": 4, "četri": 4, "četras": 4}


def ram_key(facts: Dict[str, Any], key: str, text: str = "") -> Optional[str]:
    """'DDR4 16GB (2×8GB)', 'DDR4 16GB (1×16GB)', 'DDR3 8GB SO-DIMM (2×4GB)', 'DDR4 32GB ECC (1×32GB)'.
    Ja moduļi nav zināmi: 'DDR4 16GB (?)'. 2×8 un 1×16 ir dažādas preces ar dažādu cenu."""
    low = f"{key} {text}".lower()
    ddr = re.search(r"ddr\s*([2-5])|ddr\s*(ii|iii)", low)
    if not ddr:
        return None
    gen = ddr.group(1) or {"ii": "2", "iii": "3"}[ddr.group(2)]

    count, size = facts.get("ram_module_count"), facts.get("ram_module_gb")
    if not (count and size):
        for i, rx in enumerate(_KIT_RES):
            m = rx.search(low)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                count, size = (b, a) if i == 1 else (a, b)
                break
    if not (count and size):
        m = re.search(r"(две|два|divi|divas|четыре|četri|četras)\s+(?:планк\w*|плашк\w*|plat\w*|modu\w*|stik\w*)\s*(?:по\s*)?(\d{1,3})", low)
        if m:
            count, size = _WORD_COUNT[m.group(1)], int(m.group(2))
    total = facts.get("ram_gb")
    if not (count and size) and re.search(r"одной\s+(?:планк|плашк)|vien[aā]\s+(?:plat|modu|stik)|\b1\.?\s*x\s*\d|\(x\s*1\)|\bx\s*1\b", low):
        m = re.search(r"(\d{1,3})\s*(?:gb|гб)", low)
        if m:
            count, size = 1, int(m.group(1))
    if facts.get("price_per_module") and size:
        count = 1  # cena par vienu moduli -> vērtējam kā vienu moduli
    if count and size:
        total = count * size
    if not total:
        m = re.search(r"(\d{1,3})\s*gb", key)
        total = int(m.group(1)) if m else None
    if not total:
        return None

    tags = ""
    no_ecc = re.sub(r"non[\s-]?ecc|bez\s+ecc|без\s+ecc", "", low)
    if facts.get("ram_ecc") or re.search(r"\becc\b|rdimm|registered|\breg\b", no_ecc):
        tags += " ECC"
    if facts.get("ram_sodimm") or re.search(r"so-?dimm|sodimm|laptop|portat|ноутбук", low):
        tags += " SO-DIMM"
    modules = f"{count}×{size}GB" if count and size else "?"
    return f"DDR{gen} {total}GB{tags} ({modules})"


# ---------------------------------------------------------------- aprēķins

def refresh(store: db.Store, prices=None, max_lookups: int = 25) -> int:
    """Pārrēķina visu ceļvedi. prices: cenueksperts.PriceLookup (jaunām cenām) vai None.
    max_lookups: cik jaunus cenueksperts meklējumus drīkst šoreiz (pārējie — nākamajās reizēs)."""
    seed_targets(store)
    L = db.listings
    since = db.now() - timedelta(days=GUIDE_DAYS)
    with store.engine.connect() as c:
        rows = c.execute(select(L.c.ss_id, L.c.ai_json, L.c.price, L.c.status, L.c.first_seen, L.c.description,
                                L.c.last_seen, L.c.disappeared_at, L.c.url, L.c.guide_key)
                         .where(L.c.is_dealer == 0, L.c.ai_json.isnot(None), L.c.price.isnot(None),
                                L.c.first_seen >= since, L.c.status.in_(["active", "disappeared"]))).all()

    groups: Dict[str, List] = {}
    key_updates = []
    for r in rows:
        facts = json.loads(r.ai_json)
        if facts.get("condition") in ("broken", "for_parts"):
            continue
        k = guide_key(facts, r.description or "")
        if k != r.guide_key:
            key_updates.append({"_id": r.ss_id, "_k": k})
        if k:
            groups.setdefault(k, []).append((r, facts.get("item_type")))

    if key_updates:
        with store.engine.begin() as c:
            stmt = update(L).where(L.c.ss_id == bindparam("_id")).values(guide_key=bindparam("_k"))
            c.execute(stmt, key_updates)

    now = db.now()
    out = []
    for k, items in groups.items():
        prices_ = sorted(r.price for r, _ in items)
        med = statistics.median(prices_)
        # izmetam acīmredzamas kļūdas (1 € / 9999 €), ja ir no kā spriest
        if len(prices_) >= 3:
            prices_ = [p for p in prices_ if med * 0.3 <= p <= med * 3] or prices_
            med = statistics.median(prices_)
        gone = [r for r, _ in items if r.status == "disappeared"]
        active = [r for r, _ in items if r.status == "active"]
        days = [((r.disappeared_at or r.last_seen) - r.first_seen).total_seconds() / 86400 for r in gone]
        cheapest = min(active, key=lambda r: r.price) if active else None

        new_min = None
        # Diskiem "NVMe 512GB" ir pārāk vispārīgs — cenueksperts atrod korpusus/adapterus, ne diskus
        if prices is not None and len(items) >= NEW_PRICE_MIN_LISTINGS and items[0][1] not in ("laptop", "storage"):
            try:
                cached = prices.is_cached(k)
                if not cached:
                    if max_lookups <= 0:
                        raise LookupError("limits")
                    max_lookups -= 1
                np_ = prices.get(k)
                new_min = np_.min_price if np_ and np_.n >= 2 else None
            except LookupError:
                pass  # meklēsim nākamajā atjaunošanā
            except Exception as e:  # cenueksperts kļūda nedrīkst apturēt ceļvedi
                log.warning("cenueksperts '%s': %s", k, e)

        buy_max = med / (1 + MIN_MARGIN)
        if new_min:
            buy_max = min(buy_max, new_min * MAX_NEW_RATIO)
        out.append(dict(
            guide_key=k, item_type=items[0][1], n=len(items), n_active=len(active), n_gone=len(gone),
            price_min=prices_[0], price_median=round(med, 2), price_max=prices_[-1],
            days_to_sell=round(statistics.median(days), 1) if days else None,
            new_min=new_min, buy_max=round(buy_max), updated_at=now,
            cheapest_url=cheapest.url if cheapest else None,
            cheapest_price=cheapest.price if cheapest else None,
        ))

    with store.engine.begin() as c:
        c.execute(price_guide.delete())
        if out:
            c.execute(price_guide.insert(), out)
    log.info("Cenu ceļvedis: %d grupas no %d sludinājumiem", len(out), len(rows))
    return len(out)


def target_for(store: db.Store, key: Optional[str]) -> Optional[float]:
    if not key:
        return None
    with store.engine.connect() as c:
        return c.execute(select(price_targets.c.target_price)
                         .where(price_targets.c.guide_key == key)).scalar()


def set_target(store: db.Store, key: str, price: Optional[float], note: Optional[str] = None) -> None:
    with store.engine.begin() as c:
        c.execute(price_targets.delete().where(price_targets.c.guide_key == key))
        if price is not None or note:
            c.execute(price_targets.insert().values(guide_key=key, target_price=price, note=note,
                                                    updated_at=db.now()))


def seed_targets(store: db.Store) -> int:
    """filters.json -> "targets": [{"guide_key": "NVMe 512GB", "max_price": 45}, ...]
    Ieliek tikai tās mērķa cenas, kuru vēl nav (web lapā mainītās nepārraksta)."""
    from .filters import FILTERS_FILE
    try:
        wanted = json.loads(FILTERS_FILE.read_text(encoding="utf-8")).get("targets", [])
    except (OSError, ValueError):
        return 0
    added = 0
    for t in wanted:
        key, price = t.get("guide_key"), t.get("max_price")
        if key and price is not None and target_for(store, key) is None:
            set_target(store, key, float(price), t.get("note"))
            added += 1
    if added:
        log.info("Mērķa cenas no filters.json: %d", added)
    return added

