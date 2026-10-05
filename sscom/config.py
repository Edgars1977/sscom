"""Konfigurācija. Viss, kas mainās starp vidēm, nāk no vides mainīgajiem."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://www.ss.com"

# Kategorijas atslēga -> ceļš (bez /sell/)
CATEGORIES = {
    "pc": "/lv/electronics/computers/pc/",
    "noutbooks": "/lv/electronics/computers/noutbooks/",
    "completing-pc": "/lv/electronics/computers/completing-pc/",
}


def list_url(category: str, page: int = 1) -> str:
    base = BASE_URL + CATEGORIES[category] + "sell/"
    return base if page == 1 else f"{base}page{page}.html"


def rss_url(category: str) -> str:
    return BASE_URL + CATEGORIES[category] + "sell/rss/"


USER_AGENT = os.getenv(
    "SS_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
)

# Pauze starp pieprasījumiem vienā pārlūkošanas reizē (sekundes)
REQUEST_DELAY_MIN = float(os.getenv("REQUEST_DELAY_MIN", "2"))
REQUEST_DELAY_MAX = float(os.getenv("REQUEST_DELAY_MAX", "6"))

# Pēc cik secīgām kļūdām apturēt darbu un cik ilgi gaidīt (sekundes)
MAX_CONSECUTIVE_FAILURES = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "5"))
BACKOFF_BASE = float(os.getenv("BACKOFF_BASE", "30"))
BACKOFF_MAX = float(os.getenv("BACKOFF_MAX", "1800"))

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///sscom.db")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Grafiks (Rīgas laiks). Naktī pilnībā izslēgts.
TIMEZONE = os.getenv("TIMEZONE", "Europe/Riga")
CATCHUP_HOUR = int(os.getenv("CATCHUP_HOUR", "7"))     # viena pilnā pārbaude no rīta
ACTIVE_START = int(os.getenv("ACTIVE_START", "8"))     # no šī brīža standarta režīms
ACTIVE_END = int(os.getenv("ACTIVE_END", "21"))        # no šī brīža izslēgts

# RSS katrai kategorijai atsevišķi (sekundes)
RSS_INTERVAL_MIN = float(os.getenv("RSS_INTERVAL_MIN", "60"))
RSS_INTERVAL_MAX = float(os.getenv("RSS_INTERVAL_MAX", "150"))
# Pilnā sarakstu apstaigāšana (minūtes)
FULL_INTERVAL_MIN = float(os.getenv("FULL_INTERVAL_MIN", "30"))
FULL_INTERVAL_MAX = float(os.getenv("FULL_INTERVAL_MAX", "90"))
# Cik ilgi gaidīt pēc CircuitOpen (minūtes)
CIRCUIT_PAUSE = float(os.getenv("CIRCUIT_PAUSE", "30"))
