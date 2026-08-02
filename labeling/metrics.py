"""Weighted estimation and label-quality stats.

Things that are easy to get wrong here:

Weighted, not counted. The sample is stratified so counting rows answers a
question about the sample, not about the user's day. Everything divides by
inclusion_prob.

Wilson intervals rather than the normal approximation, which runs past 1.0 at
p=0.96 and small n.

Effective sample size rather than raw n. 200 labels from one person aren't 200
independent observations, they're one person's habits sampled 200 times.

Cohen's kappa on the repeat items is the reliability floor. If someone only
agrees with themselves 70% of the time above chance, nothing evaluated on those
labels can be shown to beat that, and a precision above the annotator's own
consistency means the harness is broken rather than the model being good.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from .schema import FIRED_STRATA


# ---------------------------------------------------------------------------
# Interval estimation
# ---------------------------------------------------------------------------
def wilson_interval(successes: float, total: float, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion. Handles p near 0 or 1 correctly."""
    if total <= 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def design_effect(cluster_sizes: Iterable[int], icc: float = 0.15) -> float:
    """Kish design effect for cluster sampling: 1 + (m_bar - 1) * ICC.

    `icc` is the intra-cluster correlation, how much more alike two labels from
    the same participant are than two from different participants. 0.15 is a
    conservative default for repeated within-person judgements; measure it once
    you have more than one participant, and say it is assumed until then.
    """
    sizes = [s for s in cluster_sizes if s > 0]
    if not sizes:
        return 1.0
    m_bar = sum(sizes) / len(sizes)
    return max(1.0, 1.0 + (m_bar - 1.0) * icc)


def effective_n(n: int, cluster_sizes: Iterable[int], icc: float = 0.15) -> float:
    """Cluster-corrected sample size.

    Returns NaN for a single cluster rather than a number. With one participant
    there is no between-person variation to observe, so the design effect is not
    estimable and any figure produced would be an artefact of the assumed ICC --
    the honest output is "not estimable, recruit a second participant".
    """
    sizes = [s for s in cluster_sizes if s > 0]
    if len(sizes) <= 1:
        return float("nan")
    return n / design_effect(sizes, icc)


def stratified_bootstrap_ratio(
    rows_by_stratum: dict[str, list[tuple[bool, bool, float]]],
    n_boot: int = 2000,
    seed: int = 12345,
    alpha: float = 0.05,
) -> tuple[float, float, float, int]:
    """Weighted ratio-of-totals with a stratified bootstrap interval.

    Each element is ``(in_numerator, in_denominator, weight)`` and the whole
    answered sample for a stratum is passed in -- **not** a pre-filtered subset.
    That distinction is the entire point, and getting it wrong produces a
    zero-width interval:

        precision : denominator = "was fired",         numerator = "+ judged useful"
        recall    : denominator = "participant wanted", numerator = "+ we fired"

    For recall, whether a moment fired is *determined* by its stratum, so every
    row inside a stratum is identical. Resampling a pre-filtered `wanted` subset
    therefore reproduces itself exactly and the interval collapses to a point.
    The real uncertainty is not within strata at all, it is in **how many
    wanted-moments each stratum turns out to contain**, which is a binomial draw
    over the stratum's full answered sample. Resampling the full sample and
    recomputing both totals captures that; resampling the subset cannot.

    This is a ratio estimator: numerator and denominator are both random and
    correlated, which is also why a closed-form binomial interval is the wrong
    tool and a bootstrap is the right one.
    """
    live = {h: v for h, v in rows_by_stratum.items() if v}
    if not live:
        return (0.0, 0.0, 0.0, 0)

    def ratio(sample: dict[str, list[tuple[bool, bool, float]]]) -> float | None:
        num = sum(w for rows in sample.values() for in_n, _in_d, w in rows if in_n)
        den = sum(w for rows in sample.values() for _in_n, in_d, w in rows if in_d)
        return (num / den) if den > 0 else None

    point = ratio(live) or 0.0
    n_den = sum(1 for rows in live.values() for _n, in_d, _w in rows if in_d)

    # Zero observed successes: the bootstrap can only ever resample zeros, so it
    # returns [0, 0] -- which reads as certainty and is not. The classical fix is
    # the rule of three: with 0 events in n trials the 95% upper bound is ~3/n.
    # "We saw no annoyance in 22 items" honestly means "annoyance is under ~14%",
    # not "annoyance is zero".
    observed = sum(1 for rows in live.values() for in_n, _d, _w in rows if in_n)
    if observed == 0:
        return (0.0, 0.0, min(1.0, 3.0 / n_den) if n_den else 1.0, n_den)

    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(n_boot):
        resampled = {
            h: [rows[rng.randrange(len(rows))] for _ in range(len(rows))]
            for h, rows in live.items()
        }
        r = ratio(resampled)
        if r is not None:  # a draw with an empty denominator carries no information
            draws.append(r)
    if not draws:
        return (point, 0.0, 1.0, n_den)
    draws.sort()

    lo = draws[int((alpha / 2) * (len(draws) - 1))]
    hi = draws[int((1 - alpha / 2) * (len(draws) - 1))]
    return (point, lo, hi, n_den)


def stratified_proportion(
    per_stratum: dict[str, tuple[int, int, float]], z: float = 1.96
) -> tuple[float, float, float, int]:
    """Point estimate and CI for a proportion under stratified sampling.

    `per_stratum` maps stratum -> (successes, answered, weight) where
    weight = 1 / inclusion_prob.

        N_h = n_h * w_h              estimated population of stratum h
        W_h = N_h / sum(N_h)         its share of the population
        P   = sum(W_h * p_h)         the weighted proportion
        Var = sum(W_h^2 * p_h(1-p_h) / n_h)

    The point and the interval are derived from the *same* weights. Computing a
    weighted point estimate and then a Wilson interval from raw counts, the
    obvious shortcut, is incoherent, and can place the point estimate outside
    its own confidence interval.

    The finite population correction is deliberately omitted, which makes the
    interval slightly conservative (wider). That is the right direction to err.
    """
    live = {h: v for h, v in per_stratum.items() if v[1] > 0 and v[2] > 0}
    if not live:
        return (0.0, 0.0, 0.0, 0)

    pop = {h: n * w for h, (_, n, w) in live.items()}
    total_pop = sum(pop.values())
    if total_pop <= 0:
        return (0.0, 0.0, 0.0, 0)

    point = 0.0
    var = 0.0
    n_total = 0
    for h, (succ, n, _w) in live.items():
        W = pop[h] / total_pop
        p = succ / n
        point += W * p
        # With n_h == 1 the within-stratum variance is unobservable; fall back to
        # the maximum-variance assumption (p=0.5) so a single-observation stratum
        # widens the interval instead of silently contributing zero uncertainty.
        var_h = (p * (1 - p) / n) if n > 1 else (0.25 / 1)
        var += W * W * var_h
        n_total += n

    se = math.sqrt(var)
    return (point, max(0.0, point - z * se), min(1.0, point + z * se), n_total)


# ---------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------
def cohens_kappa(pairs: list[tuple[str, str]]) -> float:
    """Chance-corrected agreement over (first_answer, second_answer) pairs.

    Raw agreement is misleading when one category dominates: if 90% of answers
    are "Neutral", two random raters agree 81% of the time by luck alone. Kappa
    subtracts that.
    """
    if not pairs:
        return float("nan")
    n = len(pairs)
    labels = sorted({a for a, _ in pairs} | {b for _, b in pairs})
    observed = sum(1 for a, b in pairs if a == b) / n

    first = defaultdict(int)
    second = defaultdict(int)
    for a, b in pairs:
        first[a] += 1
        second[b] += 1
    expected = sum((first[l] / n) * (second[l] / n) for l in labels)

    if expected >= 1.0:
        return float("nan")
    return (observed - expected) / (1 - expected)


# ---------------------------------------------------------------------------
# The estimates that matter
# ---------------------------------------------------------------------------
@dataclass
class Estimate:
    point: float
    lo: float
    hi: float
    weighted_numerator: float = 0.0
    weighted_denominator: float = 0.0
    raw_n: int = 0

    def __str__(self) -> str:
        return f"{self.point * 100:.1f}%  [{self.lo * 100:.1f}, {self.hi * 100:.1f}]  (n={self.raw_n})"


@dataclass
class LabelMetrics:
    precision: Estimate | None = None
    recall: Estimate | None = None
    annoyance_rate: Estimate | None = None
    kappa: float = float("nan")
    gold_pass_rate: float = float("nan")
    response_rate: float = float("nan")
    effective_n: float = 0.0
    per_stratum: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _weight(row: dict[str, Any]) -> float:
    pi = float(row.get("inclusion_prob") or 0.0)
    return 1.0 / pi if pi > 0 else 0.0


def _answered(row: dict[str, Any]) -> bool:
    return bool(row.get("welcome"))


def compute(rows: list[dict[str, Any]], icc: float = 0.15) -> LabelMetrics:
    """Compute all estimates from the joined rows returned by schema.labelled_rows().

    Definitions, stated explicitly because they are the arguable part:

      precision = among candidates the production policy FIRED, the weighted
                  fraction the participant judged "Useful".

      recall    = among ALL moments the participant said they wanted a nudge
                  ("wanted" == yes, any stratum), the weighted fraction that
                  production actually fired. This is the number that only exists
                  because non-fired strata were sampled.

      annoyance = among FIRED candidates, the weighted fraction judged
                  "Annoying". Tracked separately because it, not precision, is
                  what predicts uninstalls.
    """
    m = LabelMetrics()

    # Repeat items are for reliability only, including them in the primary
    # estimates would double-count one moment's opinion.
    repeats = [r for r in rows if r.get("gold_expected") == "__repeat__"]
    primary = [r for r in rows if r.get("gold_expected") != "__repeat__"]

    sent = len(primary)
    answered_rows = [r for r in primary if _answered(r)]
    m.response_rate = len(answered_rows) / sent if sent else float("nan")

    # --- attention checks
    golds = [r for r in answered_rows if r.get("is_gold")]
    if golds:
        passed = sum(1 for r in golds if r.get("did_act", "").lower() == (r.get("gold_expected") or "").lower())
        m.gold_pass_rate = passed / len(golds)
        if m.gold_pass_rate < 0.8:
            m.notes.append(
                f"Attention checks failed: {passed}/{len(golds)} gold items matched the "
                "behaviour log. Treat this participant's batch as unreliable."
            )

    # --- straightlining
    fast = [r for r in answered_rows if 0 < int(r.get("latency_ms") or 0) < 1500]
    if answered_rows and len(fast) / len(answered_rows) > 0.5:
        m.notes.append(
            f"{len(fast)}/{len(answered_rows)} items answered in under 1.5s, "
            "possible straightlining."
        )

    def _estimate(in_den, in_num) -> Estimate:
        """Ratio estimate over the FULL answered sample.

        The whole sample is passed to the bootstrap, with membership expressed as
        predicates, so that the denominator is allowed to vary across resamples.
        Pre-filtering to the denominator subset first is the mistake that
        collapses the recall interval to zero width.
        """
        grouped: dict[str, list[tuple[bool, bool, float]]] = defaultdict(list)
        for r in answered_rows:
            d = bool(in_den(r))
            grouped[r["stratum"]].append((d and bool(in_num(r)), d, _weight(r)))
        p, lo, hi, n = stratified_bootstrap_ratio(dict(grouped))
        return Estimate(
            p, lo, hi,
            sum(_weight(r) for r in answered_rows if in_den(r) and in_num(r)),
            sum(_weight(r) for r in answered_rows if in_den(r)),
            n,
        )

    is_fired = lambda r: r["stratum"] in FIRED_STRATA                      # noqa: E731
    wants = lambda r: (r.get("wanted") or "").lower() == "yes"             # noqa: E731

    # --- precision: among FIRED candidates, the share judged useful
    if any(is_fired(r) for r in answered_rows):
        m.precision = _estimate(is_fired, lambda r: r["welcome"].lower() == "useful")
        m.annoyance_rate = _estimate(is_fired, lambda r: r["welcome"].lower() == "annoying")

    # --- recall: among every moment the participant WANTED a nudge, the share we fired.
    # This estimate exists only because the non-fired strata were sampled, and it
    # is weighted across strata whose weights differ by an order of magnitude -
    # which is why the unweighted version would be nonsense.
    if any(wants(r) for r in answered_rows):
        m.recall = _estimate(wants, is_fired)
    else:
        m.notes.append(
            "No item was marked 'wanted = yes', so recall is not estimable from this "
            "sample. Increase the non-fired allocation (S3/S4/S6)."
        )

    # --- reliability from repeats
    by_candidate: dict[int, list[str]] = defaultdict(list)
    for r in repeats + primary:
        if _answered(r):
            by_candidate[r["candidate_id"]].append(r["welcome"])
    pairs = [(v[0], v[1]) for v in by_candidate.values() if len(v) >= 2]
    m.kappa = cohens_kappa(pairs)
    if pairs and not math.isnan(m.kappa) and m.kappa < 0.4:
        m.notes.append(
            f"Test-retest kappa {m.kappa:.2f} is weak, the participant disagrees with "
            "themselves often, so this is the ceiling on any model evaluated here."
        )

    # --- clustering
    per_participant: dict[str, int] = defaultdict(int)
    for r in answered_rows:
        per_participant[r["participant_id"]] += 1
    m.effective_n = effective_n(len(answered_rows), per_participant.values(), icc)
    if len(per_participant) == 1:
        m.notes.append(
            f"All {len(answered_rows)} labels come from a single participant. Between-person "
            "variance is unestimable, so the confidence intervals above describe sampling "
            "error WITHIN this person only, they do not generalise to other users. "
            "Recruiting a second participant is worth more than doubling the labels from "
            "the first."
        )

    # --- per-stratum breakdown
    for stratum in sorted({r["stratum"] for r in primary}):
        srows = [r for r in primary if r["stratum"] == stratum]
        sans = [r for r in srows if _answered(r)]
        m.per_stratum[stratum] = {
            "sent": len(srows),
            "answered": len(sans),
            "weight": round(_weight(srows[0]), 2) if srows else 0.0,
            "useful": sum(1 for r in sans if r["welcome"].lower() == "useful"),
            "annoying": sum(1 for r in sans if r["welcome"].lower() == "annoying"),
            "wanted_yes": sum(1 for r in sans if (r.get("wanted") or "").lower() == "yes"),
        }

    return m


def report(m: LabelMetrics) -> str:
    out = ["=== Labelled dataset ==="]
    out.append(f"  response rate     : {m.response_rate * 100:5.1f}%" if not math.isnan(m.response_rate) else "  response rate     : n/a")
    out.append(
        f"  effective n       : {m.effective_n:.0f}"
        if not math.isnan(m.effective_n)
        else "  effective n       : not estimable (single participant)"
    )
    out.append(f"  test-retest kappa : {m.kappa:.2f}" if not math.isnan(m.kappa) else "  test-retest kappa : n/a (no repeats answered)")
    out.append(f"  attention checks  : {m.gold_pass_rate * 100:.0f}% passed" if not math.isnan(m.gold_pass_rate) else "  attention checks  : n/a")
    out.append("")
    out.append(f"  PRECISION (fired judged useful)   : {m.precision}" if m.precision else "  PRECISION : not estimable")
    out.append(f"  RECALL    (wanted & we fired)     : {m.recall}" if m.recall else "  RECALL    : not estimable")
    out.append(f"  ANNOYANCE (fired judged annoying) : {m.annoyance_rate}" if m.annoyance_rate else "  ANNOYANCE : not estimable")
    out.append("")
    out.append(f"  {'stratum':<18}{'sent':>6}{'ans':>5}{'wt':>8}{'useful':>8}{'annoy':>7}{'wanted':>8}")
    for s, d in m.per_stratum.items():
        out.append(
            f"  {s:<18}{d['sent']:>6}{d['answered']:>5}{d['weight']:>8.2f}"
            f"{d['useful']:>8}{d['annoying']:>7}{d['wanted_yes']:>8}"
        )
    if m.notes:
        out.append("")
        out.append("  WARNINGS:")
        for n in m.notes:
            out.append(f"    - {n}")
    return "\n".join(out)
