"""1. soļa pārbaude: python -m sscom.try_parse noutbooks [sludinājumu_skaits]"""
from __future__ import annotations

import logging
import sys

from . import config, parser
from .http import Client


def show(l: parser.Listing) -> None:
    keys = {k: l.key(k) for k in parser.KEY_FIELDS if l.key(k)}
    print(f"  {l.ss_id or '-':>9} | {l.price!s:>7} € | {(l.city or '-')[:14]:14} | {l.title[:50]}")
    print(f"            {keys}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cat = sys.argv[1] if len(sys.argv) > 1 else "noutbooks"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    c = Client()

    html = c.get(config.list_url(cat))
    rows = parser.parse_list_page(html, cat)
    print(f"\n== SARAKSTS {cat}: {len(rows)} rindas, pēdējā lapa {parser.last_page_number(html)}")
    for l in rows[:5]:
        show(l)
    print(f"  bez cenas: {sum(1 for l in rows if l.price is None)}")

    items = parser.parse_rss(c.get(config.rss_url(cat)), cat)
    print(f"\n== RSS: {len(items)} ieraksti")
    for l in items[:3]:
        print(f"  {l.posted_at} | {l.price} € | {l.params} | {l.url}")

    print(f"\n== SLUDINĀJUMI ({n})")
    for row in rows[:n]:
        d = parser.parse_detail(c.get(row.url), row.url, cat)
        if d is None:
            continue
        print(f"\n  {d.url}\n  id={d.ss_id} cena={d.price} datums={d.posted_at} foto={len(d.photos)}")
        print(f"  params={d.params}")
        print(f"  kanon={ {k: d.key(k) for k in parser.KEY_FIELDS if d.key(k)} }")
        print("  apraksts: " + d.description[:200].replace("\n", " / "))
    c.close()


if __name__ == "__main__":
    main()
