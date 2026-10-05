"""Telegram ziņas. Kļūda sūtīšanā nekad nenogāž skrāpi — tikai žurnālā."""
from __future__ import annotations

import html
import logging
from typing import Optional

import httpx

from . import config
from .parser import Listing

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
CAPTION_LIMIT = 1024


def _call(method: str, **data) -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.warning("Telegram nav konfigurēts (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        return False
    try:
        r = httpx.post(
            API.format(token=config.TELEGRAM_BOT_TOKEN, method=method),
            data={"chat_id": config.TELEGRAM_CHAT_ID, "parse_mode": "HTML", **data},
            timeout=30,
        )
        if r.status_code != 200:
            log.warning("Telegram %s: %s %s", method, r.status_code, r.text[:200])
            return False
        return True
    except httpx.HTTPError as e:
        log.warning("Telegram %s neizdevās: %s", method, e)
        return False


def send_text(text: str) -> bool:
    return _call("sendMessage", text=text, disable_web_page_preview="true")


def format_listing(l: Listing, filter_name: str, extra: Optional[str] = None) -> str:
    e = html.escape
    specs = []
    for label, key, suffix in (("", "brand", ""), ("", "model", ""), ("CPU", "cpu", ""),
                               ("RAM", "ram", " GB"), ("Disks", "disk", " GB"),
                               ("GPU", "gpu", ""), ("", "screen", ""), ("", "condition", "")):
        v = l.key(key)
        if v and v != "-":
            specs.append(f"{label} {v}{suffix}".strip())
    price = f"{l.price:,.0f}".replace(",", " ") if l.price is not None else "?"
    lines = [
        f"🆕 <b>{price} €</b> · {e(l.city or '')}",
        f"<b>{e(l.title[:150])}</b>",
    ]
    if specs:
        lines.append(e(" · ".join(specs)))
    if extra:
        lines.append(extra)
    lines.append(f"🔎 {e(filter_name)}")
    lines.append(f'<a href="{e(l.url)}">Atvērt ss.com</a>')
    return "\n".join(lines)


def send_listing(l: Listing, filter_name: str, extra: Optional[str] = None) -> bool:
    text = format_listing(l, filter_name, extra)
    if l.photos and len(text) <= CAPTION_LIMIT:
        if _call("sendPhoto", photo=l.photos[0], caption=text):
            return True
        # foto var neielādēties — tad sūtām vismaz tekstu
    return send_text(text)
