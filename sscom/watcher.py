"""Jaunu sludinājumu atklāšana (RSS + saraksti) un paziņošana."""
from __future__ import annotations

import logging
from typing import List, Optional

from sqlalchemy import select

from . import config, db, dealer, filters, parser, telegram
from . import deals as dealmod
from .http import Client

log = logging.getLogger(__name__)


class Watcher:
    def __init__(self, store: db.Store, client: Client, deals=None):
        self.store = store
        self.client = client
        self.deals = deals  # DealEngine vai None
        self.filters: List[filters.Filter] = filters.load()
        self.boilerplate = dealer.Boilerplate(store.all_descriptions())

    def reload_filters(self) -> None:
        self.filters = filters.load()

    def _has_full_crawl(self, category: str) -> bool:
        with self.store.engine.connect() as c:
            row = c.execute(
                select(db.scrape_log.c.id)
                .where(db.scrape_log.c.category == category, db.scrape_log.c.kind == "full")
                .limit(1)
            ).first()
        return row is not None

    # ------------------------------------------------------------ viens jauns

    def process_new(self, url: str, category: str, notify: bool,
                    known_ids: Optional[dict] = None) -> Optional[parser.Listing]:
        """Atver sludinājumu vienreiz, saglabā un, ja der filtram, paziņo."""
        html = self.client.get(url)
        if html is None:
            return None
        l = parser.parse_detail(html, url, category)
        if l is None or not l.ss_id:
            return None
        known_ids = known_ids if known_ids is not None else self.store.known_ids(category)
        if l.ss_id in known_ids:
            # tas pats sludinājums ar citu saiti — tikai atjauninām
            self.store.touch(l.ss_id, l.price, known_ids[l.ss_id])
            return None

        self.boilerplate.add(l.description)
        l.dealer_score, l.dealer_reasons = dealer.classify(l, self.boilerplate)

        if l.price is None:
            # bez cenas (maiņai, runājama...) — izlaižam, bet atceramies, lai neatvērtu vēlreiz
            self.store.insert_listing(l, status="no_price")
            return None

        self.store.insert_listing(l)
        known_ids[l.ss_id] = l.price
        if notify and self.deals is not None and self.deals.enabled:
            try:
                if self.deals.handle_new(l):
                    return l
            except Exception:  # AI/cenu kļūda nedrīkst apturēt skrāpi
                log.exception("Darījuma vērtēšana neizdevās: %s", l.url)
        if notify:
            f = filters.first_match(self.filters, l)
            extra = None
            last = getattr(self.deals, "last_eval", None) if self.deals is not None else None
            if f and last:
                extra = dealmod.format_eval_line(*last)
            if f and telegram.send_listing(l, f"parauga filtrs: {f.name}", extra=extra):
                self.store.mark_notified(l.ss_id, f.name)
                log.info("Paziņots: %s (%s)", l.url, f.name)
        return l

    # ------------------------------------------------------------ RSS

    def check_rss(self, category: str) -> int:
        """Atgriež jauno skaitu. Pirmajā reizē tikai iegaumē (bez paziņojumiem)."""
        errors_before = self.client.errors
        xml = self.client.get(config.rss_url(category))
        if xml is None:
            self.store.log(category, "rss", errors=self.client.errors - errors_before + 1)
            return 0
        items = parser.parse_rss(xml, category)
        known_urls = self.store.known_urls(category)
        notify = bool(known_urls)  # tukša bāze = sākumstāvoklis, nespamojam
        new_items = [i for i in items if i.url not in known_urls]
        new = 0
        known_ids = self.store.known_ids(category)
        for item in reversed(new_items):  # vecākie vispirms
            if self.process_new(item.url, category, notify, known_ids):
                new += 1
        self.store.log(category, "rss", found=len(items), new=new,
                       errors=self.client.errors - errors_before)
        if new_items:
            log.info("[%s] RSS: %d jauni%s", category, new, "" if notify else " (sākumstāvoklis)")
        return new

    # ------------------------------------------------------------ pilnā apstaigāšana

    def full_crawl(self, category: str, fetch_details: bool = True) -> int:
        """Izstaigā visas sarakstu lapas. Zināmajiem atjaunina last_seen/cenu,
        jaunajiem vienreiz atver sludinājumu. Paziņo tikai, ja agrāk jau bija pilna apstaigāšana."""
        errors_before = self.client.errors
        notify = self._has_full_crawl(category)
        known_ids = self.store.known_ids(category)
        seen: set = set()
        new = 0

        first = self.client.get(config.list_url(category, 1))
        if first is None:
            self.store.log(category, "full", errors=self.client.errors - errors_before + 1)
            return 0
        last = parser.last_page_number(first)
        log.info("[%s] pilnā apstaigāšana: %d lapas, bāzē %d", category, last, len(known_ids))

        rows: List[parser.Listing] = []
        for page in range(1, last + 1):
            html = first if page == 1 else self.client.get(config.list_url(category, page))
            if html is None:
                continue
            page_rows = parser.parse_list_page(html, category)
            fresh = [r for r in page_rows if r.ss_id not in seen]
            if page > 1 and page_rows and not fresh:
                break  # ss.com aiz pēdējās lapas atdod 1. lapu
            seen.update(r.ss_id for r in page_rows)
            rows.extend(fresh)

        for r in rows:
            if r.ss_id in known_ids:
                self.store.touch(r.ss_id, r.price, known_ids[r.ss_id], city=r.city)
            elif fetch_details:
                if r.price is None:
                    continue  # sarakstā jau redzams, ka nav cenas — neveram vaļā
                if self.process_new(r.url, category, notify, known_ids):
                    new += 1

        self.store.log(category, "full", found=len(rows), new=new,
                       errors=self.client.errors - errors_before)
        self.reclassify_dealers()
        log.info("[%s] pilnā apstaigāšana: %d sludinājumi, %d jauni", category, len(rows), new)
        return new

    # ------------------------------------------------------------ firmas

    def reclassify_dealers(self) -> int:
        """Pārvērtē visus — veidnes kļūst redzamas tikai, kad sakrājas dati. Atgriež izmaiņu skaitu."""
        with self.store.engine.connect() as c:
            rows = c.execute(select(db.listings)).all()
        self.boilerplate = dealer.Boilerplate(r.description for r in rows)
        changed = 0
        for r in rows:
            l = db.row_to_listing(r)
            score, reasons = dealer.classify(l, self.boilerplate)
            if score != (r.dealer_score or 0) or "; ".join(reasons) != (r.dealer_reasons or ""):
                self.store.set_dealer(r.ss_id, score, reasons, dealer.is_dealer(score))
                changed += 1
        if changed:
            log.info("Firmu vērtējums pārrēķināts: %d izmaiņas", changed)
        return changed

    # ------------------------------------------------------------ AI vecajiem ierakstiem

    def backfill_ai(self, limit: int = 10) -> int:
        """Analizē vecos privātos sludinājumus (bez paziņojumiem), lai būtu ar ko salīdzināt."""
        if self.deals is None or not self.deals.enabled:
            return 0
        from .deals import ANALYSE_MAX_PRICE
        L = db.listings
        with self.store.engine.connect() as c:
            rows = c.execute(
                select(L).where(L.c.ai_at.is_(None), L.c.is_dealer == 0, L.c.price.isnot(None),
                                L.c.price <= ANALYSE_MAX_PRICE, L.c.status != "no_price")
                .order_by(L.c.first_seen.desc()).limit(limit)
            ).all()
        done = 0
        for r in rows:
            l = db.row_to_listing(r)
            if self.deals.analyse(l) is None:
                # atzīmējam, lai neķertos pie tā paša bezgalīgi
                with self.store.engine.begin() as c:
                    c.execute(db.listings.update().where(L.c.ss_id == r.ss_id).values(ai_at=db.now()))
            done += 1
        if done:
            log.info("AI analīze vecajiem: %d", done)
        return done
