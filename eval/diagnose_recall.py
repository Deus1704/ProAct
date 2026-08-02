"""Why does a positive session fail to produce a notification?

Recall sits at 75% across every cooldown setting, which is expected (a cooldown
cannot suppress the *first* fire of a session) but leaves 25% of genuine habits
unnotified. "Recall is 75%" is not an acceptable thing to state without knowing
which stage drops them, so this walks each failing positive session and reports
the first stage that rejected it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault(
    "ASSISTANT_DB_PATH", os.path.join(tempfile.gettempdir(), "proact_diag_boot.db")
)

from run_eval import _seed_dismissals, _seed_session_db, load_sessions  # noqa: E402

from proactive_assistant_app import database as db  # noqa: E402
from proactive_assistant_app.agents import (  # noqa: E402
    DecisionAgent,
    Orchestrator,
    TriggerPolicy,
)
from proactive_assistant_app.pattern_engine import PatternEngine  # noqa: E402

UTC = timezone.utc


class _NullEngine:
    def reweight(self, feedback):
        pass


async def diagnose(sessions, policy: TriggerPolicy, window_minutes: int, poll_seconds: int):
    reasons: Counter[str] = Counter()
    details: list[str] = []
    tmpdir = tempfile.mkdtemp(prefix="proact_diag_")

    for index, session in enumerate(sessions):
        if session["label"] != 1:
            continue

        db_path = os.path.join(tmpdir, f"d{index}.db")
        evaluate_at = datetime.fromisoformat(session["evaluate_at"])
        if evaluate_at.tzinfo is None:
            evaluate_at = evaluate_at.replace(tzinfo=UTC)

        _seed_session_db(session, db_path)
        _seed_dismissals(session, evaluate_at)

        domain = session["domain"]

        # Stage A: did extraction produce any pattern at all?
        patterns = (
            db.list_departure_patterns(include_suppressed=True)
            if domain == "ride"
            else db.list_order_patterns(include_suppressed=True)
        )
        if not patterns:
            reasons["A: extraction produced no pattern"] += 1
            details.append(f"{session['session_id']} {domain}: no pattern extracted")
            continue

        # Stage B: does any pattern match the evaluation moment?
        minute_of_day = evaluate_at.hour * 60 + evaluate_at.minute
        if domain == "ride":
            matches = db.find_matching_departure_patterns(
                evaluate_at.weekday(), minute_of_day, policy.ride_tolerance_minutes
            )
        else:
            matches = db.find_matching_order_patterns(
                evaluate_at.weekday(), minute_of_day, policy.food_tolerance_minutes
            )
        if not matches:
            best = max(float(p.get("confidence") or 0) for p in patterns)
            reasons["B: pattern exists but none matches this weekday/bin"] += 1
            details.append(
                f"{session['session_id']} {domain}: {len(patterns)} patterns, none in window "
                f"(dow={evaluate_at.weekday()} min={minute_of_day} best_conf={best:.2f})"
            )
            continue

        # Stage C: does policy reject the matching candidate?
        orch = Orchestrator(
            pattern_engine=_NullEngine(),
            suggestion_builder=None,
            broadcaster=None,
            policy=policy,
            live_context_enabled=False,
        )
        await orch.start()
        fired = False
        last_reason = "?"
        try:
            current = evaluate_at
            # A single tick at the exact moment is enough to classify the stage.
            result = await orch.tick(now=current)
            fired = result is not None
            if not fired:
                # Re-derive the verdict synchronously to get its reason string.
                agent = DecisionAgent(asyncio.Queue(), lambda: policy)
                matches.sort(key=lambda r: -float(r["confidence"]))
                p = matches[0]
                from proactive_assistant_app.agents import Candidate

                cand = Candidate(
                    domain=domain,
                    pattern_ref=f"{'departure' if domain == 'ride' else 'order'}_patterns:{p['id']}",
                    confidence=float(p["confidence"]),
                    trigger_reasons=["x"],
                    fired_at=current,
                    hour_bin=int(p["hour_bin"]),
                    features={
                        "destination_id": p.get("destination_id"),
                        "restaurant_id": p.get("restaurant_id"),
                        "already_suppressed": bool(p.get("suppressed")),
                    },
                )
                last_reason = agent.evaluate(cand, current).reason
        finally:
            await orch.stop()

        if not fired:
            key = "C: policy rejected -- " + last_reason.split(":")[0]
            reasons[key] += 1
            details.append(
                f"{session['session_id']} {domain}: {last_reason} "
                f"(conf={float(matches[0]['confidence']):.2f})"
            )

        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass

    return reasons, details


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dataset", default=os.path.join(os.path.dirname(__file__), "holdout_sessions.json")
    )
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--window-minutes", type=int, default=45)
    ap.add_argument("--poll-seconds", type=int, default=60)
    args = ap.parse_args()

    sessions, _ = load_sessions(args.dataset)
    if args.limit:
        sessions = sessions[: args.limit]
    positives = [s for s in sessions if s["label"] == 1]

    policy = TriggerPolicy()
    reasons, details = asyncio.run(
        diagnose(sessions, policy, args.window_minutes, args.poll_seconds)
    )

    total_failures = sum(reasons.values())
    print(f"positive sessions examined : {len(positives)}")
    print(f"failed to notify           : {total_failures}")
    print(f"=> recall                  : {(len(positives) - total_failures) / len(positives) * 100:.1f}%")
    print("\nfirst stage that dropped them:")
    for reason, count in reasons.most_common():
        print(f"  {count:3d}  {reason}")
    print("\nsamples:")
    for line in details[:15]:
        print(f"  {line}")


if __name__ == "__main__":
    main()
