# Labelling pipeline — turning real behaviour into a hold-out set

The evaluation in [`../eval/`](../eval/) runs on a **synthetic** 200-session
dataset. It validates the mechanism and the tuning, and it cannot say anything
about real users. This pipeline produces the real thing.

```bash
python -m labeling.run_pipeline demo        # whole pipeline, simulated respondent
pytest tests/test_labeling.py               # 29 tests
```

---

## The one idea everything else follows from

> **A survey may only ask what you cannot observe.**

Whether the user placed an order is already in the behavioural log. Asking them
re-collects a variable you have — and worse, it *feels* like ground truth, so you
build on it. The label has to be the thing the log cannot contain:

| Question | Purpose | Why this one |
|---|---|---|
| **"If your phone had shown this then, would it have been Useful / Neutral / Annoying?"** | **the label** | Not in any log. This is the only question whose answer you cannot compute. |
| "Were you actually about to do this?" | intent | Lets *non-fired* moments produce false negatives, which is the only way recall becomes measurable. |
| "Did you end up doing it that day?" | **not a label** | Its true answer is already known from the order log, so disagreement measures whether this participant is reading the questions. A silent attention check. |

The framing is counterfactual on purpose — *"if your phone had shown this"* — so
the participant judges the **interruption**, not their own past behaviour.

---

## The four validity threats, and what each cost to fix

These are the reasons a naive version of this idea produces confident, useless
numbers.

### 1. Hindsight collapse
Asking "was this suggestion right?" a day later gets answered from the outcome:
yes if they ordered, no if they didn't. The label degenerates into a variable you
already had, and precision computed from it is circular.

**Fix.** The primary label is *welcomeness*, which is orthogonal to whether they
ordered. A user can have been about to order anyway and still find the nudge
redundant — that gap is where the product actually lives.

### 2. Fired-only sampling makes recall unmeasurable
A dataset built from notifications you actually sent contains no information
about the moments you stayed silent. Recall is then structurally unobservable,
and a system tuned on it drifts toward silence, because silence is never
penalised.

**Fix.** [`capture.py`](capture.py) runs the predictor **twice per tick** —
production policy and a permissive shadow policy — and records both, along with
*why* production declined. Non-fired strata (S3–S6) are sampled and asked about.

### 3. Unweighted stratified sampling gives a biased number
Most 15-minute windows are negatives. Sampling uniformly burns the participant's
patience on obvious non-events. But once you oversample the interesting strata,
**counting rows answers a question about your sample, not about their day**.

**Fix.** [`sample.py`](sample.py) records `inclusion_prob` = nₛ/Nₛ for every item.
Every estimate in [`metrics.py`](metrics.py) weights by its inverse. A stratified
sample without its weights is not merely awkward to analyse — it is unusable, and
the `items.inclusion_prob` column is `NOT NULL` for that reason.

### 4. Retrospective recall bias
End-of-day reconstruction of "did I want a nudge at 8:15pm" is poor.

**Fix.** Two tiers. An **EMA** tier (one question, delivered at the moment) has
the highest validity but can only cover fired candidates. A **daily batch** tier
carries the non-fired strata. Neither is sufficient alone; items appearing in
both let you measure how much answers drift with delay.

---

## Pipeline

```
 ingest history ─► extract patterns ─► SHADOW CAPTURE ─► stratify ─► sample ─► form
   (existing)         (existing)      both policies,     6 strata    with πₛ    ↓
                                      record the reason                       send
                                                                               ↓
   weighted metrics ◄── QA ◄── consolidate ◄── ingest ◄── HMAC verify ◄── response
   + eval/ dataset      kappa,              idempotent
                        attention
```

| Stage | Module | What it does |
|---|---|---|
| Capture | `capture.py` | Runs both policies per tick; records every decision plus the declining reason. Never notifies. |
| Stratify | `capture.classify` | Maps a verdict onto one of six strata, **driven by the reason string** rather than re-deriving the decision — two implementations of one policy is exactly the bug that makes an eval measure the wrong system. |
| Sample | `sample.py` | Stratified draw without replacement, recording πₛ; randomised presentation order; repeat items for reliability. |
| Form | `forms.py` | Google Forms `create` + `batchUpdate` payloads, HMAC-signed batch token, prefill URL. Offline JSON form as a privacy-preserving alternative. |
| Ingest | `ingest.py` | Verifies the token, rejects foreign items, normalises answers, **idempotent per item**. |
| Metrics | `metrics.py` | Weighted ratio estimators with stratified bootstrap intervals, Cohen's κ, attention checks, design effect. |
| Export | `ingest.export_sessions` | Emits the **same schema** `eval/run_eval.py` already reads. |

### Six strata

| | Meaning | Why it exists |
|---|---|---|
| `S1_FIRED_STRONG` | fired, comfortable margin | precision |
| `S2_FIRED_SOFT` | fired on the soft tier | precision at the boundary |
| `S3_NEARMISS` | just below threshold | **most informative** — a small tuning change flips these |
| `S4_COOLDOWN` | suppressed by cooldown | was the suppression correct? |
| `S5_SUPPRESSED` | dismissal history / already acted | was the suppression correct? |
| `S6_CONTROL` | no pattern at all | false negatives from *feature extraction*, and attention checks |

`S6` is the one people skip. A participant marking a random 3am window "useful"
is not reading; and a "wanted = yes" in `S6` is a miss no amount of policy tuning
would have caught, because the failure was upstream in extraction.

---

## The statistics, and one mistake I made building it

Estimates are **ratios of weighted totals**, so the intervals come from a
stratified bootstrap rather than a closed form.

I first wrote the interval as Wilson-on-raw-counts around a weighted point
estimate. That is incoherent — the point and the interval used different
weightings, and the point could land outside its own CI. Fixing that surfaced a
second, more interesting problem.

**Whether a moment fired is *determined* by its stratum** (S1/S2 fired, the rest
did not). So within-stratum variance for recall is exactly zero, and a
stratified-binomial formula returns a **zero-width interval** — which looks like
certainty and is actually the wrong model. The uncertainty in recall is not
within strata at all: it is in *how many wanted-moments each stratum turns out to
contain*.

The fix is to hand the bootstrap the **full answered sample** with numerator and
denominator membership as predicates, so the denominator varies across resamples.
Pre-filtering to the denominator subset is precisely what collapses the interval.
`tests/test_labeling.py::test_recall_interval_is_not_degenerate` pins it.

Three more details worth knowing:

- **Zero events ≠ certainty.** With 0 annoyance reports in 22 items, the bootstrap
  can only resample zeros and returns [0, 0]. The rule of three gives the honest
  answer: the 95% upper bound is ≈3/n, so "under ~14%", not "zero".
- **Effective n is not estimable from one participant.** With a single cluster
  there is no between-person variance to observe, so the code returns NaN and
  says so, rather than printing a number that is an artefact of an assumed ICC.
- **κ is the ceiling.** Test–retest agreement on repeated items bounds what any
  model evaluated on these labels can be shown to achieve. A measured precision
  above the annotator's own consistency is a sign of a broken harness.

---

## What this costs to actually run

At 12 items/day and the ~87% response rate the demo assumes:

| Participants | Days to 200 labels |
|---|---|
| 1 | ~19 |
| 3 | ~7 |
| 5 | ~4 |

**Recruiting a second participant is worth more than doubling the labels from the
first**, because it is the only thing that makes the intervals generalise beyond
one person's habits.

---

## Honest limitations

- **Perceived usefulness ≠ incremental conversion.** No survey can tell you
  whether a notification *caused* an order. That is causal and needs a randomised
  holdout where a slice gets nothing. Say this before you are asked.
- **Non-response is not random.** People answer when delighted or annoyed. The
  response rate is reported per stratum so the bias is visible, but it is not
  corrected for.
- **Google Forms sends item cards to a third party.** Cards therefore carry only
  weekday, time and a coarse nickname — never coordinates, addresses or order
  contents (`tests/test_labeling.py::TestPrivacy` enforces this). If the
  participant is not you, prefer `forms.build_offline_form`.
- **The token is tamper-evident, not tamper-proof.** Forms has no hidden fields,
  so the participant can edit the prefilled reference code — editing it
  invalidates the HMAC and ingestion rejects the submission. That is the correct
  guarantee for a survey; it is not authentication.
- **Consent is a prerequisite, not a checkbox.** This collects a person's travel
  and eating patterns. Anyone enrolled must be told what is collected, where it
  goes, how long it is kept, and how to withdraw.

---

## How to describe this in an interview

**60 seconds:**

> "The hard part of a proactive system isn't prediction — it's that there's no
> label for *should I have interrupted you*. Your behaviour log tells you what
> someone did; it never tells you what they wanted. So I built a labelling
> pipeline.
>
> It runs the predictor in shadow mode below the production threshold, so I
> capture not just what it would have sent but what it *nearly* sent — that's how
> you get false negatives, which a fired-only dataset structurally cannot give
> you. Then I stratify-sample across six strata, record the inclusion probability
> for every item, and weight by its inverse when I compute metrics, because
> counting rows in a stratified sample answers a question about the sample rather
> than about the user's day.
>
> The question I ask is the one that isn't in the log: *would this have been
> welcome or annoying*. I deliberately don't use 'did you order' as the label —
> that's already in my data. A survey should only ever measure what you can't
> observe."

**The follow-up you want**, and the answer:

> *"So what does that give you that your synthetic eval didn't?"*
>
> "Recall against real intent, and an annoyance rate — neither of which a
> synthetic set can produce, because I wrote the labels. And it gives me honest
> intervals: with ~200 labels from one person, precision comes out around 67%
> with a confidence interval of roughly ±22 points. That width is the real
> finding. It's why I'd recruit a second participant before collecting more
> labels from the first — one participant gives you no between-person variance at
> all, so the intervals don't generalise."

**If they push on causality** — and a good interviewer will:

> "It can't answer that, and I wouldn't claim it does. This measures *perceived*
> usefulness. Whether the notification caused an order that wouldn't otherwise
> have happened is a causal question, and offline data can't answer it — you need
> a holdout A/B where a random slice gets no notification, and you measure
> incremental conversion, not raw conversion."

### The bug story to keep in your pocket

The recall interval came out **zero-width** the first time. It looked like a
beautifully precise result. It was the wrong model: whether a moment fired is
determined by its stratum, so within-stratum variance is zero by construction.
Realising that a suspiciously perfect number was a modelling error rather than a
good outcome is the story — it is the same instinct as the 94 req/s benchmark in
the proxy, and interviewers remember the instinct far longer than the number.
