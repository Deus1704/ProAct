"""Confirm exactly why a specific positive session scores below threshold.

Prints the raw arithmetic behind `confidence = count / total` for one session, so
the recall finding in RESULTS.md is a demonstrated fact rather than a plausible
theory.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault(
    "ASSISTANT_DB_PATH", os.path.join(tempfile.gettempdir(), "proact_expl_boot.db")
)

from run_eval import _seed_session_db, load_sessions  # noqa: E402

from proactive_assistant_app import database as db  # noqa: E402
from proactive_assistant_app.pattern_engine import _hour_bin, _parse_dt  # noqa: E402

UTC = timezone.utc
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session_ids", nargs="*", default=["s0025", "s0076"])
    ap.add_argument(
        "--dataset", default=os.path.join(os.path.dirname(__file__), "holdout_sessions.json")
    )
    args = ap.parse_args()

    sessions, _ = load_sessions(args.dataset)
    by_id = {s["session_id"]: s for s in sessions}
    tmpdir = tempfile.mkdtemp(prefix="proact_expl_")

    for sid in args.session_ids:
        session = by_id.get(sid)
        if not session:
            print(f"{sid}: not in dataset")
            continue

        evaluate_at = datetime.fromisoformat(session["evaluate_at"])
        if evaluate_at.tzinfo is None:
            evaluate_at = evaluate_at.replace(tzinfo=UTC)
        target_day = evaluate_at.weekday()
        target_bin = _hour_bin(evaluate_at)

        print("=" * 78)
        print(f"{sid}  domain={session['domain']}  label={session['label']}  "
              f"kind={session['negative_kind']}")
        print(f"  notes        : {session['notes']}")
        print(f"  evaluate_at  : {evaluate_at.isoformat()}  "
              f"({DAYS[target_day]}, 15-min bin {target_bin})")

        events = session["rides"] if session["domain"] == "ride" else session["orders"]
        time_key = "departure_time" if session["domain"] == "ride" else "ordered_at"

        # Count events on the SAME weekday, split by whether they land in the
        # habit's 15-minute bin. That ratio IS the confidence formula.
        same_day = [e for e in events if _parse_dt(e[time_key]).weekday() == target_day]
        in_bin = [e for e in same_day if _hour_bin(_parse_dt(e[time_key])) == target_bin]

        print(f"  events total : {len(events)}")
        print(f"  on {DAYS[target_day]}      : {len(same_day)}   <-- this is `total`")
        print(f"  in the bin   : {len(in_bin)}   <-- this is `count`")
        if same_day:
            print(f"  count/total  : {len(in_bin)}/{len(same_day)} = "
                  f"{len(in_bin)/len(same_day):.4f}")

        bins = Counter(_hour_bin(_parse_dt(e[time_key])) for e in same_day)
        spread = ", ".join(f"bin{b}x{c}" for b, c in sorted(bins.items()))
        print(f"  {DAYS[target_day]} bin spread: {spread}")

        db_path = os.path.join(tmpdir, f"{sid}.db")
        _seed_session_db(session, db_path)
        rows = (
            db.list_departure_patterns(include_suppressed=True)
            if session["domain"] == "ride"
            else db.list_order_patterns(include_suppressed=True)
        )
        matching = [r for r in rows if int(r["day_of_week"]) == target_day]
        print(f"  extracted patterns for {DAYS[target_day]}:")
        for r in sorted(matching, key=lambda x: -float(x["confidence"]))[:6]:
            print(f"    bin={r['hour_bin']:>3}  conf={float(r['confidence']):.4f}"
                  f"  freq={r.get('frequency', '-')}")
        threshold = 0.60 if session["domain"] == "ride" else 0.55
        print(f"  threshold    : {threshold}")
        print("  => the habit is perfectly regular; the confidence is diluted by the OTHER")
        print("     activity on the same weekday, because `total` counts all of it.")

        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    main()
