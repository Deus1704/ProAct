"""Stratified sampling with recorded inclusion probabilities.

Most windows in a day are boring. Sampling uniformly would burn ~95% of the
participant's patience on obvious negatives and they'd stop answering by day two,
so we oversample the interesting strata.

The catch is that oversampling changes the estimate. Sample 40 of 50 fired
candidates and 40 of 4000 control windows, count rows, and the precision you get
describes your sample rather than the user's day. So record the probability each
unit had of being picked and weight by its inverse (Horvitz-Thompson):

    pi_s  = n_s / N_s
    weight = 1 / pi_s

which is why items.inclusion_prob is NOT NULL. A stratified sample without the
weights isn't awkward to analyse, it's unusable.

Allocation is fixed rather than Neyman for now since we don't know the variances
before the first batch. It front-loads S3_NEARMISS because that's where a small
threshold change flips the outcome, so a label there buys the most.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .schema import (
    Item,
    STRATA,
    candidates_in_stratum,
    insert_items,
    stratum_counts,
)

# Per-batch allocation. Sums to 12, see MAX_ITEMS_PER_BATCH rationale below.
DEFAULT_ALLOCATION = {
    "S3_NEARMISS": 3,
    "S1_FIRED_STRONG": 2,
    "S2_FIRED_SOFT": 2,
    "S4_COOLDOWN": 2,
    "S5_SUPPRESSED": 1,
    "S6_CONTROL": 2,
}

# Response quality degrades sharply with form length. Twelve items at ~15 seconds
# each is about three minutes, which is the outer limit for a daily instrument
# someone answers unpaid. Longer forms do not buy more labels, they buy the
# same number of labels plus a straightlining problem at the bottom.
MAX_ITEMS_PER_BATCH = 12


@dataclass
class SamplePlan:
    """What was drawn, and with what probability. Persisted with the batch."""

    participant_id: str
    allocation: dict[str, int]
    population: dict[str, int]
    inclusion_prob: dict[str, float]
    drawn: dict[str, list[int]]          # stratum -> candidate_ids

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.drawn.values())

    def summary(self) -> str:
        lines = [f"participant={self.participant_id}  items={self.total}"]
        lines.append(f"  {'stratum':<18}{'N':>7}{'n':>5}{'pi':>9}{'weight':>10}")
        for s in STRATA:
            N = self.population.get(s, 0)
            n = len(self.drawn.get(s, []))
            pi = self.inclusion_prob.get(s, 0.0)
            w = (1.0 / pi) if pi > 0 else 0.0
            lines.append(f"  {s:<18}{N:>7}{n:>5}{pi:>9.4f}{w:>10.2f}")
        return "\n".join(lines)


def plan_batch(
    participant_id: str,
    allocation: dict[str, int] | None = None,
    seed: int | None = None,
    exclude_candidate_ids: set[int] | None = None,
    labels_db: str | None = None,
) -> SamplePlan:
    """Draw a stratified sample without replacement, recording pi_s.

    `exclude_candidate_ids` carries the ids already sent in earlier batches, so a
    participant is not asked the same question twice, except deliberately, see
    `add_repeat_items`.
    """
    rng = random.Random(seed)
    alloc = dict(allocation or DEFAULT_ALLOCATION)
    exclude = exclude_candidate_ids or set()

    population = stratum_counts(participant_id, labels_db)
    drawn: dict[str, list[int]] = {}
    pi: dict[str, float] = {}

    # First pass: draw what each stratum can supply.
    shortfall = 0
    for stratum in STRATA:
        want = alloc.get(stratum, 0)
        if want <= 0:
            continue
        pool = [
            c["candidate_id"]
            for c in candidates_in_stratum(participant_id, stratum, labels_db)
            if c["candidate_id"] not in exclude
        ]
        take = min(want, len(pool))
        if take < want:
            shortfall += want - take
        if take == 0:
            pi[stratum] = 0.0
            drawn[stratum] = []
            continue
        picked = rng.sample(pool, take)
        drawn[stratum] = picked
        # pi is over the ELIGIBLE pool, not the raw stratum size: units already
        # sent had no chance of selection this round, so including them would
        # understate pi and inflate every weight.
        pi[stratum] = take / len(pool)

    # Second pass: redistribute the shortfall to strata that still have supply,
    # so a thin stratum does not silently shrink the batch. pi is recomputed for
    # any stratum we top up, a stratum's weight must reflect what actually
    # happened, not what was planned.
    if shortfall:
        for stratum in ("S3_NEARMISS", "S1_FIRED_STRONG", "S6_CONTROL", "S4_COOLDOWN"):
            if shortfall <= 0:
                break
            pool = [
                c["candidate_id"]
                for c in candidates_in_stratum(participant_id, stratum, labels_db)
                if c["candidate_id"] not in exclude and c["candidate_id"] not in drawn.get(stratum, [])
            ]
            if not pool:
                continue
            extra = min(shortfall, len(pool))
            picked = rng.sample(pool, extra)
            drawn.setdefault(stratum, []).extend(picked)
            eligible = len(pool) + len(drawn[stratum]) - extra
            pi[stratum] = len(drawn[stratum]) / max(eligible, len(drawn[stratum]))
            shortfall -= extra

    return SamplePlan(
        participant_id=participant_id,
        allocation=alloc,
        population=population,
        inclusion_prob=pi,
        drawn=drawn,
    )


def materialise(
    plan: SamplePlan,
    batch_id: int,
    gold_candidate_ids: set[int] | None = None,
    gold_expected: dict[int, str] | None = None,
    seed: int | None = None,
    labels_db: str | None = None,
) -> list[Item]:
    """Turn a plan into ordered items and persist them.

    Presentation order is randomised across strata. If items arrived grouped --
    all the fired ones first, the participant would learn the pattern and their
    answers would correlate with position rather than with content.
    """
    rng = random.Random(seed)
    gold = gold_candidate_ids or set()
    expected = gold_expected or {}

    flat: list[tuple[str, int]] = []
    for stratum, ids in plan.drawn.items():
        for cid in ids:
            flat.append((stratum, cid))
    rng.shuffle(flat)
    flat = flat[:MAX_ITEMS_PER_BATCH]

    items = [
        Item(
            batch_id=batch_id,
            candidate_id=cid,
            stratum=stratum,
            inclusion_prob=plan.inclusion_prob.get(stratum, 0.0),
            position=pos,
            is_gold=cid in gold,
            gold_expected=expected.get(cid, ""),
        )
        for pos, (stratum, cid) in enumerate(flat)
    ]
    insert_items(items, labels_db)
    return items


def add_repeat_items(
    previous_items: list[Item],
    batch_id: int,
    k: int = 1,
    seed: int | None = None,
) -> list[Item]:
    """Re-ask k previously-answered items in a later batch.

    This is the only way to measure whether the instrument is reliable at all.
    Agreement between a participant's two answers to the same item (Cohen's
    kappa, see metrics.py) is an upper bound on how much signal the labels can
    carry: if someone disagrees with themselves 30% of the time, no model trained
    on those labels can do better than that noise floor.

    They are marked with the same inclusion_prob as the original so they do not
    distort the weighted estimates, but consolidate.py excludes repeats from
    the primary metrics and uses them only for the reliability calculation.
    """
    if not previous_items or k <= 0:
        return []
    rng = random.Random(seed)
    picked = rng.sample(previous_items, min(k, len(previous_items)))
    return [
        Item(
            batch_id=batch_id,
            candidate_id=it.candidate_id,
            stratum=it.stratum,
            inclusion_prob=it.inclusion_prob,
            position=MAX_ITEMS_PER_BATCH + n,
            is_gold=False,
            gold_expected="__repeat__",
        )
        for n, it in enumerate(picked)
    ]
