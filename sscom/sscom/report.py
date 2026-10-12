"""Kopsavilkums par periodu: ko bots redzēja, ko sūtīja, cik maksāja AI, kas pārdodas.
Lieto: python -m sscom report [dienas]  un iknedēļas Telegram ziņa (scheduler)."""
from __future__ import annotations

import html
import statistics
from collections import Counter, defaultdict
from datetime import timedelta
from typing import List

from sqlalchemy import func, select

from . import db
from .ai import ai_usage

# Aptuvenas cenas USD par 1M tokeniem (ievade, izvade) — tikai orientējošai izmaksu aplēsei
PRICE_PER_M = {"gpt-5.4-nano": (0.20, 1.25), "gpt-5.4-mini": (0.75, 4.50)}

CAT_LV = {"pc": "Datori", "noutbooks": "Portatīvie", "completing-pc": "Detaļas"}


def build(store: db.Store, days: int = 7) -> str:
    since = db.now() - timedelta(days=days)
    L = db.listings
    with store.engine.connect() as c:
        new = c.execute(select(L.c.category, func.count(), func.sum(L.c.is_dealer))
                        .where(L.c.first_seen >= since).group_by(L.c.category)).all()
        sent = c.execute(select(L.c.matched_filter, func.count())
                         .where(L.c.notified_at >= since).group_by(L.c.matched_filter)).all()
        deals = c.execute(select(L.c.title, L.c.price, L.c.resale_est, L.c.partout_est, L.c.status,
                                 L.c.first_seen, L.c.disappeared_at, L.c.last_seen, L.c.url)
                          .where(L.c.notified_at >= since, L.c.matched_filter.like("AI%"))
                          .order_by(L.c.notified_at)).all()
        evaluated = c.execute(select(L.c.model_key, L.c.margin, L.c.item_type)
                              .where(L.c.ai_at >= since, L.c.margin.isnot(None))).all()
        gone = c.execute(select(L.c.model_key, L.c.price, L.c.first_seen, L.c.last_seen)
                         .where(L.c.is_dealer == 0, L.c.model_key.isnot(None),
                                L.c.last_seen < db.now() - timedelta(days=2),
                                L.c.first_seen >= since - timedelta(days=30))).all()
        usage = c.execute(select(ai_usage.c.model, func.count(), func.sum(ai_usage.c.prompt_tokens),
                                 func.sum(ai_usage.c.completion_tokens))
                          .where(ai_usage.c.ts >= since).group_by(ai_usage.c.model)).all()
        errors = c.execute(select(func.sum(db.scrape_log.c.errors), func.count())
                           .where(db.scrape_log.c.ts >= since)).first()

    lines: List[str] = [f"📊 <b>SS.com atskaite — {days} dienas</b>"]

    total = sum(n for _, n, _ in new)
    dealers = sum(d or 0 for _, _, d in new)
    lines.append(f"\n<b>Jauni sludinājumi:</b> {total} (firmas {dealers}, {100 * dealers // max(total, 1)}%)")
    for cat, n, d in sorted(new):
        lines.append(f"  {CAT_LV.get(cat, cat)}: {n} (privāti {n - (d or 0)})")

    lines.append("\n<b>Telegram ziņas:</b> " + (", ".join(f"{k or '-'}: {n}" for k, n in sent) or "nav"))

    if evaluated:
        margins = [m for _, m, _ in evaluated]
        good = sum(1 for m in margins if m >= 0.2)
        lines.append(f"\n<b>AI novērtēti:</b> {len(evaluated)} · mediānā peļņa {statistics.median(margins) * 100:+.0f}% "
                     f"· ≥20%: {good}")

    if deals:
        lines.append("\n<b>Nosūtītie darījumi:</b>")
        for t, price, resale, partout, status, first, gone_at, last, url in deals[-10:]:
            target = partout or resale or 0
            # pazušanas loģikas vēl nav: "pazudis" = pilnajās pārbaudēs nav redzēts > 2 dienas
            missing = last < db.now() - timedelta(days=2)
            life = ((gone_at or last) - first).total_seconds() / 3600 if missing else None
            state = f"pazuda pēc ~{life:.0f} h" if life is not None else "vēl aktīvs"
            lines.append(f'  {price:.0f}→{target:.0f} € · {state} · <a href="{html.escape(url)}">'
                         f"{html.escape((t or '')[:40])}</a>")

    # Kas pazūd ātri (privātie, pēc modeļa) — tikai ja ir pazušanas dati
    life = defaultdict(list)
    for key, price, first, last in gone:
        life[key].append((last - first).total_seconds() / 86400)
    fast = sorted(((statistics.median(v), k, len(v)) for k, v in life.items() if len(v) >= 2))[:8]
    if fast:
        lines.append("\n<b>Ātrāk pazūd (mediāna dienās):</b>")
        for d, k, n in fast:
            lines.append(f"  {d:.1f} d · {html.escape(k[:45])} ({n}×)")

    if usage:
        cost = 0.0
        parts = []
        for model, n, pin, pout in usage:
            pi, po = PRICE_PER_M.get(model, (0, 0))
            cost += (pin or 0) / 1e6 * pi + (pout or 0) / 1e6 * po
            parts.append(f"{model} {n}×")
        lines.append(f"\n<b>AI:</b> {', '.join(parts)} · aplēse ~${cost:.2f}")

    if errors and errors[1]:
        lines.append(f"<b>Kļūdas:</b> {errors[0] or 0} no {errors[1]} pārbaudēm")
    return "\n".join(lines)
