"""Datoru firmu / veikalu sludinājumu atpazīšana (bez AI).

ss.com nerāda pārdevēja identitāti, tāpēc vērtējam tekstu:
  1) frāzes ar svaru (veikals, mājas lapa, garantija N mēn., SIA, PVN, магазин ...)
  2) veidnes teksts — apraksta rinda, kas burtiski atkārtojas vairākos sludinājumos.
Punkti >= DEALER_THRESHOLD => firma.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, List, Optional, Tuple

from .parser import Listing

DEALER_THRESHOLD = 3
BOILERPLATE_MIN_LISTINGS = 2   # rinda jāatrod vismaz tik dažādos sludinājumos (2× = +2, 3+× = +3)
BOILERPLATE_MIN_LEN = 25       # īsākas rindas ("Cena: 100 €") neskaitām
BOILERPLATE_MIN_WORDS = 5      # teikums, nevis specifikācija ("cpu intel core i5 3.2 ghz")

# (regex, punkti, paskaidrojums)
RULES: List[Tuple[str, int, str]] = [
    # stipras — privātpersona tā gandrīz nekad neraksta
    (r"pārdod\s+veikals|^veikals\b|\bmūsu\s+veikal|\bveikalā\s+uz\s+vietas", 3, "veikals"),
    (r"datortirdzniecīb", 3, "datortirdzniecība"),
    (r"izvēlies\s+no\s+\d+|vairāk\s+piedāvājumu|plašs\s+(klāsts|piedāvājums)", 3, "sortiments"),
    # NB: domēnus (dateks.lv) un "rēķins" neskaitām — privātie raksta "pirkts dateks.lv, ir rēķins"
    (r"mājas\s*lap|\bwww\.|https?://", 3, "mājaslapa"),
    (r"\bsia\b|\bpvn\b|juridisk|bezskaidr", 3, "uzņēmums/PVN"),
    (r"магазин|в\s+наличии\s+(более|много)|наш[а-я]*\s+(ассортимент|сайт)", 3, "магазин"),
    (r"привоз|\bmēs\s+atrodamies|\bmūsu\s+(adrese|birojs|serviss|darbnīca)", 3, "veikala adrese/piegādes"),
    (r"мы\s+находимся|\bpārdodam\b|\bпродаём\b|\bпродаем\b", 2, "mēs pārdodam"),
    (r"\bpiedāvājam\b|\bmēs\s+piedāvājam|предлагаем", 2, "piedāvājam"),
    # vidējas
    (r"garantij[a-z]*\s*[-–:]?\s*\d+\s*(mēn|men|g\b|gad)|\d+\s*(mēn\.?|mēneš[a-z]*|g\.?|gad[a-z]*)\s*garantij",
     2, "garantija N mēn."),
    (r"prece\s+ar\s+garantiju|гарантия\s*\d+|\d+\s*(мес|год)[а-я.]*\s*гарант", 2, "garantija (veikala)"),
    (r"\bir\s+uz\s+vietas\b|piegāde\s+visā\s+latvijā|доставка\s+по", 2, "piegāde/uz vietas"),
    (r"\blīzing|\blizing|nomaksā|рассрочк", 2, "līzings"),
    # vājas — vienas pašas nepietiek
    (r"garantij|гаранти", 1, "garantija"),
]
_COMPILED = [(re.compile(p, re.I | re.M), pts, why) for p, pts, why in RULES]


def _norm_line(line: str) -> str:
    line = line.lower()
    line = re.sub(r"\d+", "#", line)          # "100+" un "1000+" = viena veidne
    return re.sub(r"[^\w#]+", " ", line).strip()


def description_lines(description: Optional[str]) -> List[str]:
    out = set()
    for line in (description or "").splitlines():
        n = _norm_line(line)
        words = [w for w in n.split() if len(w) >= 3 and "#" not in w]
        if len(n) >= BOILERPLATE_MIN_LEN and len(words) >= BOILERPLATE_MIN_WORDS:
            out.add(n)
    return list(out)


class Boilerplate:
    """Cik dažādos sludinājumos parādās katra apraksta rinda."""

    def __init__(self, descriptions: Iterable[Optional[str]] = ()):
        self.counts: Counter = Counter()
        for d in descriptions:
            self.add(d)

    def add(self, description: Optional[str]) -> None:
        self.counts.update(description_lines(description))

    def repeated(self, description: Optional[str]) -> List[Tuple[str, int]]:
        """Rindas, kas atrodamas vēl vismaz BOILERPLATE_MIN_LISTINGS-1 citos sludinājumos."""
        hits = []
        for line in description_lines(description):
            # self.counts var jau ietvert šo pašu sludinājumu; skaitām citus
            others = self.counts[line] - 1 if self.counts[line] else 0
            if others + 1 >= BOILERPLATE_MIN_LISTINGS:
                hits.append((line, others + 1))
        return hits


def classify(l: Listing, boilerplate: Optional[Boilerplate] = None) -> Tuple[int, List[str]]:
    text = "\n".join([l.title or "", l.description or ""])
    score, reasons = 0, []
    for rx, pts, why in _COMPILED:
        if why in reasons or (why == "garantija" and any(r.startswith("garantija") for r in reasons)):
            continue
        if rx.search(text):
            score += pts
            reasons.append(why)
    if l.category == "pc" and (l.key("condition") or "").lower().startswith("jaun"):
        score += 1
        reasons.append("jauns dators")
    if boilerplate is not None:
        rep = boilerplate.repeated(l.description)
        if rep:
            line, n = max(rep, key=lambda x: x[1])
            score += 3 if n >= 3 else 2
            reasons.append(f"veidne {n}× «{line[:40]}»")
    return score, reasons


def is_dealer(score: int) -> bool:
    return score >= DEALER_THRESHOLD
