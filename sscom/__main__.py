"""Komandas:
  python -m sscom run                  — galvenais cikls (RSS + saraksti + grafiks)
  python -m sscom rss                  — viena RSS pārbaude visām kategorijām
  python -m sscom full [kategorija]    — pilnā apstaigāšana (bez kategorijas = visas)
  python -m sscom test-filters         — parāda, kuri bāzes sludinājumi atbilst filtriem
  python -m sscom dealers [kategorija] — firmu atpazīšanas atskaite
"""
from __future__ import annotations

import json
import logging
import sys

from sqlalchemy import select

from . import config, db, filters, parser
from .http import Client
from .scheduler import Scheduler
from .watcher import Watcher


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    store = db.Store(db.get_engine())

    if cmd == "test-filters":
        fs = filters.load()
        with store.engine.connect() as c:
            rows = c.execute(select(db.listings).where(db.listings.c.status == "active")).all()
        hits = 0
        for r in rows:
            l = db.row_to_listing(r)
            f = filters.first_match(fs, l)
            if f:
                hits += 1
                print(f"{l.price:>7.0f} € | {f.name[:28]:28} | {l.title[:60]} | {l.url}")
        print(f"\n{hits} no {len(rows)} atbilst filtriem")
        return

    if cmd == "dealers":
        dealers_report(store, sys.argv[2] if len(sys.argv) > 2 else None)
        return

    client = Client()
    w = Watcher(store, client)
    try:
        if cmd == "run":
            Scheduler(w).run_forever()
        elif cmd == "rss":
            for c in config.CATEGORIES:
                print(c, "jauni:", w.check_rss(c))
        elif cmd == "full":
            cats = sys.argv[2:] or list(config.CATEGORIES)
            for c in cats:
                w.full_crawl(c)
        else:
            print(__doc__)
    finally:
        client.close()


def dealers_report(store: db.Store, category=None) -> None:
    from collections import Counter
    from .dealer import Boilerplate, classify, is_dealer
    Watcher(store, None).reclassify_dealers()
    q = select(db.listings).where(db.listings.c.status == "active")
    if category:
        q = q.where(db.listings.c.category == category)
    with store.engine.connect() as c:
        rows = c.execute(q).all()
    bp = Boilerplate(r.description for r in rows)
    dealers = [r for r in rows if r.is_dealer]
    print(f"\nFirmas: {len(dealers)} no {len(rows)} ({100 * len(dealers) // max(len(rows), 1)}%)\n")
    why = Counter(x.split(" «")[0].split(" ")[0] if x.startswith("veidne") else x
                  for r in dealers for x in (r.dealer_reasons or "").split("; ") if x)
    print("Biežākie iemesli:", ", ".join(f"{k} {v}" for k, v in why.most_common()))
    print("\nBiežākās veidnes rindas:")
    for line, n in bp.counts.most_common(12):
        if n >= 3:
            print(f"  {n:>3}× {line[:90]}")
    print("\nRobežgadījumi (1–2 punkti, skaitās PRIVĀTI):")
    for r in rows:
        if 0 < (r.dealer_score or 0) < 3:
            print(f"  {r.dealer_score}p | {r.price or 0:>6.0f} € | {r.dealer_reasons} | {(r.title or '')[:55]}")


if __name__ == "__main__":
    main()
