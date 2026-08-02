"""End-to-end driver for the labelling pipeline.

    python -m labeling.run_pipeline init      --participant p001
    python -m labeling.run_pipeline capture   --participant p001 --days 14
    python -m labeling.run_pipeline batch     --participant p001 --kind daily
    python -m labeling.run_pipeline ingest    --file responses.json
    python -m labeling.run_pipeline metrics
    python -m labeling.run_pipeline export    --out eval/labelled_sessions.json
    python -m labeling.run_pipeline demo      # simulated participant, no network

`demo` runs the entire pipeline against a simulated respondent so the mechanics
can be verified without waiting days for real answers. It is also what the tests
exercise.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from labeling import forms, ingest, metrics as metrics_mod  # noqa: E402
from labeling.sample import (  # noqa: E402
    DEFAULT_ALLOCATION,
    add_repeat_items,
    materialise,
    plan_batch,
)
from labeling.schema import (  # noqa: E402
    Batch,
    Candidate,
    Participant,
    get_db,
    init_db,
    insert_batch,
    insert_candidate,
    insert_items,
    labelled_rows,
    stratum_counts,
    upsert_participant,
    utc_now_iso,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
def cmd_init(args) -> int:
    init_db(args.db)
    upsert_participant(
        Participant(
            participant_id=args.participant,
            consented_at=utc_now_iso(),
            timezone=args.timezone,
            notes=args.notes,
        ),
        args.db,
    )
    print(f"initialised {args.db or 'labels.db'} for participant {args.participant}")
    print(
        "\nCONSENT REMINDER: this pipeline collects a person's travel and eating\n"
        "patterns and, if you use Google Forms, sends coarse versions of them to a\n"
        "third party. Do not enrol anyone who has not been told: what is collected,\n"
        "where it goes, how long it is kept, and how to withdraw."
    )
    return 0


def cmd_batch(args) -> int:
    """Draw a stratified sample and build the form for it."""
    init_db(args.db)

    with get_db(args.db) as conn:
        already = {
            r["candidate_id"]
            for r in conn.execute(
                """SELECT i.candidate_id FROM items i
                   JOIN batches b ON b.batch_id = i.batch_id
                   WHERE b.participant_id = ?""",
                (args.participant,),
            ).fetchall()
        }

    plan = plan_batch(
        args.participant,
        allocation=DEFAULT_ALLOCATION,
        seed=args.seed,
        exclude_candidate_ids=already,
        labels_db=args.db,
    )
    if plan.total == 0:
        print("no candidates available to sample, run `capture` first")
        return 1

    token, expires = forms.mint_token(args.participant, args.kind, args.ttl_hours)
    batch_id = insert_batch(
        Batch(
            participant_id=args.participant,
            kind=args.kind,
            created_at=utc_now_iso(),
            token=token,
            expires_at=expires,
        ),
        args.db,
    )
    items = materialise(plan, batch_id, seed=args.seed, labels_db=args.db)

    cards = []
    with get_db(args.db) as conn:
        for it in items:
            row = conn.execute(
                "SELECT context_json, domain FROM candidates WHERE candidate_id=?",
                (it.candidate_id,),
            ).fetchone()
            cards.append(
                forms.ItemCard.from_context(it.item_id, row["context_json"], row["domain"])
            )

    print(plan.summary())
    print(f"\nbatch {batch_id}, {len(items)} items, expires {expires}")

    out = {
        "create": forms.build_create_request(args.participant, args.kind, datetime.now(UTC)),
        "batchUpdate": forms.build_batch_update_request(cards, token),
        "offline_form": forms.build_offline_form(cards, token),
        "token": token,
        "batch_id": batch_id,
    }
    path = args.out or f"batch_{batch_id}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print(f"wrote {path}")
    print(
        "\nNext: POST create -> forms.create, then batchUpdate -> forms.batchUpdate,\n"
        "store the returned questionIds, and send the prefilled URL."
    )
    return 0


def cmd_ingest(args) -> int:
    with open(args.file, encoding="utf-8") as fh:
        payload = json.load(fh)
    token, answers = ingest.parse_offline_submission(payload)
    n = ingest.record_response(token, answers, labels_db=args.db)
    print(f"recorded {n} responses")
    return 0


def cmd_metrics(args) -> int:
    rows = labelled_rows(args.db)
    if not rows:
        print("no items yet")
        return 1
    m = metrics_mod.compute(rows, icc=args.icc)
    print(metrics_mod.report(m))
    return 0


def cmd_export(args) -> int:
    rows = labelled_rows(args.db)
    payload = ingest.export_sessions(rows)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    print(f"wrote {payload['meta']['count']} labelled sessions -> {args.out}")
    if payload["meta"]["count"] < 30:
        print(
            "\nNote: fewer than 30 labelled sessions. Any precision estimate from this "
            "will have a confidence interval wide enough to be uninformative."
        )
    return 0


# ---------------------------------------------------------------------------
# Demo: a simulated participant, so the mechanics are testable today
# ---------------------------------------------------------------------------
def _simulate_candidates(participant: str, n_days: int, db: str | None, seed: int) -> int:
    """Populate the candidate frame directly, bypassing the live orchestrator.

    The real `capture.sweep_day` needs behavioural memory and a running pattern
    engine. This produces a frame with the same shape and realistic stratum
    proportions so sampling, weighting and metrics can be exercised now.
    """
    rng = random.Random(seed)
    start = datetime.now(UTC) - timedelta(days=n_days)
    # Realistic skew: control windows vastly outnumber fired candidates. This
    # imbalance is precisely why stratified sampling is required.
    mix = (
        ["S6_CONTROL"] * 60
        + ["S3_NEARMISS"] * 12
        + ["S4_COOLDOWN"] * 10
        + ["S5_SUPPRESSED"] * 6
        + ["S1_FIRED_STRONG"] * 8
        + ["S2_FIRED_SOFT"] * 4
    )
    made = 0
    for d in range(n_days):
        for t in range(14):
            when = start + timedelta(days=d, hours=8 + t)
            stratum = rng.choice(mix)
            domain = "food" if when.hour >= 12 else "ride"
            fired = stratum in ("S1_FIRED_STRONG", "S2_FIRED_SOFT")
            conf = {
                "S1_FIRED_STRONG": rng.uniform(0.75, 0.95),
                "S2_FIRED_SOFT": rng.uniform(0.55, 0.70),
                "S3_NEARMISS": rng.uniform(0.40, 0.58),
                "S4_COOLDOWN": rng.uniform(0.60, 0.90),
                "S5_SUPPRESSED": rng.uniform(0.60, 0.90),
                "S6_CONTROL": rng.uniform(0.0, 0.25),
            }[stratum]
            cid = insert_candidate(
                Candidate(
                    participant_id=participant,
                    domain=domain,
                    decided_at=when.isoformat(),
                    stratum=stratum,
                    fired_prod=fired,
                    reason="fire" if fired else stratum.lower(),
                    confidence=round(conf, 3),
                    threshold=0.60 if domain == "ride" else 0.55,
                    pattern_ref=f"{domain}_patterns:{rng.randint(1, 6)}",
                    context_json=json.dumps(
                        {
                            "domain": domain,
                            "weekday": when.strftime("%A"),
                            "time": when.strftime("%H:%M"),
                            "bin": (when.hour * 60 + when.minute) // 15,
                            "place_label": rng.choice(["Office", "Home", "Gym", ""]),
                            "restaurant_label": rng.choice(["the usual place", "Truffles", ""]),
                            "confidence": round(conf, 3),
                        },
                        sort_keys=True,
                    ),
                ),
                db,
            )
            if cid:
                made += 1
    return made


_STRATUM_PRIORS = {
    "S1_FIRED_STRONG": ([0.75, 0.18, 0.07], 0.80),
    "S2_FIRED_SOFT":   ([0.45, 0.35, 0.20], 0.35),
    "S3_NEARMISS":     ([0.40, 0.45, 0.15], 0.35),
    "S4_COOLDOWN":     ([0.15, 0.45, 0.40], 0.15),
    "S5_SUPPRESSED":   ([0.15, 0.45, 0.40], 0.12),
    "S6_CONTROL":      ([0.03, 0.85, 0.12], 0.03),
}

# How often the simulated participant contradicts their own earlier answer to the
# same item. Real people are somewhat but not perfectly consistent; this is what
# the test-retest kappa is measuring, and setting it to 0 would make the QA layer
# look like it works when it has nothing to detect.
_SELF_INCONSISTENCY = 0.15


def _simulate_answers(items, db: str | None, seed: int) -> dict[int, dict]:
    """A plausible respondent: opinion is a property of the MOMENT, not the batch.

    The key detail is that the RNG is seeded per `candidate_id`, not per call. A
    participant asked about the same moment twice will usually give the same
    answer, which is what makes the test-retest kappa meaningful. Seeding per
    call would make repeated items independent draws, and kappa would sit at
    chance no matter how well the pipeline worked.
    """
    answers: dict[int, dict] = {}
    for it in items:
        if it.item_id is None:
            continue
        # Seeded on candidate_id ALONE, deliberately not on the batch seed.
        # A participant's opinion of a moment must not change because we happened
        # to ask in a different batch, or the test-retest kappa measures the
        # simulator's RNG rather than the participant's consistency.
        stable = random.Random(1000003 + it.candidate_id)
        probs, wanted_p = _STRATUM_PRIORS.get(it.stratum, ([0.2, 0.6, 0.2], 0.1))
        welcome = stable.choices(["Useful", "Neutral", "Annoying"], probs)[0]
        wanted = "Yes" if stable.random() < wanted_p else "No"

        # A repeat of an already-seen moment: mostly agree, sometimes drift.
        jitter = random.Random(seed * 31 + it.candidate_id)
        if it.gold_expected == "__repeat__" and jitter.random() < _SELF_INCONSISTENCY:
            welcome = jitter.choice([w for w in ("Useful", "Neutral", "Annoying") if w != welcome])

        answers[it.item_id] = {
            "welcome": welcome,
            "wanted": wanted,
            # Gold items have a known correct answer taken from the behaviour log;
            # the simulated participant answers them correctly most of the time.
            "did_act": (
                it.gold_expected
                if it.is_gold and jitter.random() > 0.1
                else stable.choice(["Yes", "No", "Don't remember"])
            ),
            "latency_ms": jitter.randint(2000, 9000),
        }
    return answers


def cmd_demo(args) -> int:
    db = args.db or os.path.join(os.path.dirname(__file__), "labels_demo.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except OSError:
            pass

    os.environ.setdefault(forms.TOKEN_SECRET_ENV, "demo-secret-not-for-real-use")
    init_db(db)
    upsert_participant(
        Participant(participant_id="demo01", consented_at=utc_now_iso()), db
    )

    made = _simulate_candidates("demo01", args.days, db, args.seed)
    print(f"1. captured {made} candidates across {args.days} days")
    counts = stratum_counts("demo01", db)
    for s, c in sorted(counts.items()):
        print(f"     {s:<18} {c:>5}")

    sent_items = []
    already: set[int] = set()
    previous: list = []

    n_batches = args.batches
    print(f"\n2. running {n_batches} daily batches")
    for b in range(n_batches):
        plan = plan_batch("demo01", seed=args.seed + b, exclude_candidate_ids=already, labels_db=db)
        if plan.total == 0:
            print(f"     batch {b + 1}: frame exhausted")
            break
        token, expires = forms.mint_token("demo01", "daily")
        batch_id = insert_batch(
            Batch(
                participant_id="demo01",
                kind="daily",
                created_at=utc_now_iso(),
                token=token,
                expires_at=expires,
            ),
            db,
        )
        # Two items per batch are attention checks: their `did_act` answer is
        # already known from the behaviour log, so a mismatch reveals a
        # participant who is clicking through without reading.
        gold_ids = {c for c in list(plan.drawn.get("S1_FIRED_STRONG", []))[:1]}
        gold_ids |= {c for c in list(plan.drawn.get("S6_CONTROL", []))[:1]}
        gold_expected = {c: ("Yes" if i == 0 else "No") for i, c in enumerate(sorted(gold_ids))}

        items = materialise(
            plan, batch_id, gold_candidate_ids=gold_ids, gold_expected=gold_expected,
            seed=args.seed + b, labels_db=db,
        )

        # From the second batch on, re-ask one earlier item to measure reliability.
        if previous:
            repeats = add_repeat_items(previous, batch_id, k=1, seed=args.seed + b)
            insert_items(repeats, db)
            items = items + repeats

        already.update(i.candidate_id for i in items)
        previous = items
        sent_items.extend(items)

        answers = _simulate_answers(items, db, args.seed + b)
        # Non-response is per item, not per batch, a participant skips individual
        # awkward questions rather than abandoning the whole form.
        skipper = random.Random(args.seed * 977 + b)
        keep = {k: v for k, v in answers.items() if skipper.random() > 0.12}
        ingest.record_response(token, keep, labels_db=db)
        print(f"     batch {b + 1}: {len(items)} items sent, {len(keep)} answered")

    print("\n3. metrics")
    rows = labelled_rows(db)
    m = metrics_mod.compute(rows, icc=args.icc)
    print(metrics_mod.report(m))

    out = args.out or os.path.join(os.path.dirname(__file__), "labelled_sessions.demo.json")
    payload = ingest.export_sessions(rows)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    print(f"\n4. exported {payload['meta']['count']} labelled sessions -> {out}")
    print("   (same schema as eval/holdout_sessions.json, so run_eval.py reads it unchanged)")
    return 0


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="labels database path")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init");     p.add_argument("--participant", required=True); p.add_argument("--timezone", default="Asia/Kolkata"); p.add_argument("--notes", default=""); p.set_defaults(fn=cmd_init)
    p = sub.add_parser("batch");    p.add_argument("--participant", required=True); p.add_argument("--kind", default="daily", choices=["daily", "ema"]); p.add_argument("--seed", type=int, default=None); p.add_argument("--ttl-hours", type=int, default=72); p.add_argument("--out", default=None); p.set_defaults(fn=cmd_batch)
    p = sub.add_parser("ingest");   p.add_argument("--file", required=True); p.set_defaults(fn=cmd_ingest)
    p = sub.add_parser("metrics");  p.add_argument("--icc", type=float, default=0.15); p.set_defaults(fn=cmd_metrics)
    p = sub.add_parser("export");   p.add_argument("--out", required=True); p.set_defaults(fn=cmd_export)
    p = sub.add_parser("demo");     p.add_argument("--days", type=int, default=14); p.add_argument("--batches", type=int, default=18); p.add_argument("--seed", type=int, default=7); p.add_argument("--icc", type=float, default=0.15); p.add_argument("--out", default=None); p.set_defaults(fn=cmd_demo)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
