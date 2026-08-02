"""Why do 3 of the 20 `dismissed_pattern` negatives still fire?

Every false positive left at the tuned cooldown comes from `dismissed_pattern`
sessions, the four other negative flavours never leak. Hypothesis: this is the
bin-splitting defect surfacing a second way. If a habit straddles a 15-minute bin
boundary it becomes TWO patterns with their own IDs, and dismissal counts are keyed
per pattern ID. So dismissing the suggestion for bin 79 leaves bin 80 completely
unsuppressed, and the same suggestion comes back under a different ID.

If that is right, the leaking sessions should be exactly the ones whose habit is
split across adjacent bins with a rival pattern of comparable confidence.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault(
    "ASSISTANT_DB_PATH", os.path.join(tempfile.gettempdir(), "proact_dis_boot.db")
)

from run_eval import _seed_dismissals, _seed_session_db, load_sessions  # noqa: E402

from proactive_assistant_app import database as db  # noqa: E402
from proactive_assistant_app.agents import TriggerPolicy  # noqa: E402

UTC = timezone.utc


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dataset", default=os.path.join(os.path.dirname(__file__), "holdout_sessions.json")
    )
    args = ap.parse_args()

    sessions, _ = load_sessions(args.dataset)
    targets = [s for s in sessions if s.get("negative_kind") == "dismissed_pattern"]
    policy = TriggerPolicy()
    tmpdir = tempfile.mkdtemp(prefix="proact_dis_")

    split = 0
    single = 0
    print(f"{'session':>8} {'dom':>5} {'#pat':>5} {'seeded ref':>22} {'agent would pick':>22} "
          f"{'dismissals seen':>16}  verdict")
    print("-" * 104)

    for session in targets:
        evaluate_at = datetime.fromisoformat(session["evaluate_at"])
        if evaluate_at.tzinfo is None:
            evaluate_at = evaluate_at.replace(tzinfo=UTC)
        db_path = os.path.join(tmpdir, f"{session['session_id']}.db")
        _seed_session_db(session, db_path)

        domain = session["domain"]
        table = "departure_patterns" if domain == "ride" else "order_patterns"
        lister = db.list_departure_patterns if domain == "ride" else db.list_order_patterns

        # What _seed_dismissals targets: highest confidence.
        all_pats = lister(include_suppressed=True)
        all_pats_sorted = sorted(all_pats, key=lambda r: -float(r.get("confidence") or 0))
        seeded_ref = f"{table}:{all_pats_sorted[0]['id']}" if all_pats_sorted else "-"

        _seed_dismissals(session, evaluate_at)

        # What the agent picks: highest confidence AMONG PATTERNS MATCHING NOW.
        minute_of_day = evaluate_at.hour * 60 + evaluate_at.minute
        if domain == "ride":
            matches = db.find_matching_departure_patterns(
                evaluate_at.weekday(), minute_of_day, policy.ride_tolerance_minutes
            )
            matches.sort(key=lambda r: (-r["confidence"], -r["frequency"], r["last_seen"]))
        else:
            matches = db.find_matching_order_patterns(
                evaluate_at.weekday(), minute_of_day, policy.food_tolerance_minutes
            )
            matches.sort(key=lambda r: (-r["confidence"], r["last_seen"]))

        picked_ref = f"{table}:{matches[0]['id']}" if matches else "-"
        seen = (
            db.count_recent_dismissals_for_pattern(picked_ref, days=7, now=evaluate_at)
            if matches
            else 0
        )

        leaks = bool(matches) and seen < policy.dismissals_before_suppress
        if leaks:
            split += 1
            verdict = f"LEAKS (only {seen} dismissals on the picked pattern)"
        else:
            single += 1
            verdict = "suppressed"

        print(f"{session['session_id']:>8} {domain:>5} {len(matches):>5} {seeded_ref:>22} "
              f"{picked_ref:>22} {seen:>16}  {verdict}")

        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass

    print("-" * 104)
    print(f"dismissed_pattern sessions : {len(targets)}")
    print(f"  suppressed correctly     : {single}")
    print(f"  LEAK a notification      : {split}")
    print()
    print("When the seeded ref and the picked ref differ, the habit was split across")
    print("adjacent 15-minute bins into two patterns with separate IDs. Dismissal counts")
    print("are keyed per pattern ID, so dismissing one leaves the twin unsuppressed and")
    print("the suggestion returns under a different ID, the SAME bin-boundary defect")
    print("that caps recall, surfacing a second time as a user-visible annoyance bug.")


if __name__ == "__main__":
    main()
