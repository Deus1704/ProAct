"""Ingest form responses and export in the schema eval/run_eval.py already reads.

Takes two shapes so we're not stuck with Google: responses from the Forms API
(item ids recovered from the [123] marker that build_batch_update_request puts in
each question description), or the offline JSON form.

Both go through record_response, which is idempotent per item so a resubmitted
form updates instead of appending. Appending would double-count an opinion, and
since the estimates are weighted, a duplicate in a rare stratum moves the
headline number by several points.

Token is verified before anything gets written.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from .forms import verify_token
from .schema import Response, get_db, upsert_response

UTC = timezone.utc

_ITEM_MARKER = re.compile(r"\[(\d+)\]")

# Free text from a radio button is a closed set; normalise once, here, so that
# every downstream comparison can be a plain equality check.
_WELCOME = {"useful": "useful", "neutral": "neutral", "annoying": "annoying"}
_TERNARY = {
    "yes": "yes",
    "no": "no",
    "don't remember": "dont_remember",
    "dont remember": "dont_remember",
    "do not remember": "dont_remember",
}


class IngestError(Exception):
    pass


def _norm(value: str, table: dict[str, str]) -> str:
    return table.get((value or "").strip().lower(), "")


def _batch_for_token(token: str, labels_db: str | None = None) -> dict[str, Any] | None:
    with get_db(labels_db) as conn:
        row = conn.execute("SELECT * FROM batches WHERE token=?", (token,)).fetchone()
    return dict(row) if row else None


def _items_for_batch(batch_id: int, labels_db: str | None = None) -> set[int]:
    with get_db(labels_db) as conn:
        rows = conn.execute("SELECT item_id FROM items WHERE batch_id=?", (batch_id,)).fetchall()
    return {r["item_id"] for r in rows}


def record_response(
    token: str,
    answers: dict[int, dict[str, Any]],
    answered_at: str | None = None,
    labels_db: str | None = None,
) -> int:
    """Persist answers for one batch. `answers` maps item_id -> field dict.

    Returns the number of responses written. Raises IngestError on an invalid
    token or an item that does not belong to this batch, both indicate either
    tampering or a bug, and neither should silently produce labels.
    """
    payload = verify_token(token)
    if payload is None:
        raise IngestError("token failed verification or has expired")

    batch = _batch_for_token(token, labels_db)
    if batch is None:
        raise IngestError("no batch matches this token")
    if batch["participant_id"] != payload.get("p"):
        raise IngestError("token participant does not match the batch")

    allowed = _items_for_batch(int(batch["batch_id"]), labels_db)
    stamp = answered_at or datetime.now(UTC).isoformat()

    written = 0
    for item_id, fields in answers.items():
        if item_id is None:
            raise IngestError(
                "an answer arrived with no item id, the batch was built without "
                "persisting item ids, so responses cannot be attributed"
            )
        if int(item_id) not in allowed:
            raise IngestError(f"item {item_id} does not belong to batch {batch['batch_id']}")
        upsert_response(
            Response(
                item_id=int(item_id),
                welcome=_norm(fields.get("welcome", ""), _WELCOME),
                wanted=_norm(fields.get("wanted", ""), _TERNARY),
                did_act=_norm(fields.get("did_act", ""), _TERNARY),
                answered_at=stamp,
                latency_ms=int(fields.get("latency_ms") or 0),
                raw_json=json.dumps(fields, sort_keys=True),
            ),
            labels_db,
        )
        written += 1
    return written


# ---------------------------------------------------------------------------
# Google Forms response shape
# ---------------------------------------------------------------------------
def parse_google_response(
    form_response: dict[str, Any],
    question_index: dict[str, tuple[int, str]],
    token_question_id: str,
) -> tuple[str, dict[int, dict[str, Any]]]:
    """Convert one `forms.responses.list` entry into (token, answers).

    `question_index` maps a Forms questionId to (item_id, field_key). It is built
    once per batch from the batchUpdate reply, because the API assigns question
    ids at creation time and they are not predictable in advance.
    """
    answers_in = form_response.get("answers", {}) or {}

    def text_of(qid: str) -> str:
        block = answers_in.get(qid, {})
        vals = (block.get("textAnswers", {}) or {}).get("answers", []) or []
        return vals[0].get("value", "") if vals else ""

    token = text_of(token_question_id).strip()
    if not token:
        raise IngestError("submission carried no reference code")

    out: dict[int, dict[str, Any]] = {}
    for qid, raw in answers_in.items():
        if qid == token_question_id:
            continue
        mapped = question_index.get(qid)
        if not mapped:
            continue
        item_id, field = mapped
        vals = (raw.get("textAnswers", {}) or {}).get("answers", []) or []
        if not vals:
            continue
        out.setdefault(item_id, {})[field] = vals[0].get("value", "")

    return token, out


def build_question_index(
    batch_update_reply: dict[str, Any], cards_in_order: list[int], field_keys: list[str]
) -> tuple[dict[str, tuple[int, str]], str]:
    """Recover questionId -> (item_id, field) from the batchUpdate reply.

    The reply returns one `createItem` result per request, in request order, so
    the mapping is positional: index 0 is the token field, then each card
    contributes one text block (no question) followed by len(field_keys)
    questions.
    """
    replies = batch_update_reply.get("replies", []) or []
    ids: list[str] = []
    for r in replies:
        q = (r.get("createItem", {}) or {}).get("questionId", [])
        ids.append(q[0] if q else "")

    if not ids:
        raise IngestError("batchUpdate reply contained no created items")

    token_qid = ids[0]
    index: dict[str, tuple[int, str]] = {}
    cursor = 1
    for item_id in cards_in_order:
        cursor += 1  # the card's text block carries no questionId
        for key in field_keys:
            if cursor < len(ids) and ids[cursor]:
                index[ids[cursor]] = (item_id, key)
            cursor += 1
    return index, token_qid


def parse_offline_submission(payload: dict[str, Any]) -> tuple[str, dict[int, dict[str, Any]]]:
    """Parse the locally-served form's POST body."""
    token = (payload.get("token") or "").strip()
    if not token:
        raise IngestError("submission carried no token")
    answers: dict[int, dict[str, Any]] = {}
    for entry in payload.get("items", []) or []:
        try:
            item_id = int(entry["item_id"])
        except (KeyError, TypeError, ValueError):
            continue
        answers[item_id] = {
            k: entry.get(k, "") for k in ("welcome", "wanted", "did_act", "latency_ms")
        }
    return token, answers


# ---------------------------------------------------------------------------
# Export into the existing eval harness
# ---------------------------------------------------------------------------
def export_sessions(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Emit real labelled sessions in the schema `eval/run_eval.py` already reads.

    This is the payoff of the whole pipeline: the synthetic generator and the
    human-labelled data produce the *same shape*, so every metric, sweep and
    diagnostic written against the synthetic set runs unchanged against real
    labels. Nothing downstream needs to know which one it is given.

    Label mapping, stated explicitly because it is the arguable step:

        label = 1  if welcome == "useful"        -- the interruption was wanted
        label = 0  if welcome in (neutral, annoying)

    `wanted` is carried through as metadata rather than used as the label. It
    measures intent; "useful" measures whether the *interruption* was justified,
    and those come apart exactly where the product lives, a user can have been
    about to order anyway and still find the nudge redundant.
    """
    sessions = []
    for r in rows:
        if not r.get("welcome"):
            continue  # unanswered
        if r.get("gold_expected") == "__repeat__":
            continue  # reliability only
        ctx = json.loads(r.get("context_json") or "{}")
        sessions.append(
            {
                "session_id": f"L{r['candidate_id']:05d}",
                "domain": r["domain"],
                "label": 1 if r["welcome"] == "useful" else 0,
                "negative_kind": None if r["welcome"] == "useful" else r["stratum"],
                "evaluate_at": r["decided_at"],
                "rides": [],
                "orders": [],
                "dismissals": 0,
                "acted_today": r.get("did_act") == "yes",
                "notes": (
                    f"human-labelled | stratum={r['stratum']} "
                    f"welcome={r['welcome']} wanted={r.get('wanted')} "
                    f"fired={bool(r['fired_prod'])} conf={r['confidence']:.2f}"
                ),
                # Carried so metrics.compute() can weight correctly. The synthetic
                # generator omits it, which is the signal that a dataset is
                # unweighted and may be counted directly.
                "inclusion_prob": r["inclusion_prob"],
                "stratum": r["stratum"],
                "context": ctx,
            }
        )

    return {
        "meta": {
            "count": len(sessions),
            "source": "labeling/ingest.py::export_sessions",
            "labelled_by": "human",
            "weighted": True,
            "label_rule": "welcome == 'useful' -> 1",
            "warning": (
                "Stratified sample: estimates MUST be weighted by 1/inclusion_prob. "
                "Counting rows gives a biased result."
            ),
        },
        "sessions": sessions,
    }
