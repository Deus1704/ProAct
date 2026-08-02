"""Build a labelled hold-out set of behavioural sessions for trigger evaluation.

What a "session" is
-------------------
One evaluation session = (a synthetic user's ride/order history, a specific
moment in time, a ground-truth label saying whether a proactive notification is
warranted at that moment).

Why synthetic
-------------
The ground truth here is "would this user have wanted a nudge right now", which
does not exist in scraped Uber/Swiggy history, that history records what the
user *did*, not what they wanted to be reminded about. So the label has to be
constructed. The generator plants a known behavioural regularity, then asks the
system to rediscover it:

  POSITIVE session, the user has a genuine habit (same weekday, same 15-minute
      window, same destination/restaurant, repeated enough times to be a
      pattern), the evaluation clock sits inside that window, and the user has
      NOT already taken the action today. A notification is wanted. label=1.

  NEGATIVE session, one of the preconditions is deliberately broken. Five
      distinct flavours, because "should not fire" has more than one cause and a
      harness that only tests one of them overstates precision:
        no_habit          - history is scattered, no repeated window exists
        wrong_time        - a real habit exists, but the clock is far from it
        already_acted     - real habit, right time, but the user already went
        weak_habit        - the window repeats too few times to be trustworthy
        dismissed_pattern - real habit, right time, but the user has said no
                            repeatedly, so we must stay quiet

Determinism
-----------
Everything is derived from a single seed, so the dataset is byte-identical on
every machine and a metric change can only come from a code change. The split
is stratified so positives and each negative flavour appear in fixed proportion.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

UTC = timezone.utc

# A fixed anchor so "today" never drifts and the dataset is reproducible.
ANCHOR = datetime(2026, 3, 2, 0, 0, 0, tzinfo=UTC)  # a Monday

NEGATIVE_KINDS = ["no_habit", "wrong_time", "already_acted", "weak_habit", "dismissed_pattern"]

# Bengaluru-ish coordinates; only relative distance matters for clustering.
PLACES = [
    ("Office", 12.9716, 77.5946),
    ("Home", 12.9352, 77.6245),
    ("Airport", 13.1986, 77.7066),
    ("Gym", 12.9611, 77.6387),
    ("Mall", 12.9784, 77.6408),
    ("Station", 12.9767, 77.5713),
]

RESTAURANTS = [
    ("r-truffles", "Truffles", "Continental"),
    ("r-meghana", "Meghana Foods", "Andhra"),
    ("r-empire", "Empire Restaurant", "Indian"),
    ("r-sushi", "Sushi Counter", "Japanese"),
    ("r-pizza", "Pizza Bakery", "Italian"),
]


@dataclass
class Session:
    session_id: str
    domain: str                      # "ride" | "food"
    label: int                       # 1 = a notification is warranted
    negative_kind: str | None        # which precondition was broken
    evaluate_at: str                 # ISO timestamp: when the tick happens
    rides: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    dismissals: int = 0              # pre-seeded dismissals for this pattern
    acted_today: bool = False
    notes: str = ""


def _jitter(rng: random.Random, minutes: int) -> int:
    """Real habits are not to-the-second. Jitter within a 15-minute bin keeps the
    pattern discoverable; jitter beyond it would destroy the thing we are asking
    the system to find."""
    return rng.randint(-minutes, minutes)


def _ride(
    idx: int,
    when: datetime,
    origin: tuple[str, float, float],
    dest: tuple[str, float, float],
    rng: random.Random,
) -> dict[str, Any]:
    # Field names match what database.insert_ride() actually reads.
    return {
        "external_ride_id": f"ride-{idx}",
        "source_platform": "uber",
        "departure_time": when.isoformat(),
        "origin_label": origin[0],
        "origin_lat": origin[1] + rng.uniform(-0.001, 0.001),
        "origin_lng": origin[2] + rng.uniform(-0.001, 0.001),
        "dest_label": dest[0],
        "dest_lat": dest[1] + rng.uniform(-0.001, 0.001),
        "dest_lng": dest[2] + rng.uniform(-0.001, 0.001),
        "fare": round(rng.uniform(120, 480), 2),
        "currency": "INR",
        "duration_min": rng.randint(18, 42),
        "distance_km": round(rng.uniform(4, 18), 2),
        "ride_type": "UberGo",
        "status": "COMPLETED",
    }


def _order(
    idx: int, when: datetime, restaurant: tuple[str, str, str], rng: random.Random
) -> dict[str, Any]:
    # insert_food_order() coerces the external id to an int for the primary key,
    # so it must be numeric or the row silently gets a NULL id.
    return {
        "external_order_id": 100000 + idx,
        "source_platform": "swiggy",
        "ordered_at": when.isoformat(),
        "restaurant_id": restaurant[0],
        "restaurant_name": restaurant[1],
        "cuisine": restaurant[2],
        "items_json": json.dumps([{"name": "Usual", "quantity": 1}]),
        "total_price": round(rng.uniform(180, 720), 2),
        "currency": "INR",
        "delivery_time_min": rng.randint(24, 46),
        "status": "DELIVERED",
    }


def _habit_times(
    rng: random.Random, weeks: int, weekday: int, hour: int, minute: int, jitter_min: int = 6
) -> list[datetime]:
    """One recurring event per week on the same weekday and 15-minute window."""
    times = []
    for w in range(weeks):
        # Walk backwards from the anchor so all history is in the past.
        day = ANCHOR - timedelta(weeks=(weeks - w))
        day = day + timedelta(days=(weekday - day.weekday()) % 7)
        t = day.replace(hour=hour, minute=minute) + timedelta(minutes=_jitter(rng, jitter_min))
        times.append(t)
    return times


def _noise_rides(rng: random.Random, count: int, start_idx: int) -> list[dict[str, Any]]:
    """Background rides at scattered times, so the habit has to be found against
    a realistic amount of irrelevant history rather than in a vacuum."""
    out = []
    for i in range(count):
        day = ANCHOR - timedelta(days=rng.randint(1, 60))
        t = day.replace(hour=rng.randint(6, 23), minute=rng.randint(0, 59))
        o, d = rng.sample(PLACES, 2)
        out.append(_ride(start_idx + i, t, o, d, rng))
    return out


def _noise_orders(rng: random.Random, count: int, start_idx: int) -> list[dict[str, Any]]:
    out = []
    for i in range(count):
        day = ANCHOR - timedelta(days=rng.randint(1, 60))
        t = day.replace(hour=rng.randint(11, 23), minute=rng.randint(0, 59))
        out.append(_order(start_idx + i, t, rng.choice(RESTAURANTS), rng))
    return out


def _make_ride_session(sid: str, rng: random.Random, label: int, kind: str | None) -> Session:
    weekday = rng.randint(0, 4)
    hour = rng.choice([8, 9, 10, 18, 19, 20])
    minute = rng.choice([0, 15, 30, 45])
    origin, dest = rng.sample(PLACES, 2)

    # A "weak" habit repeats too few times to clear the confidence bar.
    weeks = 2 if kind == "weak_habit" else rng.randint(6, 10)
    rides: list[dict[str, Any]] = []
    notes = ""

    if kind == "no_habit":
        # Pure noise: no repeated (weekday, window, destination) triple at all.
        rides = _noise_rides(rng, rng.randint(18, 30), 0)
        evaluate_at = (ANCHOR + timedelta(days=weekday)).replace(hour=hour, minute=minute)
        notes = "scattered history, no recurring window"
    else:
        for i, t in enumerate(_habit_times(rng, weeks, weekday, hour, minute)):
            rides.append(_ride(i, t, origin, dest, rng))
        rides += _noise_rides(rng, rng.randint(6, 14), len(rides))
        evaluate_at = (ANCHOR + timedelta(days=weekday)).replace(hour=hour, minute=minute)

        if kind == "wrong_time":
            # Same habit, but evaluate ~5 hours away from the learned window:
            # well outside the 20-minute tolerance.
            shift = rng.choice([-5, -4, 4, 5])
            evaluate_at = evaluate_at + timedelta(hours=shift)
            notes = f"real habit, clock shifted {shift}h outside tolerance"
        elif kind == "weak_habit":
            notes = f"habit repeated only {weeks}x -> low confidence"
        elif kind == "dismissed_pattern":
            notes = "real habit at the right time, but repeatedly dismissed"
        elif kind == "already_acted":
            notes = "real habit at the right time, but already booked today"
        else:
            notes = f"habit {weeks}x on weekday {weekday} at {hour:02d}:{minute:02d}"

    session = Session(
        session_id=sid,
        domain="ride",
        label=label,
        negative_kind=kind,
        evaluate_at=evaluate_at.isoformat(),
        rides=rides,
        dismissals=4 if kind == "dismissed_pattern" else 0,
        acted_today=(kind == "already_acted"),
        notes=notes,
    )

    if kind == "already_acted":
        # Insert a ride today inside the window: the user already left.
        session.rides.append(
            _ride(len(rides) + 500, evaluate_at - timedelta(minutes=4), origin, dest, rng)
        )
    return session


def _make_food_session(sid: str, rng: random.Random, label: int, kind: str | None) -> Session:
    weekday = rng.randint(0, 6)
    hour = rng.choice([13, 14, 20, 21])
    minute = rng.choice([0, 15, 30, 45])
    restaurant = rng.choice(RESTAURANTS)
    weeks = 2 if kind == "weak_habit" else rng.randint(6, 10)
    orders: list[dict[str, Any]] = []
    notes = ""

    if kind == "no_habit":
        orders = _noise_orders(rng, rng.randint(16, 28), 0)
        evaluate_at = (ANCHOR + timedelta(days=weekday)).replace(hour=hour, minute=minute)
        notes = "scattered orders, no recurring window"
    else:
        for i, t in enumerate(_habit_times(rng, weeks, weekday, hour, minute)):
            orders.append(_order(i, t, restaurant, rng))
        orders += _noise_orders(rng, rng.randint(5, 12), len(orders))
        evaluate_at = (ANCHOR + timedelta(days=weekday)).replace(hour=hour, minute=minute)

        if kind == "wrong_time":
            shift = rng.choice([-6, -5, 5, 6])
            evaluate_at = evaluate_at + timedelta(hours=shift)
            notes = f"real habit, clock shifted {shift}h outside tolerance"
        elif kind == "weak_habit":
            notes = f"habit repeated only {weeks}x -> low confidence"
        elif kind == "dismissed_pattern":
            notes = "real habit at the right time, but repeatedly dismissed"
        elif kind == "already_acted":
            notes = "real habit at the right time, but already ordered today"
        else:
            notes = f"habit {weeks}x on weekday {weekday} at {hour:02d}:{minute:02d}"

    session = Session(
        session_id=sid,
        domain="food",
        label=label,
        negative_kind=kind,
        evaluate_at=evaluate_at.isoformat(),
        orders=orders,
        dismissals=4 if kind == "dismissed_pattern" else 0,
        acted_today=(kind == "already_acted"),
        notes=notes,
    )

    if kind == "already_acted":
        session.orders.append(
            _order(len(orders) + 500, evaluate_at - timedelta(minutes=5), restaurant, rng)
        )
    return session


def generate(n: int = 200, seed: int = 20260730, positive_ratio: float = 0.5) -> list[Session]:
    """Stratified: `positive_ratio` positives, the rest split evenly across the
    five negative flavours, alternating ride/food so neither domain dominates."""
    rng = random.Random(seed)
    n_pos = int(round(n * positive_ratio))
    n_neg = n - n_pos

    plan: list[tuple[int, str | None]] = [(1, None)] * n_pos
    per_kind = n_neg // len(NEGATIVE_KINDS)
    remainder = n_neg - per_kind * len(NEGATIVE_KINDS)
    for i, kind in enumerate(NEGATIVE_KINDS):
        count = per_kind + (1 if i < remainder else 0)
        plan += [(0, kind)] * count

    # Shuffle with the seeded rng so ordering is fixed but not grouped.
    rng.shuffle(plan)

    sessions: list[Session] = []
    for i, (label, kind) in enumerate(plan):
        sid = f"s{i:04d}"
        if i % 2 == 0:
            sessions.append(_make_ride_session(sid, rng, label, kind))
        else:
            sessions.append(_make_food_session(sid, rng, label, kind))
    return sessions


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-n", "--count", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20260730)
    ap.add_argument("--positive-ratio", type=float, default=0.5)
    ap.add_argument(
        "-o",
        "--out",
        default=os.path.join(os.path.dirname(__file__), "holdout_sessions.json"),
    )
    args = ap.parse_args()

    sessions = generate(args.count, args.seed, args.positive_ratio)
    payload = {
        "meta": {
            "count": len(sessions),
            "seed": args.seed,
            "anchor": ANCHOR.isoformat(),
            "positive_ratio": args.positive_ratio,
            "generator": "eval/generate_dataset.py",
        },
        "sessions": [asdict(s) for s in sessions],
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)

    pos = sum(1 for s in sessions if s.label == 1)
    print(f"wrote {len(sessions)} sessions -> {args.out}")
    print(f"  positives : {pos}")
    print(f"  negatives : {len(sessions) - pos}")
    by_kind: dict[str, int] = {}
    for s in sessions:
        key = s.negative_kind or "positive"
        by_kind[key] = by_kind.get(key, 0) + 1
    for k in sorted(by_kind):
        print(f"    {k:<20} {by_kind[k]}")
    rides = sum(1 for s in sessions if s.domain == "ride")
    print(f"  ride / food : {rides} / {len(sessions) - rides}")
    print(f"  total events: {sum(len(s.rides) + len(s.orders) for s in sessions)}")


if __name__ == "__main__":
    main()
