"""Telegram saruna ar Edgaru:
  • atbilde (Reply) uz bota ziņu ar jautājumu -> GPT atbild, zinot visu par šo sludinājumu un tirgu
  • ss.com / ss.lv saite -> pilns vērtējums
  • ekrānuzņēmums vai iekopēts sludinājuma teksts (FB u.c.) -> vērtējums
  • pogas ✅ Nopirku (-> pirkumu tabula) / 👎 Neinteresē
Atbild TIKAI uz TELEGRAM_CHAT_ID; citiem — klusums.
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
from datetime import timedelta
from typing import Any, Dict, Optional

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Table, select, update

from . import config, db, dealer, guide, inventory, parser, telegram
from .deals import CONDITION_LV, CONF_LV, MAX_NEW_RATIO, MIN_MARGIN, DealEngine, gen_line, shop_line
from .http import Client

log = logging.getLogger(__name__)

tg_messages = Table(
    "tg_messages", db.metadata,
    Column("message_id", BigInteger, primary_key=True),
    Column("ss_id", String(40)),
    Column("kind", String(20)),          # listing | answer | purchase | text
    Column("ref", Integer),              # purchase id
    Column("sent_at", DateTime),
)

SS_URL = re.compile(r"https?://(?:www\.|m\.)?ss\.(?:com|lv)(/msg/[^\s?#]+\.html)", re.I)
FB_URL = re.compile(r"facebook\.com|fb\.com|fb\.me", re.I)
PRICE_RE = re.compile(r"(\d[\d\s.,]{0,7})\s*(?:€|eur|eiro)", re.I)

HELP = (
    "👋 <b>Ko es protu</b>\n"
    "• <b>Atbildi</b> uz manu ziņu ar jautājumu — piem. „par cik kaulēties?”, „kāds ekrāns?”, "
    "„vai vērts ņemt tikai videokarti?”\n"
    "• Atsūti <b>ss.com saiti</b> — novērtēšu\n"
    "• Atsūti <b>ekrānuzņēmumu</b> vai iekopē sludinājuma tekstu ar cenu (piem. no Facebook) — novērtēšu\n"
    "• ✅ <b>Nopirku</b> — pievieno pirkumu tabulai; 👎 <b>Neinteresē</b> — atzīmē\n"
)

QA_SYSTEM = (
    "Tu esi Edgara palīgs lietotas datortehnikas pirkšanā un tālākpārdošanā Rīgā. "
    "Edgara noteikumi: pērk tikai, ja peļņa ≥ {margin:.0f}%, mazlietotu — ne dārgāk par {ratio:.0f}% no jaunas cenas, "
    "budžets līdz ~700 €, tikai Rīgā. Atbildi LATVISKI, uzrunā viņu ar 'tu', īsi (līdz ~8 rindām), konkrēti un ar "
    "skaitļiem no dotajiem datiem. Bez Markdown virsrakstiem; treknrakstu drīkst ar **. "
    "Ja datos atbildes nav, saki to godīgi un iesaki, ko pajautāt pārdevējam. Neizdomā specifikācijas."
)


class TgBot:
    def __init__(self, store: db.Store, deals: DealEngine):
        self.store = store
        self.deals = deals
        self.client = Client(delay_min=1, delay_max=2)  # atsevišķs no skrāpja klienta

    # ---------------------------------------------------------- infrastruktūra

    def install(self) -> None:
        telegram.on_sent = self._record

    def _record(self, message_id: int, ss_id: Optional[str], kind: str, ref: Optional[int]) -> None:
        try:
            with self.store.engine.begin() as c:
                c.execute(tg_messages.insert().values(message_id=message_id, ss_id=ss_id, kind=kind,
                                                      ref=ref, sent_at=db.now()))
        except Exception as e:  # dublikāts u.tml. — nav kritiski
            log.debug("tg_messages: %s", e)

    def start_in_thread(self) -> None:
        if not config.TELEGRAM_BOT_TOKEN:
            return
        self.install()
        threading.Thread(target=self.run, daemon=True, name="tgbot").start()
        log.info("Telegram saruna ieslēgta")

    def run(self) -> None:
        # Startā izlaižam sakrājušās vecās ziņas (atbildam tikai uz jaunām)
        pending = telegram.get_updates(None, timeout=0)
        offset = pending[-1]["update_id"] + 1 if pending else None
        while True:
            try:
                for u in telegram.get_updates(offset):
                    offset = u["update_id"] + 1
                    try:
                        self.handle(u)
                    except Exception:
                        log.exception("Telegram ziņas apstrāde neizdevās")
                        telegram.send_text("⚠️ Kaut kas nogāja greizi, mēģini vēlreiz.")
            except Exception:
                log.exception("Telegram getUpdates kļūda")
                time.sleep(10)

    def handle(self, u: Dict[str, Any]) -> None:
        if "callback_query" in u:
            cq = u["callback_query"]
            if str(cq.get("message", {}).get("chat", {}).get("id")) != str(config.TELEGRAM_CHAT_ID):
                return
            return self.on_button(cq)
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(config.TELEGRAM_CHAT_ID):
            return  # svešiniekiem — klusums
        text = (m.get("text") or m.get("caption") or "").strip()
        mid = m["message_id"]

        if text.startswith("/start") or text.startswith("/palidziba") or text.startswith("/help"):
            telegram.send_text(HELP)
            return
        reply = m.get("reply_to_message")
        if reply:
            ref = self._lookup(reply["message_id"])
            if ref and ref["kind"] == "purchase" and ref["ref"] and _number(text) is not None:
                return self.set_purchase_price(ref["ref"], _number(text), mid)
            if ref and ref["ss_id"]:
                return self.answer(ref["ss_id"], text, mid)
        if m.get("photo"):
            return self.review_screenshot(m["photo"][-1]["file_id"], text, mid)
        link = SS_URL.search(text)
        if link:
            return self.review_link("https://www.ss.com" + link.group(1), mid)
        if FB_URL.search(text):
            telegram.send_text("Facebook saiti atvērt nevaru (Facebook to aizliedz). Atsūti <b>ekrānuzņēmumu</b> "
                               "vai iekopē sludinājuma tekstu ar cenu — novērtēšu.", reply_to=mid)
            return
        if len(text) > 40 and PRICE_RE.search(text):
            return self.review_text(text, None, mid)
        telegram.send_text(HELP, reply_to=mid)

    def _lookup(self, message_id: int) -> Optional[Dict[str, Any]]:
        with self.store.engine.connect() as c:
            r = c.execute(select(tg_messages).where(tg_messages.c.message_id == message_id)).first()
        return dict(r._mapping) if r else None

    # ---------------------------------------------------------- jautājumi par sludinājumu

    def context(self, ss_id: str) -> Optional[Dict[str, Any]]:
        L = db.listings
        with self.store.engine.connect() as c:
            r = c.execute(select(L).where(L.c.ss_id == ss_id)).first()
            if r is None:
                return None
            facts = json.loads(r.ai_json or "{}")
            g = c.execute(select(guide.price_guide).where(guide.price_guide.c.guide_key == r.guide_key)).first() \
                if r.guide_key else None
            same = (L.c.guide_key == r.guide_key) if r.guide_key else (L.c.model_key == r.model_key)
            similar = c.execute(select(L.c.price, L.c.status, L.c.is_dealer, L.c.city, L.c.first_seen,
                                       L.c.last_seen, L.c.disappeared_at)
                                .where(same, L.c.ss_id != ss_id, L.c.price.isnot(None),
                                       L.c.status.in_(["active", "disappeared"]))
                                .order_by(L.c.price).limit(20)).all() if (r.guide_key or r.model_key) else []
        new = alt = None
        try:
            if facts.get("search_query"):
                new = self.deals.prices.get(facts["search_query"])
            if facts.get("new_alternative_query"):
                alt = self.deals.prices.get(facts["new_alternative_query"])
        except Exception:
            pass
        return {
            "sludinājums": {"virsraksts": r.title, "cena_eur": r.price, "pilsēta": r.city, "saite": r.url,
                            "statuss": r.status, "redzēts_kopš": str(r.first_seen.date()),
                            "parametri": json.loads(r.params_json or "{}"),
                            "apraksts": (r.description or "")[:1500], "firma": bool(r.is_dealer)},
            "ai_fakti": {k: facts.get(k) for k in ("model_key", "release_year", "cpu", "cpu_generation", "gpu",
                                                   "ram_gb", "ram_type", "storage_gb", "condition", "defects",
                                                   "urgent", "urgency_note", "components")},
            "bota_vērtējums": {"tālākpārdošana_eur": r.resale_est, "peļņa_proc": round((r.margin or 0) * 100),
                               "pārliecība": r.deal_conf, "komentārs": r.deal_note,
                               "pa_daļām": json.loads(r.partout_json) if r.partout_json else None},
            "cenu_ceļvedis": ({"grupa": g.guide_key, "sludinājumi": g.n, "mediāna": g.price_median,
                              "min": g.price_min, "max": g.price_max, "pārdodas_dienās": g.days_to_sell,
                              "pirkt_līdz": g.buy_max} if g else None),
            "līdzīgi_ss_com": [{"cena": s.price, "statuss": "pārdots/pazudis" if s.status == "disappeared" else "aktīvs",
                                "dienas": round(((s.disappeared_at or s.last_seen) - s.first_seen).total_seconds() / 86400, 1),
                                "firma": bool(s.is_dealer), "pilsēta": s.city} for s in similar],
            "jauns_LV": ({"vaicājums": new.query, "no_eur": new.min_price, "piemēri": new.names[:3]}
                         if new and new.n else None),
            "līdzīgs_jauns_LV": ({"vaicājums": alt.query, "no_eur": alt.min_price, "piemēri": alt.names[:3]}
                                 if alt and alt.n else None),
        }

    def answer(self, ss_id: str, question: str, reply_to: int) -> None:
        ctx = self.context(ss_id)
        if ctx is None:
            telegram.send_text("Šo sludinājumu vairs neatrodu datubāzē.", reply_to=reply_to)
            return
        if not self.deals.ai.enabled:
            telegram.send_text("AI nav pieslēgts (OPENAI_API_KEY).", reply_to=reply_to)
            return
        system = QA_SYSTEM.format(margin=MIN_MARGIN * 100, ratio=MAX_NEW_RATIO * 100)
        out = self.deals.ai.chat(system, f"DATI:\n{json.dumps(ctx, ensure_ascii=False, default=str)}\n\n"
                                         f"EDGARA JAUTĀJUMS: {question}")
        body = html.escape(out or "Neizdevās atbildēt, pamēģini vēlreiz.")
        body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", body)  # Markdown treknraksts -> Telegram HTML
        telegram.send_text("💬 " + body, reply_to=reply_to, ss_id=ss_id, kind="answer")

    # ---------------------------------------------------------- vērtējums pēc pieprasījuma

    def review_link(self, url: str, reply_to: int) -> None:
        L = db.listings
        path = re.search(r"/msg/[^ ]+\.html", url).group(0)
        with self.store.engine.connect() as c:
            r = c.execute(select(L).where(L.c.url.like(f"%{path}"))).first()
        if r is not None:
            l = db.row_to_listing(r)
            facts = json.loads(r.ai_json) if r.ai_json else None
        else:
            cat = re.search(r"/msg/[a-z]{2}/[^/]+/[^/]+/([^/]+)/", path)
            category = cat.group(1) if cat else "other"
            page = self.client.get(url)
            l = parser.parse_detail(page, url, category) if page else None
            if l is None or not l.ss_id:
                telegram.send_text("Neizdevās atvērt sludinājumu (varbūt dzēsts?).", reply_to=reply_to)
                return
            if l.price is None:
                telegram.send_text("Sludinājumā nav cenas — nav ar ko salīdzināt.", reply_to=reply_to)
                return
            l.dealer_score, l.dealer_reasons = dealer.classify(l)
            self.store.insert_listing(l)
            facts = None
        self._review(l, facts, reply_to)

    def review_screenshot(self, file_id: str, caption: str, reply_to: int) -> None:
        img = telegram.download_file(file_id)
        if not img:
            telegram.send_text("Neizdevās lejupielādēt attēlu.", reply_to=reply_to)
            return
        data = self.deals.ai.read_screenshot(img)
        if not data or not data.get("is_listing"):
            telegram.send_text("Attēlā neatpazinu sludinājumu. Pamēģini ekrānuzņēmumu, kur redzams nosaukums un cena.",
                               reply_to=reply_to)
            return
        text = f"{data['title']}\n{data['text']}\n{caption}"
        self.review_text(text, data.get("price_eur"), reply_to, title=data["title"], city=data.get("location"))

    def review_text(self, text: str, price: Optional[float], reply_to: int,
                    title: Optional[str] = None, city: Optional[str] = None) -> None:
        if price is None:
            m = PRICE_RE.search(text)
            price = parser.parse_price(m.group(0).replace("eur", "€").replace("eiro", "€")) if m else None
        if not price:
            telegram.send_text("Nevaru atrast cenu — pieraksti to, piem. „250 €”.", reply_to=reply_to)
            return
        l = parser.Listing(ss_id=f"m{reply_to}", category="manual", url=f"manual:{reply_to}",
                           title=(title or text.splitlines()[0])[:200], description=text[:3000],
                           price=float(price), city=city)
        self.store.insert_listing(l, status="manual")  # nepiedalās statistikā un salīdzinājumos
        self._review(l, None, reply_to, source="📋")

    def _review(self, l: parser.Listing, facts: Optional[Dict[str, Any]], reply_to: int, source: str = "🔍") -> None:
        e = html.escape
        if facts is None:
            was_dealer = l.dealer_score
            l.dealer_score = 0  # pēc pieprasījuma vērtējam arī firmas — atzīmēsim ziņā
            facts = self.deals.analyse(l, force=True)
            l.dealer_score = max(l.dealer_score, was_dealer or 0)
        if not facts:
            telegram.send_text("Neizdevās izanalizēt sludinājumu.", reply_to=reply_to)
            return
        deal = self.deals.evaluate(l, facts)
        po = self.deals.evaluate_partout(l, facts) if facts["item_type"] in ("desktop", "bundle") else None
        specs = [facts.get("model_key") or "", facts.get("cpu") or "",
                 f"{facts['ram_gb']} GB RAM" if facts.get("ram_gb") else "",
                 f"{facts['storage_gb']} GB" if facts.get("storage_gb") else "",
                 CONDITION_LV.get(facts["condition"], facts["condition"])]
        lines = [f"{source} <b>Vērtējums</b> · {l.price:.0f} € · {e(l.city or '?')}"
                 + (" · 🏪 firma" if l.is_dealer or facts.get("seller_is_business") else ""),
                 f"<b>{e(l.title[:120])}</b>", e(" · ".join(s for s in specs if s))]
        if gen_line(facts):
            lines.append(gen_line(facts))
        if deal is not None:
            market = []
            if deal.comps:
                market.append(f"ss.com {len(deal.comps)} līdzīgi: {min(deal.comps):.0f}–{max(deal.comps):.0f} €")
            if deal.new_min:
                market.append(f"jauns LV no {deal.new_min:.0f} € ({l.price / deal.new_min * 100:.0f}%)")
            if market:
                lines.append("📊 " + e(" · ".join(market)))
            if shop_line(deal):
                lines.append(shop_line(deal))
            good = "💰 <b>izdevīgi</b>" if deal.is_good else "📉 nav izdevīgi"
            lines.append(f"{good}: pārdot ~{deal.resale:.0f} € ({deal.profit:+.0f} €, {deal.margin * 100:+.0f}%)"
                         f" · pārliecība: {CONF_LV.get(deal.confidence, deal.confidence)}")
            if deal.verdict:
                lines.append("🤖 " + e(deal.verdict[:300]))
        if po is not None:
            lines.append(f"🔧 pa daļām ~{po.total:.0f} € ({po.margin * 100:+.0f}%)")
        if facts.get("defects"):
            lines.append("⚠️ " + e(facts["defects"][:150]))
        if l.url.startswith("http"):
            lines.append(f'<a href="{e(l.url)}">Atvērt ss.com</a>')
        lines.append("<i>💬 Atbildi uz šo ziņu, ja gribi jautāt sīkāk</i>")
        telegram.send_listing(l, "", text="\n".join(lines), reply_to=reply_to)

    # ---------------------------------------------------------- pogas un pirkumi

    def on_button(self, cq: Dict[str, Any]) -> None:
        action, _, ss_id = (cq.get("data") or "").partition(":")
        L = db.listings
        with self.store.engine.connect() as c:
            r = c.execute(select(L).where(L.c.ss_id == ss_id)).first()
        if r is None:
            telegram.answer_callback(cq["id"], "Sludinājums nav atrasts")
            return
        if action == "skip":
            with self.store.engine.begin() as c:
                c.execute(update(L).where(L.c.ss_id == ss_id).values(feedback="skip"))
            telegram.answer_callback(cq["id"], "👎 Atzīmēts: neinteresē")
            return
        if action == "buy":
            draft = inventory.prefill_from_listing(self.store, r.url) if r.url.startswith("http") else None
            items = (draft or {}).get("items") or [{"name": (r.model_key or r.title or "?")[:200],
                                                    "item_type": r.item_type, "guide_key": r.guide_key}]
            pid = inventory.add_purchase(self.store, title=(r.title or "?")[:200], total_price=r.price or 0,
                                         items=items, source_url=r.url if r.url.startswith("http") else None,
                                         ss_id=ss_id if r.url.startswith("http") else None)
            with self.store.engine.begin() as c:
                c.execute(update(L).where(L.c.ss_id == ss_id).values(feedback="bought"))
            telegram.answer_callback(cq["id"], "✅ Pievienots pirkumiem")
            try:
                inventory.appraise(self.store, pid, self.deals.ai)
            except Exception:
                log.exception("Pirkuma novērtēšana neizdevās")
            telegram.send_text(
                f"✅ Pievienots pirkumiem: <b>{html.escape((r.title or '')[:80])}</b> par {r.price or 0:.0f} € "
                f"({len(items)} {'detaļa' if len(items) == 1 else 'detaļas'}, tirgus cenas novērtētas).\n"
                "Ja samaksāji citu summu — <b>atbildi uz šo ziņu ar summu</b>, piem. <code>210</code>.",
                reply_to=cq.get("message", {}).get("message_id"), kind="purchase", ref=pid)
            return
        telegram.answer_callback(cq["id"])

    def set_purchase_price(self, purchase_id: int, price: float, reply_to: int) -> None:
        inventory.update_purchase(self.store, purchase_id, total_price=price)
        telegram.send_text(f"👍 Pirkuma cena nomainīta uz {price:.0f} €, pašizmaksa pārdalīta.", reply_to=reply_to)


def _number(text: str) -> Optional[float]:
    m = re.fullmatch(r"\s*(\d+(?:[.,]\d{1,2})?)\s*(?:€|eur)?\s*", text or "", re.I)
    return float(m.group(1).replace(",", ".")) if m else None
