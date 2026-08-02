"""Sweep the cooldown policy against the labelled hold-out set.

The question this answers
-------------------------
A learned habit window is ~20 minutes wide on each side and the watcher polls
every 60 seconds, so the pattern keeps matching for roughly 40 consecutive
ticks. Without a cooldown the system is correct on every one of them and
notifies on every one of them, 40 pings for one real habit. Recall is perfect
and the product is unusable.

Cooldown is the knob that converts "correct" into "useful". This script measures
what it costs and what it buys, at each setting, on the same 200 sessions.

Everything except the cooldown is held fixed, so any movement in precision is
attributable to the cooldown alone.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run_eval import Metrics, evaluate, load_sessions  # noqa: E402

from proactive_assistant_app.agents import TriggerPolicy  # noqa: E402


async def sweep(
    sessions: list[dict],
    points: list[tuple[int, int, int]],
    poll_seconds: int,
    window_minutes: int,
) -> list[tuple[TriggerPolicy, Metrics]]:
    out = []
    for ride_cd, food_cd, global_cd in points:
        policy = TriggerPolicy(
            ride_cooldown_minutes=ride_cd,
            food_cooldown_minutes=food_cd,
            global_cooldown_minutes=global_cd,
        )
        label = f"ride={ride_cd}m food={food_cd}m global={global_cd}m"
        print(f"  evaluating {label} ...", flush=True)
        metrics = await evaluate(sessions, policy, poll_seconds, window_minutes)
        print(
            f"    precision {metrics.precision * 100:6.2f}%  "
            f"recall {metrics.recall * 100:6.2f}%  "
            f"notifications {metrics.notifications:5d}  "
            f"per-session {metrics.triggers_per_session:6.3f}  "
            f"({metrics.wall_seconds:.0f}s)",
            flush=True,
        )
        out.append((policy, metrics))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dataset", default=os.path.join(os.path.dirname(__file__), "holdout_sessions.json")
    )
    ap.add_argument("--poll-seconds", type=int, default=60)
    ap.add_argument("--window-minutes", type=int, default=45)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--json-out", default=os.path.join(os.path.dirname(__file__), "cooldown_sweep.json")
    )
    args = ap.parse_args()

    sessions, meta = load_sessions(args.dataset)
    if args.limit:
        sessions = sessions[: args.limit]

    # (ride_cooldown, food_cooldown, global_cooldown) in minutes.
    # 0/0/0 is the no-cooldown baseline: the system is allowed to notify on
    # every tick where a pattern matches.
    points = [
        (0, 0, 0),
        (5, 5, 0),
        (15, 10, 0),
        (30, 20, 0),
        (45, 30, 0),      # the shipped default
        (60, 45, 0),
        (90, 60, 0),
        (45, 30, 20),     # default + a cross-domain floor
    ]

    print(f"dataset       : {args.dataset}")
    print(f"  meta        : {meta}")
    print(f"  sessions    : {len(sessions)}")
    print(f"  replay      : every {args.poll_seconds}s across +/-{args.window_minutes} min")
    print(f"  sweep points: {len(points)}\n")

    results = asyncio.run(sweep(sessions, points, args.poll_seconds, args.window_minutes))

    baseline_policy, baseline = results[0]

    print("\n" + "=" * 108)
    print("COOLDOWN SWEEP".center(108))
    print("=" * 108)
    header = (
        f"{'ride':>5} {'food':>5} {'glob':>5} | {'precis':>8} {'recall':>8} {'F1':>8} | "
        f"{'notifs':>7} {'/session':>9} | {'vs baseline: precision':>24} {'notif freq':>12}"
    )
    print(header)
    print("-" * 108)
    for policy, m in results:
        d_prec = (m.precision - baseline.precision) * 100
        if baseline.notifications:
            d_freq = (m.notifications - baseline.notifications) / baseline.notifications * 100
        else:
            d_freq = 0.0
        tag = "  <- shipped default" if (
            policy.ride_cooldown_minutes == 45
            and policy.food_cooldown_minutes == 30
            and policy.global_cooldown_minutes == 0
        ) else ""
        print(
            f"{policy.ride_cooldown_minutes:>5} {policy.food_cooldown_minutes:>5} "
            f"{policy.global_cooldown_minutes:>5} | "
            f"{m.precision * 100:7.2f}% {m.recall * 100:7.2f}% {m.f1 * 100:7.2f}% | "
            f"{m.notifications:>7} {m.triggers_per_session:>9.3f} | "
            f"{d_prec:>+22.2f}pp {d_freq:>+11.1f}%{tag}"
        )
    print("-" * 108)
    print("baseline = no cooldown (0/0/0). 'pp' = percentage points.")
    print("Recall is the number to watch while reading precision: a cooldown that")
    print("also drops recall is not tuning, it is just switching the feature off.")

    payload = {
        "meta": meta,
        "sessions_evaluated": len(sessions),
        "poll_seconds": args.poll_seconds,
        "window_minutes": args.window_minutes,
        "baseline": {"policy": baseline_policy.as_dict(), "metrics": baseline.as_dict()},
        "points": [{"policy": p.as_dict(), "metrics": m.as_dict()} for p, m in results],
    }
    with open(args.json_out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    main()
