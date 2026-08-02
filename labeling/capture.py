"""Shadow capture: record every decision, not just the ones that fired.

If you only keep the notifications you actually sent, you can measure precision
and nothing else. There's no record of the times the system stayed quiet when
the user would have wanted a nudge, so recall is unmeasurable and tuning drifts
toward silence because silence never costs you anything.

So we evaluate twice per tick: once under the production policy (would we have
notified?) and once under a permissive shadow policy (was there a pattern here
at all?). Both get recorded along with the reason production declined, and that
reason is what lets us stratify the sample later.

Capture is read-only. No notification, no trigger log write, no suggestion state.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proactive_assistant_app import database as db  # noqa: E402
from proactive_assistant_app.agents import (  # noqa: E402
    Candidate as AgentCandidate,
    DecisionAgent,
    TriggerPolicy,
)

from .schema import Candidate, insert_candidate  # noqa: E402

UTC = timezone.utc

# A candidate this far below the production threshold is still worth asking
# about: it is the region where a small tuning change flips the outcome, so it
# is where labels buy the most information.
NEARMISS_MARGIN = 0.20

# Above this much headroom over threshold, a fired candidate is "strong".
STRONG_MARGIN = 0.10


def shadow_policy(prod: TriggerPolicy) -> TriggerPolicy:
    """Same feature extraction, deliberately permissive policy.

    Cooldowns are zeroed and thresholds dropped so that the shadow pass answers
    "was there a pattern here?" rather than "would we have sent this?" -- the
    production policy already answers the second question on the same tick.
    """
    return dataclasses.replace(
        prod,
        ride_cooldown_minutes=0,
        food_cooldown_minutes=0,
        global_cooldown_minutes=0,
        ride_confidence_threshold=max(0.0, prod.ride_confidence_threshold - NEARMISS_MARGIN),
        food_confidence_threshold=max(0.0, prod.food_confidence_threshold - NEARMISS_MARGIN),
        ride_soft_threshold=0.0,
    )


def classify(
    fired_prod: bool, reason: str, confidence: float, threshold: float, soft: bool
) -> str:
    """Map a production verdict onto a sampling stratum.

    The mapping is deliberately driven by the *reason string*, not by
    re-deriving the decision here. Re-deriving would mean two implementations of
    the policy that can disagree, which is exactly the class of bug that makes an
    eval measure the wrong system.
    """
    if fired_prod:
        if soft:
            return "S2_FIRED_SOFT"
        return "S1_FIRED_STRONG" if confidence >= threshold + STRONG_MARGIN else "S2_FIRED_SOFT"

    head = reason.split(":")[0]
    if head == "below_threshold":
        return "S3_NEARMISS" if confidence >= threshold - NEARMISS_MARGIN else "S6_CONTROL"
    if head == "cooldown":
        return "S4_COOLDOWN"
    if head in ("suppressed", "already_booked_today", "already_ordered_today"):
        return "S5_SUPPRESSED"
    return "S6_CONTROL"


def _context(cand: AgentCandidate, domain: str) -> str:
    """The human-readable card the participant will actually be shown.

    Deliberately excludes raw coordinates and any free-text address. This payload
    leaves the device (it ends up in a Google Form), so it carries the minimum
    needed for the participant to recognise the moment: day, time, domain, and a
    coarse label.
    """
    fired_at: datetime = cand.fired_at
    return json.dumps(
        {
            "domain": domain,
            "weekday": fired_at.strftime("%A"),
            "time": fired_at.strftime("%H:%M"),
            "bin": cand.hour_bin,
            "place_label": cand.features.get("place_label") or "",
            "restaurant_label": cand.features.get("restaurant_label") or "",
            "confidence": round(cand.confidence, 3),
        },
        sort_keys=True,
    )


class ShadowCapture:
    """Evaluates one tick under both policies and records the outcome."""

    def __init__(self, participant_id: str, policy: TriggerPolicy | None = None,
                 labels_db: str | None = None) -> None:
        self.participant_id = participant_id
        self.prod_policy = policy or TriggerPolicy()
        self.shadow = shadow_policy(self.prod_policy)
        self.labels_db = labels_db
        self._prod_agent = DecisionAgent(_NullQueue(), lambda: self.prod_policy)
        self._shadow_agent = DecisionAgent(_NullQueue(), lambda: self.shadow)
        self.recorded = 0
        self.skipped = 0

    def _threshold_for(self, domain: str) -> float:
        return (
            self.prod_policy.ride_confidence_threshold
            if domain == "ride"
            else self.prod_policy.food_confidence_threshold
        )

    def observe(self, cand: AgentCandidate, now: datetime) -> Candidate | None:
        """Record one candidate. Returns the row written, or None if it was a
        duplicate of an already-captured (tick, pattern)."""
        prod_verdict = self._prod_agent.evaluate(cand, now)
        shadow_verdict = self._shadow_agent.evaluate(cand, now)

        # Nothing to ask about: neither policy saw a usable pattern here.
        if not prod_verdict.fire and not shadow_verdict.fire:
            if shadow_verdict.reason.split(":")[0] == "below_threshold":
                pass  # still record, this is the S3/S6 boundary we need
            else:
                self.skipped += 1
                return None

        threshold = self._threshold_for(cand.domain)
        soft = bool(
            prod_verdict.candidate
            and prod_verdict.candidate.features.get("soft")
        )
        stratum = classify(
            prod_verdict.fire, prod_verdict.reason, cand.confidence, threshold, soft
        )

        row = Candidate(
            participant_id=self.participant_id,
            domain=cand.domain,
            decided_at=now.astimezone(UTC).isoformat(),
            stratum=stratum,
            fired_prod=prod_verdict.fire,
            reason=prod_verdict.reason,
            confidence=float(cand.confidence),
            threshold=threshold,
            pattern_ref=cand.pattern_ref,
            context_json=_context(cand, cand.domain),
        )
        cid = insert_candidate(row, self.labels_db)
        if cid is None:
            self.skipped += 1
            return None
        row.candidate_id = cid
        self.recorded += 1
        return row

    def observe_control(self, domain: str, now: datetime) -> Candidate | None:
        """Record a window with no candidate at all.

        Control windows are not decoration. They are how you discover that the
        user wanted a nudge at a time your extractor found no pattern, a false
        negative that no amount of policy tuning would have caught, because the
        failure was upstream in feature extraction. They double as attention
        checks, since a participant marking a 3am control window "useful" is not
        reading the questions.
        """
        row = Candidate(
            participant_id=self.participant_id,
            domain=domain,
            decided_at=now.astimezone(UTC).isoformat(),
            stratum="S6_CONTROL",
            fired_prod=False,
            reason="no_candidate",
            confidence=0.0,
            threshold=self._threshold_for(domain),
            pattern_ref="",
            context_json=json.dumps(
                {
                    "domain": domain,
                    "weekday": now.strftime("%A"),
                    "time": now.strftime("%H:%M"),
                    "bin": (now.hour * 60 + now.minute) // 15,
                    "place_label": "",
                    "restaurant_label": "",
                    "confidence": 0.0,
                },
                sort_keys=True,
            ),
        )
        cid = insert_candidate(row, self.labels_db)
        if cid is None:
            return None
        row.candidate_id = cid
        self.recorded += 1
        return row


class _NullQueue:
    """DecisionAgent's constructor wants an outbox; the synchronous `evaluate`
    path never touches it. This keeps capture free of an event loop."""

    async def put(self, _):  # pragma: no cover - never called
        raise AssertionError("shadow capture must not enqueue")


# ---------------------------------------------------------------------------
def sweep_day(
    participant_id: str,
    day: datetime,
    pattern_agent_candidates,
    policy: TriggerPolicy | None = None,
    poll_seconds: int = 900,
    control_windows: int = 4,
    labels_db: str | None = None,
) -> ShadowCapture:
    """Walk one day at `poll_seconds` resolution, recording every decision.

    `pattern_agent_candidates(now)` is injected rather than imported so this runs
    against either live behavioural memory or a replay. Default resolution is 15
    minutes, one tick per pattern bin, because finer ticks produce many
    near-identical candidates and inflate the sampling frame without adding
    information.
    """
    cap = ShadowCapture(participant_id, policy, labels_db)
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    ticks = int(24 * 3600 / poll_seconds)

    empty_ticks: list[datetime] = []
    for i in range(ticks):
        now = start + timedelta(seconds=i * poll_seconds)
        cands = pattern_agent_candidates(now) or []
        if not cands:
            empty_ticks.append(now)
            continue
        for cand in cands:
            cap.observe(cand, now)

    # Sample control windows from genuinely empty ticks, spread across the day
    # rather than clustered at night, or every control lands at 4am and the
    # stratum stops being informative.
    if empty_ticks and control_windows > 0:
        daytime = [t for t in empty_ticks if 7 <= t.hour <= 23] or empty_ticks
        step = max(1, len(daytime) // control_windows)
        for t in daytime[::step][:control_windows]:
            cap.observe_control("food" if t.hour >= 11 else "ride", t)

    return cap
