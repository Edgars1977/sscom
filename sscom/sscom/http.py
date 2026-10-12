"""Pieklājīgs HTTP klients: pauzes, robots.txt, eksponenciāls backoff."""
from __future__ import annotations

import logging
import random
import re
import time
from typing import List, Optional
from urllib.parse import urlparse

import httpx

from . import config

log = logging.getLogger(__name__)

RETRY_STATUSES = {403, 429, 500, 502, 503, 504}


class CircuitOpen(Exception):
    """Pārāk daudz secīgu kļūdu; jāaptur darbs uz laiku."""


class Robots:
    """Vienkāršs robots.txt lasītājs ar * un $ atbalstu (stdlib to nemāk)."""

    def __init__(self, text: str):
        self.disallow: List[re.Pattern] = []
        self.allow: List[re.Pattern] = []
        applies = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, val = (p.strip() for p in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                applies = val == "*"
            elif applies and val and key in ("disallow", "allow"):
                (self.disallow if key == "disallow" else self.allow).append(self._compile(val))

    @staticmethod
    def _compile(rule: str) -> re.Pattern:
        pat = re.escape(rule).replace(r"\*", ".*")
        if pat.endswith(r"\$"):
            pat = pat[:-2] + "$"
        return re.compile(pat)

    def allowed(self, url: str) -> bool:
        path = urlparse(url).path or "/"
        if any(p.match(path) for p in self.allow):
            return True
        return not any(p.match(path) for p in self.disallow)


class Client:
    def __init__(self, delay_min: float = None, delay_max: float = None,
                 base_url: str = config.BASE_URL):
        self.base_url = base_url
        self.delay_min = config.REQUEST_DELAY_MIN if delay_min is None else delay_min
        self.delay_max = config.REQUEST_DELAY_MAX if delay_max is None else delay_max
        self.http = httpx.Client(
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "lv,en;q=0.8",
            },
            timeout=30,
            follow_redirects=True,
        )
        self._last_request = 0.0
        self.failures = 0
        self.errors = 0  # kopējais kļūdu skaits (scrape_log vajadzībām)
        self.robots: Optional[Robots] = None

    def _load_robots(self) -> None:
        try:
            r = self.http.get(self.base_url + "/robots.txt")
            self.robots = Robots(r.text if r.status_code == 200 else "")
        except httpx.HTTPError as e:
            log.warning("robots.txt neizdevās ielādēt: %s", e)
            self.robots = Robots("")

    def _wait(self) -> None:
        gap = random.uniform(self.delay_min, self.delay_max)
        elapsed = time.monotonic() - self._last_request
        if elapsed < gap:
            time.sleep(gap - elapsed)

    def get(self, url: str) -> Optional[str]:
        """Atgriež lapas tekstu vai None (404 / robots aizliegums).

        Pie 403/429/5xx un tīkla kļūdām gaida eksponenciāli ilgāk;
        pēc MAX_CONSECUTIVE_FAILURES izmet CircuitOpen.
        """
        if self.robots is None:
            self._load_robots()
        if not self.robots.allowed(url):
            log.warning("robots.txt aizliedz: %s", url)
            return None

        while True:
            self._wait()
            self._last_request = time.monotonic()
            try:
                r = self.http.get(url)
                status = r.status_code
            except httpx.HTTPError as e:
                status, r = None, None
                log.warning("Tīkla kļūda %s: %s", url, e)

            if status == 200:
                self.failures = 0
                return r.text
            if status == 404:
                self.failures = 0
                return None

            self.failures += 1
            self.errors += 1
            if status is not None and status not in RETRY_STATUSES:
                log.warning("HTTP %s: %s", status, url)
                return None
            if self.failures >= config.MAX_CONSECUTIVE_FAILURES:
                raise CircuitOpen(f"{self.failures} secīgas kļūdas, pēdējā {status} uz {url}")
            pause = min(config.BACKOFF_BASE * 2 ** (self.failures - 1), config.BACKOFF_MAX)
            pause *= random.uniform(0.8, 1.2)
            log.warning("HTTP %s uz %s, gaidu %.0f s", status, url, pause)
            time.sleep(pause)

    def close(self) -> None:
        self.http.close()
