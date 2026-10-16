"""Telegram ziņas. Kļūda sūtīšanā nekad nenogāž skrāpi — tikai žurnālā.

send_* atgriež Telegram message_id (vai None). Sludinājumu ziņām pievieno pogas
(✅ Nopirku / 👎 Neinteresē) un piefiksē message_id -> ss_id (on_sent āķis), lai uz ziņu
var atbildēt ar jautājumu (tgbot.py).
"""
from __future__ import annotations

import html
import json
import logging
from typing import Any, Callable, Dict, List, Optional

import httpx

from . import config
from .parser import Listing

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
CAPTION_LIMIT = 1024

# tgbot.py iestata: (message_id, ss_id, kind, ref) -> None
on_sent: Optional[Callable[[int, Optional[str], str, Optional[int]], None]] = None


def _call(method: str, timeout: float = 30, with_chat: bool = True, **data) -> Optional[Any]:
    if not config.TELEGRAM_BOT_TOKEN or (with_chat and not config.TELEGRAM_CHAT_ID):
        log.warning("Telegram nav konfigurēts (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        return None
    payload = {"parse_mode": "HTML", **data} if method.startswith(("send", "edit")) else dict(data)
    if with_chat:
        payload.setdefault("chat_id", config.TELEGRAM_CHAT_ID)
    try:
        r = httpx.post(API.format(token=config.TELEGRAM_BOT_TOKEN, method=method), data=payload, timeout=timeout)
        body = r.json()
        if r.status_code != 200 or not body.get("ok"):
            log.warning("Telegram %s: %s %s", method, r.status_code, r.text[:200])
            return None
        return body.get("result", True)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Telegram %s neizdevās: %s", method, e)
        return None


def _markup(rows: Optional[List[List[Dict[str, str]]]]) -> Dict[str, str]:
    return {"reply_markup": json.dumps({"inline_keyboard": rows})} if rows else {}


def listing_buttons(ss_id: Optional[str]) -> Optional[List[List[Dict[str, str]]]]:
    if not ss_id:
        return None
    return [[{"text": "✅ Nopirku", "callback_data": f"buy:{ss_id}"},
             {"text": "👎 Neinteresē", "callback_data": f"skip:{ss_id}"}]]


def send_text(text: str, reply_to: Optional[int] = None, buttons=None,
              ss_id: Optional[str] = None, kind: str = "text", ref: Optional[int] = None) -> Optional[int]:
    extra = {"reply_to_message_id": reply_to} if reply_to else {}
    res = _call("sendMessage", text=text, disable_web_page_preview="true", **extra, **_markup(buttons))
    mid = res.get("message_id") if isinstance(res, dict) else None
    if mid and on_sent:
        on_sent(mid, ss_id, kind, ref)
    return mid


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


def send_listing(l: Listing, filter_name: str, extra: Optional[str] = None,
                 text: Optional[str] = None, reply_to: Optional[int] = None) -> Optional[int]:
    """Sludinājuma ziņa ar foto (ja ir) un pogām. Atgriež message_id vai None."""
    text = text or format_listing(l, filter_name, extra)
    buttons = listing_buttons(l.ss_id)
    if l.photos and len(text) <= CAPTION_LIMIT:
        extra_args = {"reply_to_message_id": reply_to} if reply_to else {}
        res = _call("sendPhoto", photo=l.photos[0], caption=text, **extra_args, **_markup(buttons))
        if isinstance(res, dict) and res.get("message_id"):
            if on_sent:
                on_sent(res["message_id"], l.ss_id, "listing", None)
            return res["message_id"]
        # foto var neielādēties — tad sūtām vismaz tekstu
    return send_text(text, reply_to=reply_to, buttons=buttons, ss_id=l.ss_id, kind="listing")


def answer_callback(callback_id: str, text: str = "") -> None:
    _call("answerCallbackQuery", with_chat=False, callback_query_id=callback_id, text=text)


def get_updates(offset: Optional[int], timeout: int = 30) -> List[Dict[str, Any]]:
    data = {"timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query"])}
    if offset is not None:
        data["offset"] = offset
    res = _call("getUpdates", timeout=timeout + 10, with_chat=False, **data)
    return res if isinstance(res, list) else []


def download_file(file_id: str) -> Optional[bytes]:
    """Telegram faila (ekrānuzņēmuma) saturs. Saite satur bota tokenu — to nekur tālāk nedodam."""
    info = _call("getFile", with_chat=False, file_id=file_id)
    if not isinstance(info, dict) or not info.get("file_path"):
        return None
    try:
        r = httpx.get(f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/{info['file_path']}", timeout=30)
        return r.content if r.status_code == 200 else None
    except httpx.HTTPError:
        return None
