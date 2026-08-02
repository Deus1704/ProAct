"""Google Forms generation, signed tokens, and the question design.

The question design is the actual experiment. A survey should only ask what you
can't observe. Whether they ordered is already in the log, so asking re-collects
a variable we have, and worse it feels like ground truth. The primary label has
to be the thing the log can't contain:

    Q1  "If your phone had shown this at that moment, would it have been
         Useful / Neutral / Annoying?"                          <- the label

    Q2  "Were you actually about to do this?"                   <- intent, so
         Yes / No / Don't remember                                 non-fired
                                                                   strata can
                                                                   yield FNs

    Q3  "Did you end up doing it?"                              <- NOT a label.
         Yes / No / Don't remember                                 Verifiable
                                                                   against the
                                                                   log, so it
                                                                   measures
                                                                   answer quality

Q3 is the quiet one. Because its true answer is already known, disagreement
between Q3 and the order log measures whether this participant is reading the
questions, without them knowing they are being checked.

Two tiers, because neither works alone. EMA is one item sent at the moment the
candidate fires: best validity since there's no reconstructing from memory, but
it can only ever cover things that fired, so it can't measure recall. The daily
batch is up to 12 stratified items at a quiet hour and includes the non-fired
strata, which is the only way a false negative shows up. Items that appear in
both let us see how much answers drift with delay.

Google Forms caveats:
 - no hidden fields, so the batch token rides in a prefilled short-answer box
   that the participant can edit. It's HMAC-signed, so editing it invalidates the
   signature and ingest rejects it. Tamper-evident, not tamper-proof, which is
   the right guarantee for a survey.
 - form content goes to Google, so cards carry weekday/time/nickname only, never
   coordinates or addresses or order contents (see capture._context).
 - one form per participant per batch, so responses are attributable without
   collecting an email address.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

UTC = timezone.utc

TOKEN_SECRET_ENV = "PROACT_LABEL_SECRET"
DEFAULT_TTL_HOURS = 72


# ---------------------------------------------------------------------------
# Signed batch tokens
# ---------------------------------------------------------------------------
def _secret() -> bytes:
    s = os.environ.get(TOKEN_SECRET_ENV, "")
    if not s:
        raise RuntimeError(
            f"{TOKEN_SECRET_ENV} is not set. Refusing to mint unsigned label tokens -- "
            "an unsigned token means any link holder can submit labels for any participant."
        )
    return s.encode()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mint_token(participant_id: str, batch_kind: str, ttl_hours: int = DEFAULT_TTL_HOURS) -> tuple[str, str]:
    """Return (token, expires_at_iso).

    Payload carries a nonce so two batches minted in the same second for the same
    participant are still distinct, which matters because `batches.token` is UNIQUE.
    """
    expires = datetime.now(UTC) + timedelta(hours=ttl_hours)
    payload = {
        "p": participant_id,
        "k": batch_kind,
        "e": int(expires.timestamp()),
        "n": secrets.token_urlsafe(8),
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}", expires.isoformat()


def verify_token(token: str) -> dict[str, Any] | None:
    """Constant-time verification. Returns the payload, or None if invalid/expired."""
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except Exception:
        return None
    if int(payload.get("e", 0)) < int(datetime.now(UTC).timestamp()):
        return None
    return payload


# ---------------------------------------------------------------------------
# Item cards
# ---------------------------------------------------------------------------
@dataclass
class ItemCard:
    """What the participant sees for one item."""

    item_id: int
    title: str
    detail: str

    @classmethod
    def from_context(cls, item_id: int, context_json: str, domain_hint: str = "") -> "ItemCard":
        ctx = json.loads(context_json or "{}")
        domain = ctx.get("domain") or domain_hint
        weekday = ctx.get("weekday", "")
        time_s = ctx.get("time", "")
        place = ctx.get("place_label") or ctx.get("restaurant_label") or ""

        if domain == "ride":
            what = f"a ride{f' to {place}' if place else ''}"
        else:
            what = f"an order{f' from {place}' if place else ''}"

        return cls(
            item_id=item_id,
            title=f"{weekday} at {time_s}",
            # Framed as a counterfactual ("if your phone had shown") so the
            # participant judges the interruption, not their own past behaviour.
            detail=f"If your phone had suggested {what} at this moment...",
        )


QUESTIONS = [
    {
        "key": "welcome",
        "title": "...how would that have felt?",
        "options": ["Useful", "Neutral", "Annoying"],
        "required": True,
    },
    {
        "key": "wanted",
        "title": "Were you actually about to do this?",
        "options": ["Yes", "No", "Don't remember"],
        "required": True,
    },
    {
        "key": "did_act",
        "title": "Did you end up doing it that day?",
        "options": ["Yes", "No", "Don't remember"],
        "required": False,
    },
]


# ---------------------------------------------------------------------------
# Google Forms API payloads
# ---------------------------------------------------------------------------
def build_create_request(participant_id: str, batch_kind: str, when: datetime) -> dict[str, Any]:
    """`forms.create` body. Title deliberately carries no personal detail --
    form titles show up in Drive listings and notification previews."""
    label = "Quick check" if batch_kind == "ema" else "Daily review"
    return {
        "info": {
            "title": f"ProAct {label}, {when.strftime('%d %b')}",
            "documentTitle": f"proact-{batch_kind}-{participant_id}-{when.strftime('%Y%m%d%H%M')}",
        }
    }


def build_batch_update_request(cards: list[ItemCard], token: str) -> dict[str, Any]:
    """`forms.batchUpdate` body: the token field, then one block per item.

    Index arithmetic matters here: requests are applied in order and each
    insertion shifts subsequent indices, so the token occupies index 0 and item i
    starts at 1 + i*len(QUESTIONS).
    """
    requests: list[dict[str, Any]] = [
        {
            "createItem": {
                "item": {
                    "title": "Reference code (please do not edit)",
                    "description": "Identifies this review. Editing it will invalidate the submission.",
                    "questionItem": {
                        "question": {
                            "required": True,
                            "textQuestion": {"paragraph": False},
                        }
                    },
                },
                "location": {"index": 0},
            }
        }
    ]

    idx = 1
    for card in cards:
        requests.append(
            {
                "createItem": {
                    "item": {
                        "title": card.title,
                        "description": card.detail,
                        "textItem": {},
                    },
                    "location": {"index": idx},
                }
            }
        )
        idx += 1
        for q in QUESTIONS:
            requests.append(
                {
                    "createItem": {
                        "item": {
                            "title": q["title"],
                            "description": f"[{card.item_id}]",  # parsed back on ingest
                            "questionItem": {
                                "question": {
                                    "required": q["required"],
                                    "choiceQuestion": {
                                        "type": "RADIO",
                                        "options": [{"value": o} for o in q["options"]],
                                    },
                                }
                            },
                        },
                        "location": {"index": idx},
                    }
                }
            )
            idx += 1

    return {"requests": requests, "includeFormInResponse": False}


def prefill_url(form_id: str, token_entry_id: str, token: str) -> str:
    """Prefilled response URL. `entry.<id>` ids come back from the Forms API
    after creation and must be stored on the batch row."""
    from urllib.parse import quote

    return (
        f"https://docs.google.com/forms/d/e/{form_id}/viewform"
        f"?usp=pp_url&entry.{token_entry_id}={quote(token, safe='')}"
    )


# ---------------------------------------------------------------------------
# Offline fallback
# ---------------------------------------------------------------------------
def build_offline_form(cards: list[ItemCard], token: str) -> dict[str, Any]:
    """A self-contained JSON form definition, for when Google is not in the loop.

    This exists because the privacy objection to Google Forms is real: item cards
    leave the device. A locally-served form removes that entirely, and the
    ingestion path in ingest.py accepts either shape. Prefer this if the
    participant is not you.
    """
    return {
        "token": token,
        "questions": [q["key"] for q in QUESTIONS],
        "items": [
            {
                "item_id": c.item_id,
                "title": c.title,
                "detail": c.detail,
                "questions": [
                    {"key": q["key"], "title": q["title"], "options": q["options"],
                     "required": q["required"]}
                    for q in QUESTIONS
                ],
            }
            for c in cards
        ],
    }
