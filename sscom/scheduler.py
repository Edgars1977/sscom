"""Galvenais cikls: RSS katrai kategorijai ar savu nejaušu taimeri,
pilnā apstaigāšana retāk, 07:00 rīta pārbaude, naktī izslēgts."""
from __future__ import annotations

import logging
import random
import time
from datetime import date, datetime
from typing import Dict, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from . import config, telegram
from .http import CircuitOpen
from .watcher import Watcher

log = logging.getLogger(__name__)
TZ = ZoneInfo(config.TIMEZONE)


def local_now() -> datetime:
    return datetime.now(TZ)


def is_active(t: datetime) -> bool:
    return config.ACTIVE_START <= t.hour < config.ACTIVE_END


class Scheduler:
    def __init__(self, watcher: Watcher):
        self.w = watcher
        now = time.monotonic()
        # izkliedējam sākumu, lai kategorijas nestartē vienlaikus
        self.next_rss: Dict[str, float] = {c: now + random.uniform(0, 30) for c in config.CATEGORIES}
        self.next_full: Dict[str, float] = {
            c: now + random.uniform(5, 20) * 60 for c in config.CATEGORIES
        }
        self.catchup_done: Optional[date] = None
        self.next_ai = time.monotonic() + 120

    def _rss_gap(self) -> float:
        return random.uniform(config.RSS_INTERVAL_MIN, config.RSS_INTERVAL_MAX)

    def _full_gap(self) -> float:
        return random.uniform(config.FULL_INTERVAL_MIN, config.FULL_INTERVAL_MAX) * 60

    def tick(self) -> None:
        t = local_now()
        mono = time.monotonic()

        # 07:00 — viena pilnā pārbaude visām kategorijām (savāc nakts sludinājumus)
        if t.hour == config.CATCHUP_HOUR and self.catchup_done != t.date():
            log.info("Rīta pārbaude")
            self.w.reload_filters()
            for c in random.sample(list(config.CATEGORIES), len(config.CATEGORIES)):
                self.w.full_crawl(c)
            self.catchup_done = t.date()
            for c in config.CATEGORIES:
                self.next_full[c] = time.monotonic() + self._full_gap()
            return

        if not is_active(t):
            return

        due = [c for c, at in self.next_rss.items() if at <= mono]
        for c in due:
            self.w.reload_filters()
            self.w.check_rss(c)
            self.next_rss[c] = time.monotonic() + self._rss_gap()

        if mono >= self.next_ai:
            self.w.backfill_ai(limit=10)
            self.next_ai = time.monotonic() + 60

        due = [c for c, at in self.next_full.items() if at <= mono]
        for c in due[:1]:  # ne vairāk kā viena pilnā apstaigāšana vienā reizē
            self.w.full_crawl(c)
            self.next_full[c] = time.monotonic() + self._full_gap()

    def run_forever(self) -> None:
        log.info("Sāku darbu. Aktīvs %02d:00–%02d:00, rīta pārbaude %02d:00 (%s)",
                 config.ACTIVE_START, config.ACTIVE_END, config.CATCHUP_HOUR, config.TIMEZONE)
        while True:
            try:
                self.tick()
            except CircuitOpen as e:
                log.error("Apturēts: %s. Gaidu %.0f min", e, config.CIRCUIT_PAUSE)
                telegram.send_text(f"⚠️ SS.com skrāpis apturēts uz {config.CIRCUIT_PAUSE:.0f} min: {e}")
                time.sleep(config.CIRCUIT_PAUSE * 60)
                self.w.client.failures = 0
            except Exception:  # neparedzēta kļūda — žurnālā un turpinām
                log.exception("Neparedzēta kļūda ciklā")
                time.sleep(60)
            time.sleep(5)
