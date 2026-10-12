"""ss.com lapu parseri. Neviena funkcija neizmet izņēmumu par trūkstošu lauku:
trūkstošais lauks paliek None, un žurnālā tiek ierakstīts brīdinājums."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional
from xml.etree import ElementTree

from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node

from . import config

log = logging.getLogger(__name__)

# ss.com lauku nosaukumi -> mūsu kanoniskie nosaukumi
FIELD_MAP = {
    "marka": "brand",
    "modelis": "model",
    "displejs": "screen",
    "ekrāns": "screen",
    "procesors": "cpu",
    "cpu": "cpu",
    "ram": "ram",
    "operatīvā atmiņa": "ram",
    "hdd": "disk",
    "hdd apjoms": "disk",
    "cietais disks": "disk",
    "disks": "disk",
    "videokarte": "gpu",
    "video": "gpu",
    "ražotājs": "brand",
    "stāvoklis": "condition",
    "cena": "price",
}

KEY_FIELDS = ("subcategory", "brand", "model", "cpu", "ram", "disk", "gpu", "screen", "condition")


@dataclass
class Listing:
    ss_id: Optional[str]
    category: str
    url: str
    title: str = ""
    city: Optional[str] = None
    price: Optional[float] = None
    params: Dict[str, str] = field(default_factory=dict)  # visi lauki kā ss.com tos rāda
    # aizpildās tikai no sludinājuma lapas / RSS
    description: Optional[str] = None
    posted_at: Optional[datetime] = None
    photos: List[str] = field(default_factory=list)
    raw_block: Optional[str] = None
    # firmas/veikala vērtējums (dealer.classify)
    dealer_score: int = 0
    dealer_reasons: List[str] = field(default_factory=list)

    @property
    def is_dealer(self) -> bool:
        from .dealer import is_dealer
        return is_dealer(self.dealer_score)

    @property
    def subcategory(self) -> Optional[str]:
        """completing-pc apakšsadaļa no saites: .../completing-pc/videocards/xx.html -> videocards"""
        m = re.search(r"/computers/[^/]+/([^/]+)/[^/]+\.html$", self.url)
        return m.group(1) if m else None

    def key(self, name: str) -> Optional[str]:
        """Kanoniskā lauka vērtība (brand, cpu, ram, ...)."""
        if name == "subcategory":
            return self.subcategory
        for label, value in self.params.items():
            # "Operatīvā atmiņa, Gb" -> "operatīvā atmiņa"
            if FIELD_MAP.get(label.split(",")[0].strip().lower()) == name:
                return value
        return None


def _text(node: Optional[Node]) -> str:
    if node is None:
        return ""
    # <br> -> atstarpe, lai "Lenovo<br>ThinkPad" nesaplūst
    html = node.html or ""
    html = re.sub(r"<br\s*/?>", " ", html, flags=re.I)
    return re.sub(r"\s+", " ", HTMLParser(html).text()).strip()


def parse_price(text: str) -> Optional[float]:
    """'1 250 €' -> 1250.0. 'maiņai', 'pērku', 'runājama' -> None."""
    if not text or "€" not in text:
        return None
    m = re.search(r"(\d[\d\s,.]*)\s*€", text)
    if not m:
        return None
    num = re.sub(r"\s", "", m.group(1)).rstrip(".,")
    # ss.com: "1,800 €" = 1800 (komats tūkstošiem); "12.50 €" = 12.5
    num = re.sub(r",(?=\d{3}(?:\D|$))", "", num).replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return None


def _abs(url: str) -> str:
    return url if url.startswith("http") else config.BASE_URL + url


# ---------------------------------------------------------------- saraksts

def parse_list_page(html: str, category: str) -> List[Listing]:
    tree = HTMLParser(html)
    head = tree.css_first("tr#head_line")
    if head is None:
        log.warning("[%s] sarakstā nav galvenes (head_line) — struktūra mainījusies?", category)
        return []
    # Pirmās kolonnas (checkbox, foto, teksts) apvienotas vienā colspan=3 šūnā
    columns = [_text(td) for td in head.css("td.msg_column_td")]

    out: List[Listing] = []
    for tr in tree.css('tr[id^="tr_"]'):
        tr_id = tr.attributes.get("id", "")
        if not re.fullmatch(r"tr_\d+", tr_id):
            continue  # reklāmas un citas rindas
        try:
            out.append(_parse_row(tr, tr_id[3:], columns, category))
        except Exception as e:  # neļaujam vienai rindai nogāzt visu lapu
            log.warning("[%s] rindu %s neizdevās parsēt: %s", category, tr_id, e)
    if not out:
        log.warning("[%s] sarakstā nav atrasts neviens sludinājums", category)
    return out


def _parse_row(tr: Node, ss_id: str, columns: List[str], category: str) -> Listing:
    link = tr.css_first("a.am")
    url = _abs(link.attributes.get("href", "")) if link else ""
    region = tr.css_first(".ads_region")
    cells = tr.css("td.msga2-o")

    params: Dict[str, str] = {}
    price = None
    for name, td in zip(columns, cells):
        value = _text(td)
        if name.lower() == "cena":
            price = parse_price(value)
        elif value and value != "-":
            params[name] = value
    if len(cells) != len(columns):
        log.warning("[%s] %s: %d kolonnas, bet %d šūnas", category, ss_id, len(columns), len(cells))

    return Listing(
        ss_id=ss_id,
        category=category,
        url=url,
        title=_text(link),
        city=_text(region) or None,
        price=price,
        params=params,
    )


def last_page_number(html: str) -> int:
    """1. lapā saite rel=prev rāda uz pēdējo lapu."""
    tree = HTMLParser(html)
    nums = [1]
    for a in tree.css("a.navi"):
        m = re.search(r"/page(\d+)\.html", a.attributes.get("href", ""))
        if m:
            nums.append(int(m.group(1)))
    return max(nums)


# ---------------------------------------------------------------- RSS

def parse_rss(xml: str, category: str) -> List[Listing]:
    try:
        root = ElementTree.fromstring(xml.encode("utf-8"))
    except ElementTree.ParseError as e:
        log.warning("[%s] RSS neizdevās parsēt: %s", category, e)
        return []

    out: List[Listing] = []
    for item in root.iter("item"):
        url = (item.findtext("link") or "").strip()
        desc_html = item.findtext("description") or ""
        params: Dict[str, str] = {}
        price = None
        # "Marka: <b>Apple<br>Macbook Pro</b><br/>RAM: <b>16</b>..."
        for label, value in re.findall(r"([^<>:]+):\s*<b>(.*?)</b>", desc_html, re.S):
            label = label.strip()
            value = _text(HTMLParser(value).body)
            if value and label.lower() != "cena":
                params[label] = value
        # "Cena: <b>650</b>  €" — € reizēm ir ārpus <b>
        m = re.search(r"Cena:(.*?)(?:<br|$)", desc_html, re.S)
        if m:
            price = parse_price(_text(HTMLParser(m.group(1)).body))
        posted = None
        pub = item.findtext("pubDate")
        if pub:
            try:
                posted = parsedate_to_datetime(pub)
            except (TypeError, ValueError):
                log.warning("[%s] nesaprotams pubDate: %s", category, pub)
        out.append(Listing(
            ss_id=None,  # RSS satur tikai saiti; ID iegūst no sludinājuma lapas
            category=category,
            url=url,
            title=re.sub(r"\s+", " ", item.findtext("title") or "").strip(),
            price=price,
            params=params,
            posted_at=posted,
        ))
    return out


# ---------------------------------------------------------------- sludinājums

def parse_detail(html: str, url: str, category: str) -> Optional[Listing]:
    tree = HTMLParser(html)
    block = tree.css_first("#msg_div_msg")
    if block is None:
        log.warning("[%s] %s: nav #msg_div_msg (dzēsts vai mainīta struktūra)", category, url)
        return None

    ss_id = None
    m = re.search(r"\baf\('(\d+)'", html) or re.search(r'name="mid\[\]" value="(\d+)', html)
    if m:
        ss_id = m.group(1)
    else:
        log.warning("[%s] %s: neatradu ss_id", category, url)

    params: Dict[str, str] = {}
    for name_td in block.css("td.ads_opt_name"):
        value_td = name_td.next
        while value_td is not None and value_td.tag != "td":
            value_td = value_td.next
        label = _text(name_td).rstrip(":").strip()
        value = _text(value_td)
        # ss.com pievieno saiti "[Karte]" pie adreses u.tml.
        value = re.sub(r"\s*\[[^\]]*\]\s*$", "", value)
        if label and value:
            params[label] = value

    price_td = block.css_first("td.ads_price")
    price = parse_price(_text(price_td))

    # Apraksts = bloka teksts bez parametru tabulas un sistēmas div
    desc_tree = HTMLParser(block.html)
    # Apraksts ir vienkāršs teksts ar <br>; visas tabulas/div ir parametri, cena, foto
    for sel in ("table", "#content_sys_div_msg", "#calc_td", "#tr_foto", "script", "iframe"):
        for n in desc_tree.css(sel):
            n.decompose()
    desc_html = re.sub(r"<br\s*/?>", "\n", desc_tree.body.html if desc_tree.body else "", flags=re.I)
    description = "\n".join(
        line.strip() for line in HTMLParser(desc_html).text().splitlines() if line.strip()
    )

    posted = None
    m = re.search(r"Datums:\s*(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})", html)
    if m:
        posted = datetime.strptime(m.group(1), "%d.%m.%Y %H:%M")
    else:
        log.warning("[%s] %s: nav ievietošanas datuma", category, url)

    photos = [
        a.attributes["href"]
        for a in tree.css(".pic_dv_thumbnail a")
        if a.attributes.get("href", "").startswith("http")
    ]

    title = description.split("\n", 1)[0][:200] if description else ""
    city = params.get("Pilsēta") or params.get("Pilsēta, rajons") or params.get("Vieta")
    if not city:
        # kontaktu blokā: <td class="ads_contacts_name">Vieta:</td><td class="ads_contacts">Rīga</td>
        for td in tree.css("td.ads_contacts_name"):
            if _text(td).rstrip(":").strip().lower() in ("vieta", "pilsēta", "место"):
                nxt = td.next
                while nxt is not None and nxt.tag != "td":
                    nxt = nxt.next
                city = _text(nxt) or None
                break

    return Listing(
        ss_id=ss_id,
        category=category,
        url=url,
        title=title,
        city=city,
        price=price,
        params=params,
        description=description,
        posted_at=posted,
        photos=photos,
        raw_block=block.html,
    )
