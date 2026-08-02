# Trigger policy evaluation

Every number here comes from the scripts in this directory, driving the same
`Orchestrator` the application runs. Reproduce with:

```bash
python eval/generate_dataset.py     # writes holdout_sessions.json (fixed seed)
python eval/tune_cooldown.py        # the sweep below (~1 h)
python eval/run_eval.py             # a single config
python eval/diagnose_recall.py      # why the recall ceiling is where it is
python eval/explain_confidence.py   # the arithmetic behind one failing session
```

---

## The question

A learned habit window is ±20 minutes wide and the watcher polls every 60 seconds,
so a matching pattern keeps matching for roughly 40 consecutive ticks. A system
that notifies on every one of them is **correct 40 times** and unusable.

So the interesting question is not "can it predict?" but "how many times does it
interrupt you for one prediction?" -- and that requires an evaluation that treats a
session as a stretch of time rather than a single yes/no.

## The dataset

200 sessions, generated deterministically from seed `20260730`:

| | |
|---|---|
| Positive | 100 -- a genuine habit, clock inside the window, user has not already acted. **Exactly one** notification is wanted. |
| Negative | 100 -- one precondition deliberately broken. **Zero** notifications wanted. |
| Negative flavours | 20 each of `no_habit`, `wrong_time`, `already_acted`, `weak_habit`, `dismissed_pattern` |
| Domain split | 100 ride / 100 food |
| Synthetic events | 3,572 rides + orders total |

Five negative flavours rather than one, because "should not fire" has more than one
cause and a harness that only tests the easy one (`no_habit`) reports a precision
that does not exist.

## Replay

Each session is replayed tick by tick across ±45 minutes around its evaluation
moment, at the production poll interval of 60 s: **91 ticks per session, 18,200
ticks per policy configuration.**

Determinism is enforced, not hoped for: live enrichment disabled, geocoders stubbed,
a fresh throwaway SQLite database per session, and the clock injected. Two runs of
the same config produce identical metrics -- verified:

```
run 1 :  notifications 19 | PRECISION 57.89% | RECALL 100.00%
run 2 :  notifications 19 | PRECISION 57.89% | RECALL 100.00%
```

## Scoring

```
TP     = positive sessions that received at least one notification
FP_dup = notifications beyond the first, within a positive session
FP_neg = any notification in a negative session
FN     = positive sessions that received none

precision = TP / (every notification emitted)
recall    = TP / (positive sessions)
```

**Every duplicate counts as a precision failure.** This is the load-bearing choice.
If precision only penalised *wrong* notifications, the version that fires 40 times
per habit would score near-perfectly, and the metric would be measuring the wrong
thing. Spam is a precision failure, so it is counted as one.

Recall is the guard rail: a cooldown that also drops recall is not tuning, it is
switching the feature off.

---

## Cooldown sweep

200 sessions x 18,200 ticks at each of 8 configurations. Everything except the
cooldown is held fixed, so any movement in precision is attributable to the cooldown
alone.

| ride | food | global | precision | recall | F1 | notifications | /session | vs no-cooldown |
|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 0 | 0 | 0 | 7.16% | 75.00% | 13.08% | 1047 | 5.235 | baseline |
| 5 | 5 | 0 | 26.13% | 75.00% | 38.76% | 287 | 1.435 | +18.97 pp, −72.6% |
| 15 | 10 | 0 | 42.86% | 75.00% | 54.55% | 175 | 0.875 | +35.69 pp, −83.3% |
| 30 | 20 | 0 | 48.08% | 75.00% | 58.59% | 156 | 0.780 | +40.91 pp, −85.1% |
| 45 | 30 | 0 | 62.50% | 75.00% | 68.18% | 120 | 0.600 | +55.34 pp, −88.5% |
| **60** | **45** | **0** | **96.15%** | **75.00%** | **84.27%** | **78** | **0.390** | **+88.99 pp, −92.6%** |
| 90 | 60 | 0 | 96.15% | 75.00% | 84.27% | 78 | 0.390 | +88.99 pp, −92.6% |
| 45 | 30 | 20 | 62.50% | 75.00% | 68.18% | 120 | 0.600 | +55.34 pp, −88.5% |

Decomposed, which is where the story is:

| ride | food | TP | duplicate spam | FP on negatives | FN | which negatives leaked |
|---:|---:|---:|---:|---:|---:|---|
| 0 | 0 | 75 | 939 | 33 | 25 | `dismissed_pattern` only |
| 5 | 5 | 75 | 203 | 9 | 25 | `dismissed_pattern` only |
| 15 | 10 | 75 | 94 | 6 | 25 | `dismissed_pattern` only |
| 30 | 20 | 75 | 75 | 6 | 25 | `dismissed_pattern` only |
| 45 | 30 | 75 | 42 | 3 | 25 | `dismissed_pattern` only |
| **60** | **45** | **75** | **0** | **3** | **25** | `dismissed_pattern` only |
| 90 | 60 | 75 | 0 | 3 | 25 | `dismissed_pattern` only |

### Four things this table says

**1. Cooldown is the whole product.** With none, the system emits 1,047
notifications for 100 genuine habits — 939 of them duplicates of a suggestion it had
already made. Precision 7.16%. It is *correct* almost every time and unusable.

**2. It saturates at 60/45, and that is why 60/45 is the answer.** Duplicate spam
reaches exactly **0** there, and 90/60 produces byte-identical results. So the choice
is not "longer is better" — beyond 60/45 extra cooldown buys nothing measurable and
only adds over-suppression risk. 60/45 is the *smallest* cooldown that captures the
entire available gain, which is the only defensible way to pick a point on this
curve.

**3. Recall never moves.** 75.00% at every setting, because a cooldown cannot
suppress the first fire of a session. That is the difference between tuning and
simply turning the feature down, and it is why recall belongs next to precision in
any statement of the result.

**4. Four of the five negative flavours never produce a single false positive** at
any setting. `no_habit`, `wrong_time`, `already_acted` and `weak_habit` are rejected
correctly 100% of the time across all 8 configurations. Every false positive in the
entire sweep comes from `dismissed_pattern` — which turned out to be the same root
cause as the recall ceiling. See below.

---

## The 3 remaining false positives are the bin-splitting defect again

At the tuned setting, 3 notifications leak, all from `dismissed_pattern` sessions —
users who have repeatedly said no. `diagnose_dismissals.py` walks all 20 of them:

```
session   dom  #pat             seeded ref       agent would pick  dismissals seen  verdict
   s0011  food     2       order_patterns:1       order_patterns:2                0  LEAKS
   s0137  food     2       order_patterns:1       order_patterns:2                0  LEAKS
   ...    (the other 18 suppressed correctly, 4 dismissals seen)
dismissed_pattern sessions : 20
  suppressed correctly     : 18
  LEAK a notification      : 2
```

**Dismissal counts are keyed per pattern ID.** When a habit straddles a 15-minute bin
boundary it becomes two patterns with separate IDs, so the user's four dismissals
land on `order_patterns:1` while the agent goes on to pick `order_patterns:2`, which
has zero dismissals recorded against it. The suggestion the user explicitly rejected
comes back under a different ID.

(2 leaking sessions producing 3 notifications: with a 60-minute food cooldown inside
a 90-minute replay window, one of them fires twice.)

This is a **user-visible annoyance bug**, and it is the same defect that caps recall
at 75% — the third place a single modelling choice shows up. Fixing the binning fixes
recall *and* dismissal suppression together.

---

## The recall ceiling: 75%, and exactly why

Recall is 75.00% at **every** cooldown setting. That the cooldown does not move it
is expected and correct -- a cooldown cannot suppress the *first* fire of a session,
because a freshly-seeded session has no prior trigger. But 25 genuine habits going
unnotified is not something to state without explaining, so
`diagnose_recall.py` walks every failing positive session and reports the first
stage that dropped it:

```
positive sessions examined : 100
failed to notify           : 25
=> recall                  : 75.0%

first stage that dropped them:
   25  C: policy rejected -- below_threshold
```

**All 25 are the confidence threshold.** Not extraction failures, not the cooldown,
not the tolerance window. Samples:

```
s0025 food: below_threshold:0.50<0.55
s0076 ride: below_threshold:0.36<0.60
s0033 food: below_threshold:0.38<0.55
```

Chasing *why* the confidence is low on habits that are genuinely regular found two
real defects. `explain_confidence.py` prints the arithmetic.

### Defect 1 -- fixed bin boundaries split a habit in two

Session `s0076` is a real habit: 8 rides, all "around 20:00" on Tuesdays, jittered
±6 minutes. Extraction produced:

```
bin=79  conf=0.3636  freq=4        (bin 79 = 19:45-20:00)
bin=80  conf=0.3636  freq=4        (bin 80 = 20:00-20:15)
```

The jitter straddles the 20:00 bin edge, so **one habit became two patterns of half
the strength**, and neither clears the 0.60 ride threshold. Same in `s0025`:
`bin84 x4` and `bin85 x6` for a 21:15 habit.

This is a property of any fixed grid: a habit centred near an edge is split, and its
apparent frequency roughly halves. Fixes, none implemented: sliding windows;
soft assignment where an event contributes to both adjacent bins with distance
weights; or 1-D clustering on time instead of a fixed grid -- which is already what
the code does for *destinations*, so it is the same idea applied to the other axis.

### Defect 2 -- the confidence denominator punishes active users

```
confidence = count / total          where total = ALL events on that weekday
```

For `s0025`: 12 events on Fridays, 6 of them in the habit's bin, so `6/12 = 0.50`
against a 0.55 threshold. Rejected -- for a habit that never missed a week.

"What share of your Friday activity was this?" is not the same question as **"how
reliably does this recur?"** A user who orders lunch *and* dinner *and* a late snack
on Fridays has every one of those habits scored lower, precisely *because* they have
more habits. That is backwards.

The fix is a recurrence rate, which is scale-free with respect to unrelated
activity:

```
confidence = weeks_the_habit_occurred / weeks_observed
```

Optionally combined as recurrence × share, so consistency and dominance both count
but neither alone can veto.

### Why this is the most valuable output of the whole exercise

Neither defect is visible by reading the code. Binning is correctly implemented, and
a ratio is a perfectly reasonable confidence. They are *modelling* mistakes, and the
only way to find a modelling mistake is to measure the outcome and then refuse to
accept a number you cannot explain. The chain was:

> recall is 75% → all 25 misses are the threshold → the threshold is rejecting
> habits that are genuinely regular → the confidence formula understates them →
> here are the two reasons why.

And bin splitting turned out to cause **three** distinct symptoms from one root
cause, in three different parts of the system:

| Symptom | Where it shows up |
|---|---|
| Recall capped at 75% | split habit halves its frequency, neither half clears the confidence threshold |
| Dismissal suppression leaks | dismissal counts are keyed per pattern ID, so dismissing one half leaves the twin unsuppressed |
| Every remaining false positive | all 3 FPs at the tuned setting trace back to the leak above |

Fixing the binning fixes all three. That is the difference between a bug list and a
root cause -- and it is only visible because the eval measured recall, precision and
the per-flavour false-positive breakdown separately instead of reporting one score.

---

## Limits of this evaluation

Stated plainly, because each of these bounds what the numbers above can be used to
claim.

**The data is synthetic, and it has to be.** The ground truth required -- "did the
user *want* a nudge at this moment" -- does not exist in scraped Uber or Swiggy
history, which records what someone *did*, not what they wanted to be reminded
about. So known habits and known negatives were planted and the system was asked to
rediscover them. That validates the mechanism and the tuning. It says nothing about
real-world precision, which needs real users and their real dismissal rate.

**The sweep cannot penalise an over-long cooldown.** Each session contains exactly
one habit, so a longer cooldown can only ever look better or equal -- it removes
duplicates and has nothing legitimate left to suppress. In reality a 90-minute
cooldown would silence a genuine second habit an hour after the first, and this
metric is blind to that cost.

This is why the **saturation point matters more than the maximum**. Because 60/45
and 90/60 score identically, the data can distinguish "this cooldown is doing work"
from "this cooldown is merely long", and 60/45 is chosen as the smallest setting
capturing the whole measured gain. Had the curve kept climbing, this dataset could
not have told us where to stop. Measuring the over-suppression cost properly requires
sessions containing two genuine habits close together, which this dataset does not
have.

**The cross-domain (global) cooldown is untestable with this dataset**, and the
sweep's last row demonstrates it rather than hiding it. Every session is either
ride-only or food-only:

```
sessions with BOTH rides and orders : 0
ride-only : 100    food-only : 100
```

A global cooldown works by checking the *other* domain's last trigger time. With no
session containing both domains that trigger is never set, so the rule can never
fire, and `ride=45 food=30 global=20` must produce numerically identical results to
`ride=45 food=30 global=0`. It does. That null result is the correct outcome and it
is worth keeping in the table: it says the knob exists, is wired in, and that this
dataset cannot evaluate it. Doing so needs sessions with a ride habit and a food
habit minutes apart.

**Confidence values are quantised by small sample sizes.** With 6-10 habit
repetitions, `count/total` lands on a coarse grid (0.36, 0.50, 0.55...), so results
near a threshold are sensitive to a single event. Larger synthetic histories would
smooth this.

**One tick at a time, single user.** No concurrency, no interleaving users, no
contention on the shared database.

## Files

| | |
|---|---|
| `generate_dataset.py` | Builds the labelled hold-out set. Seeded, stratified. |
| `holdout_sessions.json` | The dataset itself, committed so results are reproducible. |
| `run_eval.py` | Replays sessions against a policy, computes the metrics. |
| `tune_cooldown.py` | Sweeps the cooldown policy, holding everything else fixed. |
| `cooldown_sweep.json` | Raw sweep output, including the per-flavour FP breakdown. |
| `diagnose_recall.py` | For each failing positive, the first stage that dropped it. |
| `explain_confidence.py` | The `count/total` arithmetic behind a specific session. |
