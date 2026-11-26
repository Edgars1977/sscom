"""Telegram saruna ar Edgaru:
  • atbilde (Reply) uz bota ziņu ar jautājumu -> GPT atbild, zinot visu par šo sludinājumu un tirgu
  • ss.com / ss.lv saite -> pilns vērtējums
  • ekrānuzņēmums vai iekopēts sludinājuma teksts (FB u.c.) -> vērtējums
  • pogas ✅ Nopirku (-> pirkumu tabula) / 👎 Neinteresē
  • brīva ziņa -> GPT nosaka nolūku: jautājums par pēdējo sludinājumu / tirgus jautājums no datubāzes /
    iekopēts sludinājums / mērķa cena / atskaite; nesaprotot — pasaka, ko saprata, un rāda iespējas
  • pēc restarta (deploy) apstrādā ziņas, kas atnākušas pēdējās 30 min
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
from .deals import CONDITION_LV, CONF_LV, MAX_NEW_RATIO, MIN_MARGIN, DealEngine, gen_line, hybrid_lines, shop_line
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
    "👋 <b>Ko es protu</b> — raksti brīvi, sapratīšu:\n"
    "• <b>Jautājums par sludinājumu</b> — atbildi uz manu ziņu vai vienkārši raksti "
    "(„par cik kaulēties?” — pieņemšu, ka par pēdējo)\n"
    "• <b>Tirgus</b> — „cik maksā RTX 3060?”, „pa cik pārdot ThinkPad T480?”\n"
    "• <b>ss.com saite</b>, <b>ekrānuzņēmums</b> vai iekopēts sludinājums ar cenu — novērtēšu\n"
    "• <b>Mērķa cena</b> — „paziņo, ja NVMe 1TB zem 50 €”\n"
    "• <b>Atskaite</b> — „kā gāja šonedēļ?”\n"
    "• ✅ <b>Nopirku</b> / 👎 <b>Neinteresē</b> pogas zem sludinājumiem\n"
)

MARKET_SYSTEM = (
    "Tu esi Edgara palīgs lietotas datortehnikas tālākpārdošanā Rīgā. Tev ir dati no viņa ss.com datubāzes "
    "(privātie sludinājumi; 'pazudis' ≈ pārdots, 'dienas' = cik ilgi bija ievietots) un cenu ceļvedis. "
    "Edgara noteikumi: pērk, ja peļņa ≥ {margin:.0f}%, mazlietotu ne dārgāk par {ratio:.0f}% no jaunas cenas. "
    "Atbildi LATVISKI, uzrunā ar 'tu', īsi (līdz ~10 rindām), ar konkrētiem skaitļiem: tipiskā cena, cik ātri "
    "pārdodas, par cik pirkt (≈ mediāna/1.2), par cik likt pārdošanā. Detaļas un portatīvos/datorus ar to pašu "
    "detaļu vērtē ATSEVIŠĶI (videokarte ≠ portatīvais ar to). Ja datu maz vai nav — saki godīgi un "
    "iesaki precīzāku vaicājumu. Bez Markdown virsrakstiem; treknrakstu drīkst ar **."
)
CATEGORY_LV = {"noutbooks": "portatīvais", "pc": "dators", "completing-pc": "detaļa"}
BACKLOG_MINUTES = 30  # pēc restarta apstrādājam ziņas, kas atnākušas pēdējās N minūtēs

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
        # Startā: ziņas, kas atnākušas restarta (deploy) laikā, apstrādājam; senākas izlaižam
        pending = telegram.get_updates(None, timeout=0)
        offset = pending[-1]["update_id"] + 1 if pending else None
        cutoff = time.time() - BACKLOG_MINUTES * 60
        for u in pending:
            # pogas nospiešanai laika nav (ir tikai sākotnējās ziņas laiks) — tās apstrādājam vienmēr
            when = u["message"].get("date", 0) if "message" in u else time.time()
            if when >= cutoff:
                try:
                    self.handle(u)
                except Exception:
                    log.exception("Telegram ziņas apstrāde neizdevās")
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
        telegram.typing()

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
        if len(text) > 120 and PRICE_RE.search(text):
            return self.review_text(text, None, mid)  # garš teksts ar cenu = iekopēts sludinājums
        if not text:
            telegram.send_text("Šāda veida ziņu (" + ", ".join(k for k in ("sticker", "voice", "document", "video")
                               if k in m) + ") vēl nesaprotu.\n\n" + HELP, reply_to=mid)
            return
        return self.route(text, mid)

    # ---------------------------------------------------------- brīvas ziņas: ko Edgars grib?

    def last_alert(self) -> Optional[tuple]:
        """Pēdējais sludinājums, ko bots sūtīja -> (ss_id, virsraksts)"""
        L = db.listings
        with self.store.engine.connect() as c:
            r = c.execute(select(tg_messages.c.ss_id, L.c.title).join(L, L.c.ss_id == tg_messages.c.ss_id)
                          .where(tg_messages.c.kind == "listing")
                          .order_by(tg_messages.c.sent_at.desc()).limit(1)).first()
        return (r.ss_id, r.title) if r else None

    def route(self, text: str, mid: int) -> None:
        if not self.deals.ai.enabled:
            telegram.send_text(HELP, reply_to=mid)
            return
        last = self.last_alert()
        it = self.deals.ai.classify_intent(text, last[1][:120] if last else None)
        if not it:
            telegram.send_text("Neizdevās saprast (AI kļūda). Pamēģini vēlreiz.\n\n" + HELP, reply_to=mid)
            return
        intent, query, price = it["intent"], (it.get("query") or "").strip(), it.get("price")
        log.info("Telegram nolūks: %s | %s | %s", intent, query, price)
        if intent == "listing_question":
            if not last:
                telegram.send_text("Vēl neesmu sūtījis nevienu sludinājumu. Atsūti ss.com saiti, par kuru jautā.",
                                   reply_to=mid)
                return
            telegram.send_text(f"<i>Pieņemu, ka jautā par pēdējo: {html.escape(last[1][:80])}. "
                               "Par citu — atbildi tieši uz tā ziņu.</i>", reply_to=mid)
            return self.answer(last[0], text, mid)
        if intent == "market_question" and query:
            return self.market_answer(query, text, mid)
        if intent == "evaluate_listing":
            return self.review_text(text, price, mid)
        if intent == "set_target" and query:
            return self.set_target_from_text(query, price, mid)
        if intent == "weekly_report":
            from . import report
            telegram.send_text(report.build(self.store, 7), reply_to=mid)
            return
        reply = (it.get("reply_lv") or "").strip()
        if intent in ("market_question", "set_target") and not query:
            reply = "Nesapratu, par kuru preci. Uzraksti precīzāk, piem. „cik maksā RTX 3060 12GB?”"
        telegram.send_text(html.escape(reply) + "\n\n" + HELP if reply else HELP, reply_to=mid)

    def _matching(self, query: str):
        """Privātie sludinājumi (60 d) un ceļveža grupas, kas satur visus vaicājuma vārdus."""
        tokens = [t for t in re.split(r"[\s,/]+", query.lower()) if t]
        def hit(*parts) -> bool:
            blob = " ".join(p or "" for p in parts).lower()
            squashed = blob.replace(" ", "")
            return all(t in blob or t in squashed for t in tokens)
        L = db.listings
        since = db.now() - timedelta(days=60)
        with self.store.engine.connect() as c:
            groups = [g for g in c.execute(select(guide.price_guide)).all() if hit(g.guide_key)]
            rows = [r for r in c.execute(
                select(L.c.title, L.c.model_key, L.c.guide_key, L.c.price, L.c.status, L.c.city, L.c.category,
                       L.c.first_seen, L.c.last_seen, L.c.disappeared_at)
                .where(L.c.is_dealer == 0, L.c.price.isnot(None), L.c.first_seen >= since,
                       L.c.status.in_(["active", "disappeared"]))).all()
                if hit(r.title, r.model_key, r.guide_key)]
        rows.sort(key=lambda r: r.first_seen, reverse=True)
        return groups, rows

    def market_answer(self, query: str, question: str, mid: int) -> None:
        groups, rows = self._matching(query)
        if not groups and not rows:
            telegram.send_text(f"Par „{html.escape(query)}” pēdējās 60 dienās datubāzē nekā nav. "
                               "Pamēģini īsāk/citādi, piem. „3060” vai „t480”.", reply_to=mid)
            return
        ctx = {
            "vaicājums": query,
            "cenu_ceļvedis": [{"grupa": g.guide_key, "sludinājumi": g.n, "mediāna": g.price_median,
                               "min": g.price_min, "max": g.price_max, "pārdodas_dienās": g.days_to_sell,
                               "jauns_LV_no": g.new_min, "pirkt_līdz": g.buy_max,
                               "lētākais_aktīvais": g.cheapest_price} for g in groups[:8]],
            "sludinājumi": [{"kategorija": CATEGORY_LV.get(r.category, r.category),
                             "virsraksts": (r.title or "")[:70], "cena": r.price,
                             "statuss": "pazudis" if r.status == "disappeared" else "aktīvs",
                             "dienas": round(((r.disappeared_at or r.last_seen) - r.first_seen).total_seconds() / 86400, 1)}
                            for r in rows[:40]],
            "kopā_atrasti": len(rows),
        }
        system = MARKET_SYSTEM.format(margin=MIN_MARGIN * 100, ratio=MAX_NEW_RATIO * 100)
        out = self.deals.ai.chat(system, f"DATI:\n{json.dumps(ctx, ensure_ascii=False, default=str)}\n\n"
                                         f"EDGARA JAUTĀJUMS: {question}")
        body = html.escape(out or "Neizdevās atbildēt, pamēģini vēlreiz.")
        body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", body)
        telegram.send_text(f"📊 {body}\n<i>({len(rows)} sludinājumi, {len(groups)} ceļveža grupas)</i>",
                           reply_to=mid, kind="answer")

    def set_target_from_text(self, query: str, price: Optional[float], mid: int) -> None:
        groups, _ = self._matching(query)
        if price is None:
            telegram.send_text(f"Kāda cena? Piem. „paziņo, ja {html.escape(query)} zem 50 €”.", reply_to=mid)
            return
        if not groups:
            telegram.send_text(f"Ceļvedī nav grupas „{html.escape(query)}”. Grupas veidojas no sludinājumiem "
                               "(piem. „NVMe 1TB”, „RTX 3060 12GB”, „DDR4 16GB (2×8GB)”) — pamēģini tā.",
                               reply_to=mid)
            return
        if len(groups) > 1:
            groups.sort(key=lambda g: -g.n)
            exact = [g for g in groups if g.guide_key.lower() == query.lower()]
            if not exact:
                names = "\n".join(f"• <code>{html.escape(g.guide_key)}</code> ({g.n}×)" for g in groups[:6])
                telegram.send_text(f"Atradu vairākas grupas — kuru domāji?\n{names}\n\n"
                                   f"Uzraksti, piem.: „paziņo, ja {html.escape(groups[0].guide_key)} zem {price:.0f} €”",
                                   reply_to=mid)
                return
            groups = exact
        g = groups[0]
        guide.set_target(self.store, g.guide_key, float(price), "no Telegram")
        telegram.send_text(f"🎯 Iestatīts: <b>{html.escape(g.guide_key)}</b> — paziņošu, ja zem {price:.0f} €. "
                           f"(ss.com mediāna {g.price_median:.0f} €, {g.n} sludinājumi)", reply_to=mid)

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
        cat = re.search(r"/msg/[a-z]{2}/[^/]+/[^/]+/([^/]+)/", path)
        category = cat.group(1) if cat else "other"
        fresh = None
        if r is not None and r.status == "disappeared":
            # ss.com dzēsta sludinājuma saiti piešķir citam — pārbaudām, vai tas vēl ir tas pats
            page = self.client.get(url)
            fresh = parser.parse_detail(page, url, category) if page else None
            if fresh is not None and fresh.ss_id and fresh.ss_id != r.ss_id:
                r = None
        if r is not None:
            l = db.row_to_listing(r)
            facts = json.loads(r.ai_json) if r.ai_json else None
        else:
            if fresh is None:
                page = self.client.get(url)
                fresh = parser.parse_detail(page, url, category) if page else None
            l = fresh
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
        if city is None and re.search(r"\br[īi]g[aāuy]|\bриг", text, re.I):
            city = "Rīga"
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
            lines.extend(hybrid_lines(deal))
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
