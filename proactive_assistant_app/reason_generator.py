"""Groq is only used here for concise human-readable reason strings."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from groq import Groq

log = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()


def generate_reason(trigger_event: dict[str, Any], suggestion: dict[str, Any]) -> str:
    reasons = trigger_event.get("trigger_reason") or []
    if not GROQ_API_KEY:
        return _fallback_reason(reasons, suggestion)

    prompt = (
        "Given these signals: "
        f"{json.dumps({'trigger_reasons': reasons, 'suggestion': suggestion}, ensure_ascii=True)} "
        "generate ONE sentence (max 15 words) explaining why this suggestion is shown. "
        "Be specific, not generic. Do not say 'I noticed' or 'Based on'. Just state the fact."
    )
    try:
        client = Groq(api_key=GROQ_API_KEY)
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            temperature=0.2,
            max_tokens=40,
            messages=[
                {"role": "system", "content": "Return one sentence only."},
                {"role": "user", "content": prompt},
            ],
        )
        content = (response.choices[0].message.content or "").strip()
        return content[:120] or _fallback_reason(reasons, suggestion)
    except Exception as exc:
        log.warning("Groq reason generation failed: %s", exc)
        return _fallback_reason(reasons, suggestion)


def _fallback_reason(reasons: list[str], suggestion: dict[str, Any]) -> str:
    if "traffic_deviation" in reasons and suggestion.get("type") == "ride":
        return "Traffic is heavier than usual on your regular route."
    if suggestion.get("type") == "food":
        restaurant = suggestion.get("restaurant_name") or "this restaurant"
        return f"You often order from {restaurant} around this time."
    destination = suggestion.get("destination_label") or "your usual destination"
    return f"You usually leave for {destination} around now."

