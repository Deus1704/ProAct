"""Deterministic offline evaluation of the trigger policy.

How a session is scored
-----------------------
A session is not a single yes/no question, it is a stretch of time. The live
system polls every 60 seconds, so a habit window that is 20 minutes wide gets
looked at dozens of times. Asking "did it fire?" once would completely miss the
failure mode that actually matters, which is firing *repeatedly* about the same
thing.

So each session is replayed as a sequence of ticks across a window centred on
the session's evaluation moment, and the notifications emitted are counted.

    positive session  -> exactly ONE notification is wanted
    negative session  -> ZERO notifications are wanted

Metrics follow directly from that:

    true positives  TP = positive sessions that received at least one notification
    spam        FP_dup = notifications beyond the first within a positive session
    wrong       FP_neg = notifications in negative sessions
    misses          FN = positive sessions that received none

    precision = TP / (all notifications emitted)
    recall    = TP / (positive sessions)
    trigger frequency = notifications / session

Counting every duplicate as a false positive is the point. A policy that fires
30 times for one real habit has perfect recall and is unusable; precision as
defined here is the number that notices.

Determinism
-----------
Live enrichment is disabled (it would hit Playwright and the network), the
geocoders are stubbed, every session gets a fresh throwaway database, and the
clock is injected. Two runs on the same dataset produce identical numbers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point the persistence layer at a scratch database BEFORE it is imported, so a
# stray import-time connection can never touch the developer's real assistant.db.
_SCRATCH = os.path.join(tempfile.gettempdir(), "proact_eval_boot.db")
os.environ.setdefault("ASSISTANT_DB_PATH", _SCRATCH)

from proactive_assistant_app import database as db  # noqa: E402
from proactive_assistant_app.agents import (  # noqa: E402
    Orchestrator,
    TriggerPolicy,
)
from proactive_assistant_app.pattern_engine import PatternEngine  # noqa: E402

UTC = timezone.utc


# ---------------------------------------------------------------------------
@dataclass
class Metrics:
    sessions: int = 0
    positives: int = 0
    negatives: int = 0
    notifications: int = 0          # every notification emitted
    true_positives: int = 0         # positive sessions with >= 1 notification
    dup_notifications: int = 0      # extra notifications inside positive sessions
    false_positives: int = 0        # notifications in negative sessions
    false_negatives: int = 0        # positive sessions with none
    ticks: int = 0
    by_negative_kind: dict[str, int] = field(default_factory=dict)  # kind -> notifications
    wall_seconds: float = 0.0

    @property
    def precision(self) -> float:
        return self.true_positives / self.notifications if self.notifications else 0.0

    @property
    def recall(self) -> float:
        return self.true_positives / self.positives if self.positives else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def triggers_per_session(self) -> float:
        return self.notifications / self.sessions if self.sessions else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "sessions": self.sessions,
            "positives": self.positives,
            "negatives": self.negatives,
            "notifications": self.notifications,
            "true_positives": self.true_positives,
            "dup_notifications": self.dup_notifications,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "triggers_per_session": round(self.triggers_per_session, 4),
            "ticks": self.ticks,
            "by_negative_kind": self.by_negative_kind,
            "wall_seconds": round(self.wall_seconds, 2),
        }


# ---------------------------------------------------------------------------
def _stub_geocoders(engine: PatternEngine) -> None:
    """Pattern extraction reverse-geocodes unlabelled clusters over HTTP. In an
    offline replay that is both nondeterministic and slow, and the synthetic
    data already carries labels, so it is stubbed rather than mocked at the
    socket layer."""
    engine._reverse_geocode = lambda lat, lng: None  # type: ignore[assignment]
    engine._forward_geocode = lambda label: None  # type: ignore[assignment]


def _seed_session_db(session: dict[str, Any], path: str) -> PatternEngine:
    db.DB_PATH = path
    db.init_db()

    for ride in session.get("rides", []):
        db.insert_ride(ride)
    for order in session.get("orders", []):
        db.insert_food_order(order)

    engine = PatternEngine()
    _stub_geocoders(engine)
    engine.run_full_extraction()
    return engine


def _seed_dismissals(session: dict[str, Any], evaluate_at: datetime) -> int:
    """Recreate a user who has repeatedly said no to this exact pattern.

    Dismissals are counted by joining dismissed_suggestions -> suggestions and
    reading payload_json.pattern_ref, so a realistic history has to be written
    through those tables rather than faked with a counter.
    """
    count = int(session.get("dismissals", 0))
    if count <= 0:
        return 0

    domain = session["domain"]
    if domain == "ride":
        patterns = db.list_departure_patterns(include_suppressed=True)
        table = "departure_patterns"
    else:
        patterns = db.list_order_patterns(include_suppressed=True)
        table = "order_patterns"
    if not patterns:
        return 0

    # Target the pattern the agent would actually pick: highest confidence.
    patterns.sort(key=lambda r: -float(r.get("confidence") or 0))
    pattern_ref = f"{table}:{patterns[0]['id']}"

    written = 0
    for i in range(count):
        # 1..N days back: recent enough to fall inside the 7-day dismissal
        # lookback, old enough that the accompanying trigger_log rows sit well
        # outside any cooldown window (<= 90 min). So this seeds dismissal
        # history without also seeding a cooldown, keeping the two policy paths
        # independently testable.
        when = evaluate_at - timedelta(days=i + 1)
        trigger_id = db.insert_trigger_event(
            domain, ["seeded_dismissal"], 0.9, when, suppressed=False
        )
        suggestion_id = db.insert_suggestion(
            trigger_id, domain, {"pattern_ref": pattern_ref}, "seeded", when
        )
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO dismissed_suggestions(suggestion_id, dismissed_at) VALUES (?, ?)",
                (suggestion_id, when.isoformat()),
            )
        written += 1

    return written


async def _replay_session(
    session: dict[str, Any],
    policy: TriggerPolicy,
    poll_seconds: int,
    window_minutes: int,
) -> tuple[int, int]:
    """Returns (notifications emitted, ticks executed)."""
    evaluate_at = datetime.fromisoformat(session["evaluate_at"])
    if evaluate_at.tzinfo is None:
        evaluate_at = evaluate_at.replace(tzinfo=UTC)

    engine = PatternEngine()
    _stub_geocoders(engine)

    orchestrator = Orchestrator(
        pattern_engine=engine,
        suggestion_builder=None,     # no live hydration: deterministic + offline
        broadcaster=None,
        policy=policy,
        live_context_enabled=False,
    )
    await orchestrator.start()

    notifications = 0
    ticks = 0
    try:
        start = evaluate_at - timedelta(minutes=window_minutes)
        end = evaluate_at + timedelta(minutes=window_minutes)
        current = start
        while current <= end:
            result = await orchestrator.tick(now=current)
            ticks += 1
            if result is not None:
                notifications += 1
            current += timedelta(seconds=poll_seconds)
    finally:
        await orchestrator.stop()

    return notifications, ticks


async def evaluate(
    sessions: list[dict[str, Any]],
    policy: TriggerPolicy,
    poll_seconds: int = 60,
    window_minutes: int = 45,
    progress: bool = False,
) -> Metrics:
    metrics = Metrics()
    started = time.time()
    tmpdir = tempfile.mkdtemp(prefix="proact_eval_")

    for index, session in enumerate(sessions):
        db_path = os.path.join(tmpdir, f"s{index}.db")
        evaluate_at = datetime.fromisoformat(session["evaluate_at"])
        if evaluate_at.tzinfo is None:
            evaluate_at = evaluate_at.replace(tzinfo=UTC)

        _seed_session_db(session, db_path)
        _seed_dismissals(session, evaluate_at)

        fired, ticks = await _replay_session(session, policy, poll_seconds, window_minutes)

        metrics.sessions += 1
        metrics.ticks += ticks
        metrics.notifications += fired

        if session["label"] == 1:
            metrics.positives += 1
            if fired > 0:
                metrics.true_positives += 1
                metrics.dup_notifications += fired - 1
            else:
                metrics.false_negatives += 1
        else:
            metrics.negatives += 1
            metrics.false_positives += fired
            if fired:
                kind = session.get("negative_kind") or "unknown"
                metrics.by_negative_kind[kind] = metrics.by_negative_kind.get(kind, 0) + fired

        # Each session owns its database; delete eagerly so a 200-session sweep
        # does not leave a few hundred WAL files behind.
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass

        if progress and (index + 1) % 25 == 0:
            print(f"    ... {index + 1}/{len(sessions)} sessions", flush=True)

    metrics.wall_seconds = time.time() - started
    try:
        os.rmdir(tmpdir)
    except OSError:
        pass
    return metrics


def load_sessions(path: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    return payload["sessions"], payload.get("meta", {})


def print_report(title: str, policy: TriggerPolicy, metrics: Metrics) -> None:
    print(f"\n=== {title} ===")
    print(f"  policy      : {policy.as_dict()}")
    print(f"  sessions    : {metrics.sessions} ({metrics.positives} pos / {metrics.negatives} neg)"
          f"  ticks {metrics.ticks}  wall {metrics.wall_seconds:.1f}s")
    print(f"  notifications emitted : {metrics.notifications}")
    print(f"    correct (first in a positive session) : {metrics.true_positives}")
    print(f"    duplicate spam in positive sessions   : {metrics.dup_notifications}")
    print(f"    fired in negative sessions            : {metrics.false_positives}")
    print(f"    missed positive sessions              : {metrics.false_negatives}")
    print(f"  PRECISION   : {metrics.precision * 100:6.2f}%")
    print(f"  RECALL      : {metrics.recall * 100:6.2f}%")
    print(f"  F1          : {metrics.f1 * 100:6.2f}%")
    print(f"  triggers/session : {metrics.triggers_per_session:.3f}")
    if metrics.by_negative_kind:
        print("  false positives by negative flavour:")
        for kind in sorted(metrics.by_negative_kind):
            print(f"    {kind:<20} {metrics.by_negative_kind[kind]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dataset",
        default=os.path.join(os.path.dirname(__file__), "holdout_sessions.json"),
    )
    ap.add_argument("--poll-seconds", type=int, default=60)
    ap.add_argument("--window-minutes", type=int, default=45)
    ap.add_argument("--ride-cooldown", type=int, default=45)
    ap.add_argument("--food-cooldown", type=int, default=30)
    ap.add_argument("--global-cooldown", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0, help="evaluate only the first N sessions")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    sessions, meta = load_sessions(args.dataset)
    if args.limit:
        sessions = sessions[: args.limit]

    policy = TriggerPolicy(
        ride_cooldown_minutes=args.ride_cooldown,
        food_cooldown_minutes=args.food_cooldown,
        global_cooldown_minutes=args.global_cooldown,
    )

    print(f"dataset : {args.dataset}")
    print(f"  meta  : {meta}")
    print(f"  replay: every {args.poll_seconds}s across +/-{args.window_minutes} min")

    metrics = asyncio.run(
        evaluate(sessions, policy, args.poll_seconds, args.window_minutes, progress=True)
    )
    print_report("RESULT", policy, metrics)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"policy": policy.as_dict(), "metrics": metrics.as_dict()}, fh, indent=1)
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    main()
