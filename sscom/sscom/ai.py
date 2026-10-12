"""OpenAI izsaukumi (httpx, bez SDK). Katrs izsaukums tiek pierakstīts ai_usage tabulā."""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy import Column, Integer, String, Table

from . import db

log = logging.getLogger(__name__)

API_URL = "https://api.openai.com/v1/chat/completions"
MODEL_FAST = os.getenv("OPENAI_MODEL_FAST", "gpt-5.4-nano")   # analīze, nosaukumu pārbaude
MODEL_SMART = os.getenv("OPENAI_MODEL_SMART", "gpt-5.4-mini")  # tālākpārdošanas cenas novērtējums

ai_usage = Table(
    "ai_usage", db.metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", db.DateTime, nullable=False),
    Column("model", String(50), nullable=False),
    Column("purpose", String(30), nullable=False),
    Column("prompt_tokens", Integer, nullable=False, default=0),
    Column("completion_tokens", Integer, nullable=False, default=0),
)

PART_TYPES = ["cpu", "gpu", "motherboard", "ram", "storage", "psu", "cooler", "case", "other"]

ITEM_TYPES = ["desktop", "laptop", "cpu", "gpu", "ram", "motherboard", "psu", "storage",
              "case", "cooler", "monitor", "bundle", "other"]

EXTRACT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "item_type": {"type": "string", "enum": ITEM_TYPES},
        "model_key": {"type": "string", "description":
                      "lowercase canonical model that decides resale value, e.g. 'amd ryzen 5 7500f', "
                      "'lenovo thinkpad t480', 'apple macbook air m1'. GPUs: chip + memory only, WITHOUT board "
                      "partner/series (XFX, Sapphire, ASUS TUF, Gaming OC...), e.g. 'amd radeon rx 9070 xt 16gb', "
                      "'nvidia rtx 3060 12gb'. RAM: 'ddr4 16gb 3200'. "
                      "Self-built desktops: main 'cpu + gpu', e.g. 'ryzen 5 5600 + rtx 3060'."},
        "search_query": {"type": "string", "description":
                         "short query to find the NEW product in a price comparison site, model only, "
                         "e.g. 'Ryzen 5 7500F', 'Radeon RX 9070 XT', 'RTX 3060 12GB', 'MacBook Air M1' "
                         "(GPUs: chip only, no board partner). Empty for custom-built desktops."},
        "release_year": {"type": ["integer", "null"], "description": "year this model was first released, best estimate from your knowledge"},
        "cpu": {"type": ["string", "null"]},
        "gpu": {"type": ["string", "null"]},
        "ram_gb": {"type": ["integer", "null"]},
        "storage_gb": {"type": ["integer", "null"]},
        "condition": {"type": "string", "enum": ["new", "like_new", "used", "broken", "for_parts"]},
        "defects": {"type": ["string", "null"], "description": "known problems, missing parts, short"},
        "seller_is_business": {"type": "boolean", "description":
                               "true if the text reads like a shop / reseller / company advert"},
        "urgent": {"type": "boolean", "description":
                   "seller signals hurry: must sell fast, urgent, moving away, needs money, today only"},
        "urgency_note": {"type": ["string", "null"]},
        "delivers_to_riga": {"type": "boolean", "description":
                             "seller offers to bring/meet in Riga (not just parcel shipping)"},
        "components": {"type": "array", "description":
                       "ONLY for desktop PCs/bundles: every sellable part that is stated. Empty list otherwise.",
                       "items": {"type": "object", "additionalProperties": False,
                                 "properties": {
                                     "type": {"type": "string", "enum": PART_TYPES},
                                     "key": {"type": "string", "description":
                                             "same canonical lowercase format as model_key, e.g. "
                                             "'intel core i7-9700kf', 'nvidia rtx 3070 8gb', 'gigabyte z390 aorus pro wifi', "
                                             "'ddr4 32gb 3200', 'nvme 1tb', 'aio 240mm'. 'unknown' if type stated without model"}},
                                 "required": ["type", "key"]}},
    },
    "required": ["item_type", "model_key", "search_query", "release_year", "cpu", "gpu", "ram_gb",
                 "storage_gb", "condition", "defects", "seller_is_business", "urgent",
                 "urgency_note", "delivers_to_riga", "components"],
}

MATCH_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"same_product_indexes": {"type": "array", "items": {"type": "integer"}}},
    "required": ["same_product_indexes"],
}

ESTIMATE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "resale_price_eur": {"type": ["number", "null"], "description":
                             "realistic price a private seller gets for this exact item in Riga within ~2 weeks"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "verdict_lv": {"type": "string", "description": "1-2 short sentences in Latvian: why it is or isn't a good flip"},
    },
    "required": ["resale_price_eur", "confidence", "verdict_lv"],
}


PARTS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "parts": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"key": {"type": "string"},
                           "used_price_eur": {"type": "number", "description":
                                              "realistic price when sold separately on ss.com Riga within ~3 weeks; "
                                              "0 for parts nobody buys"}},
            "required": ["key", "used_price_eur"]}},
        "verdict_lv": {"type": "string", "description": "1-2 short sentences in Latvian about parting this PC out"},
    },
    "required": ["parts", "verdict_lv"],
}


class AI:
    def __init__(self, store: db.Store, api_key: Optional[str] = None):
        self.store = store
        self.api_key = (api_key or os.getenv("OPENAI_API_KEY", "")).strip()
        self.http = httpx.Client(timeout=60)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _call(self, model: str, purpose: str, system: str, user: str,
              schema: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        try:
            r = self.http.post(API_URL, headers={"Authorization": f"Bearer {self.api_key}"}, json={
                "model": model,
                "reasoning_effort": "low",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": purpose, "strict": True, "schema": schema}},
            })
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("OpenAI %s neizdevās: %s", purpose, e)
            return None
        if "error" in data:
            log.warning("OpenAI %s kļūda: %s", purpose, data["error"].get("message", "")[:200])
            return None
        usage = data.get("usage", {})
        with self.store.engine.begin() as c:
            c.execute(ai_usage.insert().values(
                ts=db.now(), model=model, purpose=purpose,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0)))
        try:
            return json.loads(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, ValueError) as e:
            log.warning("OpenAI %s: nesaprotama atbilde: %s", purpose, e)
            return None

    # ---------------------------------------------------------------- 1. analīze

    def extract(self, category: str, subcategory: Optional[str], title: str, description: str,
                params: Dict[str, str], price: Optional[float], city: Optional[str]) -> Optional[Dict[str, Any]]:
        text = (
            f"ss.com category: {category}{'/' + subcategory if subcategory else ''}\n"
            f"Price: {price} EUR\nLocation: {city or '?'}\n"
            f"Parameters: {json.dumps(params, ensure_ascii=False)}\n"
            f"Text:\n{(description or title)[:2500]}"
        )
        return self._call(MODEL_FAST, "extract",
                          "You analyse Latvian/Russian computer-hardware classified ads for a reseller. "
                          "Extract the facts precisely; do not guess specs that are not stated, except release_year.",
                          text, EXTRACT_SCHEMA)

    # ---------------------------------------------------------------- 2. cenueksperts pārbaude

    def match_products(self, query: str, names: List[str]) -> Optional[List[int]]:
        listing = "\n".join(f"{i}: {n[:160]}" for i, n in enumerate(names))
        out = self._call(MODEL_FAST, "match",
                         "Return indexes of shop listings that ARE the product itself (the same model as the query; "
                         "memory size/colour variants are fine). Exclude spare parts, accessories, batteries, "
                         "screens, keyboards, fans, different models.",
                         f"Query: {query}\nListings:\n{listing}", MATCH_SCHEMA)
        return None if out is None else [i for i in out["same_product_indexes"] if 0 <= i < len(names)]

    # ---------------------------------------------------------------- 3. tālākpārdošanas cena

    def estimate_resale(self, facts: Dict[str, Any], price: float, new_price: Optional[Tuple[float, float]],
                        comps: List[float]) -> Optional[Dict[str, Any]]:
        ctx = {
            "item": {k: facts.get(k) for k in ("item_type", "model_key", "release_year", "cpu", "gpu",
                                                "ram_gb", "storage_gb", "condition", "defects")},
            "asking_price_eur": price,
            "new_price_latvia_eur": {"min": new_price[0], "median": new_price[1]} if new_price else None,
            "similar_private_ads_on_ss_com_eur": comps,
        }
        return self._call(MODEL_SMART, "estimate",
                          "You are a used computer hardware reseller in Riga, Latvia (2026). Estimate the realistic "
                          "price a private seller gets on ss.com for this item. Used items sell well below new price; "
                          "old generations lose value fast. Be conservative.",
                          json.dumps(ctx, ensure_ascii=False), ESTIMATE_SCHEMA)

    # ---------------------------------------------------------------- 4. izjaukšanas vērtība

    def estimate_parts(self, components: List[Dict[str, Any]], asking_price: float) -> Optional[Dict[str, Any]]:
        """components: [{"type","key","comps":[cenas ss.com]}]"""
        return self._call(MODEL_SMART, "parts",
                          "You are a used PC parts reseller in Riga, Latvia (2026). For each part estimate the realistic "
                          "price when sold SEPARATELY as used on ss.com. ss.com comparable prices are the best evidence. "
                          "new_price_latvia_min_eur: for parts still in production a used one sells for ~55-75% of it; "
                          "for discontinued parts that price is leftover stock and overstates value. RAM and SSD prices "
                          "are high in 2026, do not undervalue them. Unknown/no-name PSUs and cases are worth little. "
                          "Be realistic, not pessimistic.",
                          json.dumps({"asking_price_for_whole_pc_eur": asking_price, "parts": components},
                                     ensure_ascii=False), PARTS_SCHEMA)
