"""Multi-agent trigger pipeline.

Structure
---------
A central ``Orchestrator`` owns four sub-agents. Every agent has its own
``asyncio.Queue`` inbox and its own consumer task; agents never call each other
directly, they only put messages on queues. The orchestrator is the only
component that knows the pipeline shape.

    Orchestrator --TICK--> RidePatternAgent  --+
                 --TICK--> FoodPatternAgent  --+--> LiveContextAgent --> DecisionAgent
                                                            (enrich)        (policy)

Why a queue instead of direct awaits
------------------------------------
The previous single-loop design awaited ride evaluation, then food evaluation,
then a live-data scrape, in one sequence. Three consequences, all of which the
queue fixes:

1. Ride and food evaluation are independent but ran serially. They are now
   dispatched concurrently and joined on a shared results queue.
2. Live data comes from Playwright scrapes of third-party sites. A single hung
   scrape stalled the entire trigger loop, including the cheap deterministic
   work. Each agent now runs under its own deadline; a timeout degrades that one
   stage instead of the tick.
3. An exception anywhere killed the cycle. Each agent catches, records, and
   continues, so one bad pattern cannot silence the whole system.

Separation of concerns
----------------------
Feature extraction (does a learned pattern match this moment?) is separated from
policy (given a match, should we actually interrupt the user?). Pattern agents
only propose; ``DecisionAgent`` alone applies cooldowns, dismissal thresholds,
and suppression. That split is what makes the policy tunable in isolation --
``eval/`` sweeps ``TriggerPolicy`` against a labelled hold-out set without
touching feature extraction.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Awaitable, Callable

from . import database as db
from .pattern_engine import PatternEngine
from .suggestion_builder import SuggestionBuilder

log = logging.getLogger(__name__)
UTC = timezone.utc


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
class MessageKind(str, Enum):
    TICK = "tick"                 # orchestrator -> pattern agents
    CANDIDATE = "candidate"       # pattern agents -> orchestrator
    ENRICHED = "enriched"         # live-context agent -> orchestrator
    DECISION = "decision"         # decision agent -> orchestrator
    NO_CANDIDATE = "no_candidate" # pattern agent found nothing this tick


@dataclass
class Message:
    """Envelope passed between agents. `trace_id` ties every message produced by
    one tick together, which is what makes a misfire reconstructable after the
    fact instead of guesswork."""

    kind: MessageKind
    trace_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = ""


@dataclass
class Candidate:
    """A pattern agent's proposal. Deliberately carries no policy verdict: it
    says "this pattern matches now", not "we should notify"."""

    domain: str                      # "ride" | "food"
    pattern_ref: str
    confidence: float
    trigger_reasons: list[str]
    fired_at: datetime
    hour_bin: int
    features: dict[str, Any] = field(default_factory=dict)

    def to_event(self) -> dict[str, Any]:
        """Shape expected by SuggestionBuilder.build()."""
        return {
            "type": self.domain,
            "trigger_reason": list(self.trigger_reasons),
            "confidence": self.confidence,
            "pattern_ref": self.pattern_ref,
            "fired_at": self.fired_at,
            "suppressed": False,
            "suppression_reason": None,
            "early_departure_delta": self.features.get("early_departure_delta", 0),
            "delivery_delay": bool(self.features.get("delivery_delay", False)),
            "soft": bool(self.features.get("soft", False)),
        }


# ---------------------------------------------------------------------------
# Policy, every knob the evaluation harness sweeps lives here
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TriggerPolicy:
    """All annoyance/precision knobs in one immutable object.

    Frozen so a sweep cannot accidentally mutate shared state between runs, and
    so a policy can be recorded verbatim alongside the metrics it produced.
    """

    ride_cooldown_minutes: int = 45
    food_cooldown_minutes: int = 30
    ride_confidence_threshold: float = 0.6
    food_confidence_threshold: float = 0.55
    ride_soft_threshold: float = 0.4
    allow_soft_ride: bool = True
    # After this many dismissals in the lookback window, raise the bar.
    dismissals_before_raise: int = 2
    raised_threshold: float = 0.8
    # After this many, stop showing the pattern at all.
    dismissals_before_suppress: int = 4
    dismissal_lookback_days: int = 7
    ride_tolerance_minutes: int = 20
    food_tolerance_minutes: int = 15
    pending_expiry_minutes: int = 10
    # Global floor between ANY two notifications, regardless of domain. A
    # per-domain cooldown alone still permits a ride and a food ping seconds
    # apart, which reads as spam to the user even though each was in policy.
    global_cooldown_minutes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ride_cooldown_minutes": self.ride_cooldown_minutes,
            "food_cooldown_minutes": self.food_cooldown_minutes,
            "ride_confidence_threshold": self.ride_confidence_threshold,
            "food_confidence_threshold": self.food_confidence_threshold,
            "global_cooldown_minutes": self.global_cooldown_minutes,
            "dismissals_before_raise": self.dismissals_before_raise,
            "dismissals_before_suppress": self.dismissals_before_suppress,
        }


# ---------------------------------------------------------------------------
# Agent base
# ---------------------------------------------------------------------------
class Agent:
    """One inbox, one consumer task, bounded work per message.

    Subclasses implement ``handle``. The base class owns the queue mechanics,
    error isolation, and per-agent counters so no subclass has to remember to.
    """

    name = "agent"

    def __init__(self, outbox: asyncio.Queue, *, timeout_seconds: float = 10.0) -> None:
        self.inbox: asyncio.Queue[Message | None] = asyncio.Queue()
        self.outbox = outbox
        self.timeout_seconds = timeout_seconds
        self._task: asyncio.Task | None = None
        self.processed = 0
        self.errors = 0
        self.timeouts = 0

    async def handle(self, message: Message) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._consume(), name=f"agent-{self.name}")

    async def stop(self) -> None:
        if not self._task:
            return
        await self.inbox.put(None)  # poison pill: drain in order, then exit
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None

    async def send(self, message: Message) -> None:
        await self.inbox.put(message)

    async def _consume(self) -> None:
        while True:
            message = await self.inbox.get()
            if message is None:
                return
            try:
                # The deadline is per message. Without it, one slow third-party
                # scrape becomes an unbounded stall for every later tick.
                await asyncio.wait_for(self.handle(message), timeout=self.timeout_seconds)
                self.processed += 1
            except asyncio.TimeoutError:
                self.timeouts += 1
                log.warning("%s timed out after %.1fs (trace=%s)", self.name,
                            self.timeout_seconds, message.trace_id)
                await self._emit_nothing(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors += 1
                log.exception("%s failed (trace=%s): %s", self.name, message.trace_id, exc)
                await self._emit_nothing(message)

    async def _emit_nothing(self, message: Message) -> None:
        """A failed agent must still answer, or the orchestrator waits out its
        whole join deadline for a reply that will never come."""
        await self.outbox.put(
            Message(kind=MessageKind.NO_CANDIDATE, trace_id=message.trace_id, source=self.name)
        )

    def metrics(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "errors": self.errors,
            "timeouts": self.timeouts,
            "queue_depth": self.inbox.qsize(),
        }


# ---------------------------------------------------------------------------
# 1. Ride pattern agent
# ---------------------------------------------------------------------------
class RidePatternAgent(Agent):
    """Matches the current moment against learned departure windows.

    Pure read of behavioural memory: no live network calls, no policy. That is
    what lets the evaluation harness replay it thousands of times cheaply.
    """

    name = "ride_pattern"

    def __init__(self, outbox: asyncio.Queue, policy_provider: Callable[[], TriggerPolicy]) -> None:
        super().__init__(outbox, timeout_seconds=5.0)
        self._policy = policy_provider

    async def handle(self, message: Message) -> None:
        policy = self._policy()
        now: datetime = message.payload["now"]
        minute_of_day = now.hour * 60 + now.minute

        matches = await asyncio.to_thread(
            db.find_matching_departure_patterns,
            now.weekday(),
            minute_of_day,
            policy.ride_tolerance_minutes,
        )
        if not matches:
            await self._emit_nothing(message)
            return

        # Strongest first: confidence, then how often, then most recent.
        matches.sort(key=lambda row: (-row["confidence"], -row["frequency"], row["last_seen"]))
        pattern = matches[0]

        candidate = Candidate(
            domain="ride",
            pattern_ref=f"departure_patterns:{pattern['id']}",
            confidence=float(pattern["confidence"]),
            trigger_reasons=["departure_window"],
            fired_at=now,
            hour_bin=int(pattern["hour_bin"]),
            features={
                "destination_id": pattern.get("destination_id"),
                "frequency": pattern.get("frequency"),
                "already_suppressed": bool(pattern.get("suppressed")),
            },
        )
        await self.outbox.put(
            Message(
                kind=MessageKind.CANDIDATE,
                trace_id=message.trace_id,
                payload={"candidate": candidate},
                source=self.name,
            )
        )


# ---------------------------------------------------------------------------
# 2. Food pattern agent
# ---------------------------------------------------------------------------
class FoodPatternAgent(Agent):
    """Matches the current moment against learned ordering windows."""

    name = "food_pattern"

    def __init__(self, outbox: asyncio.Queue, policy_provider: Callable[[], TriggerPolicy]) -> None:
        super().__init__(outbox, timeout_seconds=5.0)
        self._policy = policy_provider

    async def handle(self, message: Message) -> None:
        policy = self._policy()
        now: datetime = message.payload["now"]
        minute_of_day = now.hour * 60 + now.minute

        matches = await asyncio.to_thread(
            db.find_matching_order_patterns,
            now.weekday(),
            minute_of_day,
            policy.food_tolerance_minutes,
        )
        if not matches:
            await self._emit_nothing(message)
            return

        matches.sort(key=lambda row: (-row["confidence"], row["last_seen"]))
        pattern = matches[0]

        candidate = Candidate(
            domain="food",
            pattern_ref=f"order_patterns:{pattern['id']}",
            confidence=float(pattern["confidence"]),
            trigger_reasons=["order_window"],
            fired_at=now,
            hour_bin=int(pattern["hour_bin"]),
            features={
                "restaurant_id": pattern.get("restaurant_id"),
                "cuisine": pattern.get("cuisine"),
                "already_suppressed": bool(pattern.get("suppressed")),
            },
        )
        await self.outbox.put(
            Message(
                kind=MessageKind.CANDIDATE,
                trace_id=message.trace_id,
                payload={"candidate": candidate},
                source=self.name,
            )
        )


# ---------------------------------------------------------------------------
# 3. Live context agent
# ---------------------------------------------------------------------------
class LiveContextAgent(Agent):
    """Enriches a candidate with live signals (traffic, delivery ETA).

    This is the only agent that touches the network, so it is the only one with
    a long deadline, and the only one whose failure must be non-fatal. A
    candidate that cannot be enriched is still a valid candidate; it just loses
    the traffic/delay boost.
    """

    name = "live_context"

    def __init__(
        self,
        outbox: asyncio.Queue,
        suggestion_builder: SuggestionBuilder | None,
        *,
        enabled: bool = True,
        timeout_seconds: float = 12.0,
    ) -> None:
        super().__init__(outbox, timeout_seconds=timeout_seconds)
        self.suggestion_builder = suggestion_builder
        self.enabled = enabled

    async def handle(self, message: Message) -> None:
        candidate: Candidate = message.payload["candidate"]

        if not self.enabled or self.suggestion_builder is None:
            await self._forward(message, candidate)
            return

        try:
            if candidate.domain == "ride":
                candidate = await self._enrich_ride(candidate)
            else:
                candidate = await self._enrich_food(candidate)
        except Exception as exc:
            # Degrade, do not drop: losing the boost is much better than losing
            # a correct suggestion because a third-party page changed its DOM.
            log.warning("live enrichment failed for %s (%s); continuing unenriched",
                        candidate.pattern_ref, exc)

        await self._forward(message, candidate)

    async def _forward(self, message: Message, candidate: Candidate) -> None:
        await self.outbox.put(
            Message(
                kind=MessageKind.ENRICHED,
                trace_id=message.trace_id,
                payload={"candidate": candidate},
                source=self.name,
            )
        )

    async def _enrich_ride(self, candidate: Candidate) -> Candidate:
        destination_id = candidate.features.get("destination_id")
        if not destination_id:
            return candidate

        cluster = await asyncio.to_thread(db.get_destination_cluster, int(destination_id))
        rides = await asyncio.to_thread(db.get_rides_for_destination, int(destination_id))
        if not cluster or not rides:
            return candidate

        last_ride = rides[0]
        origin = {"lat": last_ride.get("origin_lat"), "lng": last_ride.get("origin_lng")}
        destination = {"lat": cluster["centroid_lat"], "lng": cluster["centroid_lng"]}

        live = await self.suggestion_builder.fetch_route_travel_time(origin, destination)
        durations = [float(r["duration_min"]) for r in rides if r.get("duration_min") is not None]
        historical = sum(durations) / len(durations) if durations else None

        # Traffic materially worse than usual is itself evidence the user wants
        # to leave now, so it both boosts confidence and pulls departure earlier.
        if live and historical and live > historical * 1.25:
            features = dict(candidate.features)
            features["early_departure_delta"] = max(0, round(live - historical))
            features["live_travel_min"] = live
            features["historical_travel_min"] = historical
            return replace(
                candidate,
                trigger_reasons=candidate.trigger_reasons + ["traffic_deviation"],
                confidence=min(1.0, candidate.confidence + 0.1),
                features=features,
            )
        return candidate

    async def _enrich_food(self, candidate: Candidate) -> Candidate:
        restaurant_id = candidate.features.get("restaurant_id")
        if not restaurant_id:
            return candidate

        recent_order = await asyncio.to_thread(db.get_recent_order_for_restaurant, restaurant_id)
        status = await self.suggestion_builder.fetch_restaurant_status(restaurant_id, recent_order)
        payload = self.suggestion_builder._status_payload(status)
        current_eta = payload.get("current_eta")
        historical = recent_order.get("delivery_time_min") if recent_order else None

        if (
            isinstance(current_eta, (int, float))
            and isinstance(historical, (int, float))
            and current_eta > historical * 1.3
        ):
            features = dict(candidate.features)
            features["delivery_delay"] = True
            features["early_departure_delta"] = max(0, round(current_eta - historical))
            return replace(
                candidate,
                trigger_reasons=candidate.trigger_reasons + ["delivery_delay"],
                confidence=min(1.0, candidate.confidence + 0.1),
                features=features,
            )
        return candidate


# ---------------------------------------------------------------------------
# 4. Decision agent, the only place policy is applied
# ---------------------------------------------------------------------------
@dataclass
class Verdict:
    fire: bool
    reason: str                  # why we fired, or why we suppressed
    candidate: Candidate | None = None


class DecisionAgent(Agent):
    """Applies cooldown, dismissal, duplicate-action and suppression policy.

    Concentrating policy here has a concrete payoff: the evaluation harness can
    hold behaviour extraction fixed and sweep only this agent's knobs, so a
    precision change is attributable to the policy and nothing else.
    """

    name = "decision"

    def __init__(self, outbox: asyncio.Queue, policy_provider: Callable[[], TriggerPolicy]) -> None:
        super().__init__(outbox, timeout_seconds=8.0)
        self._policy = policy_provider

    async def handle(self, message: Message) -> None:
        candidate: Candidate = message.payload["candidate"]
        verdict = await asyncio.to_thread(self.evaluate, candidate, candidate.fired_at)
        await self.outbox.put(
            Message(
                kind=MessageKind.DECISION,
                trace_id=message.trace_id,
                payload={"verdict": verdict},
                source=self.name,
            )
        )

    # Synchronous and side-effect-free apart from suppression bookkeeping, so
    # the eval harness can call it directly, in-process, 200x per sweep point.
    def evaluate(self, candidate: Candidate, now: datetime) -> Verdict:
        policy = self._policy()
        domain = candidate.domain

        # -- 1. per-domain cooldown: how soon may we ping about this domain again
        cooldown = (
            policy.ride_cooldown_minutes if domain == "ride" else policy.food_cooldown_minutes
        )
        last = db.get_last_trigger_time(domain)
        if last and (now - last) < timedelta(minutes=cooldown):
            return Verdict(False, f"cooldown:{domain}:{cooldown}m")

        # -- 2. global cooldown across domains, so ride+food cannot double-ping
        if policy.global_cooldown_minutes > 0:
            others = [d for d in ("ride", "food") if d != domain]
            for other in others:
                last_other = db.get_last_trigger_time(other)
                if last_other and (now - last_other) < timedelta(
                    minutes=policy.global_cooldown_minutes
                ):
                    return Verdict(False, f"cooldown:global:{policy.global_cooldown_minutes}m")

        # -- 3. dismissal history raises the bar, then silences entirely
        dismissals = db.count_recent_dismissals_for_pattern(
            candidate.pattern_ref, days=policy.dismissal_lookback_days, now=now
        )
        if dismissals >= policy.dismissals_before_suppress or candidate.features.get(
            "already_suppressed"
        ):
            db.insert_trigger_event(
                domain, candidate.trigger_reasons, candidate.confidence, now, suppressed=True
            )
            return Verdict(False, f"suppressed:dismissals={dismissals}")

        # -- 4. confidence threshold, with a lower "soft" tier for rides
        base = (
            policy.ride_confidence_threshold
            if domain == "ride"
            else policy.food_confidence_threshold
        )
        threshold = base
        soft = False
        if (
            domain == "ride"
            and policy.allow_soft_ride
            and policy.ride_soft_threshold <= candidate.confidence < base
        ):
            soft = True
            threshold = policy.ride_soft_threshold
        if dismissals >= policy.dismissals_before_raise:
            threshold = max(threshold, policy.raised_threshold)
        if candidate.confidence < threshold:
            return Verdict(False, f"below_threshold:{candidate.confidence:.2f}<{threshold:.2f}")

        # -- 5. the user may have already done the thing themselves
        center_minute = candidate.hour_bin * 15
        if domain == "ride":
            destination_id = candidate.features.get("destination_id")
            if destination_id and db.any_ride_booked_today_in_window(
                int(destination_id), center_minute, policy.ride_tolerance_minutes, now
            ):
                db.insert_trigger_event(
                    domain, candidate.trigger_reasons, candidate.confidence, now, suppressed=True
                )
                return Verdict(False, "already_booked_today")
        else:
            restaurant_id = candidate.features.get("restaurant_id")
            if restaurant_id and db.any_order_today_in_window(
                restaurant_id, center_minute, policy.food_tolerance_minutes, now
            ):
                db.insert_trigger_event(
                    domain, candidate.trigger_reasons, candidate.confidence, now, suppressed=True
                )
                return Verdict(False, "already_ordered_today")

        features = dict(candidate.features)
        features["soft"] = soft
        return Verdict(True, "fire", replace(candidate, features=features))


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
class Orchestrator:
    """Owns the four sub-agents and the message flow between them.

    ``tick()`` is one full pipeline pass and is the single decision path used by
    both the live app and the evaluation harness, so the two cannot drift.
    """

    def __init__(
        self,
        *,
        pattern_engine: PatternEngine,
        suggestion_builder: SuggestionBuilder | None,
        broadcaster: Any | None = None,
        policy: TriggerPolicy | None = None,
        live_context_enabled: bool = True,
        join_timeout_seconds: float = 20.0,
    ) -> None:
        self.pattern_engine = pattern_engine
        self.suggestion_builder = suggestion_builder
        self.broadcaster = broadcaster
        self.policy = policy or TriggerPolicy()
        self.join_timeout_seconds = join_timeout_seconds

        # One shared results queue: agents reply here, the orchestrator joins on
        # it. Cheaper than a reply queue per message and keeps ordering visible.
        self.results: asyncio.Queue[Message] = asyncio.Queue()

        provider = lambda: self.policy  # noqa: E731 - read the CURRENT policy each tick
        self.ride_agent = RidePatternAgent(self.results, provider)
        self.food_agent = FoodPatternAgent(self.results, provider)
        self.live_agent = LiveContextAgent(
            self.results, suggestion_builder, enabled=live_context_enabled
        )
        self.decision_agent = DecisionAgent(self.results, provider)

        self.agents: list[Agent] = [
            self.ride_agent,
            self.food_agent,
            self.live_agent,
            self.decision_agent,
        ]
        self.ticks = 0
        self.fired = 0
        self.suppressed_reasons: dict[str, int] = {}

    def set_policy(self, policy: TriggerPolicy) -> None:
        self.policy = policy

    async def start(self) -> None:
        for agent in self.agents:
            await agent.start()

    async def stop(self) -> None:
        for agent in self.agents:
            await agent.stop()

    async def _collect(self, expected: int, kinds: set[MessageKind], trace_id: str,
                       deadline: float) -> list[Message]:
        """Wait for `expected` replies of the given kinds, or until the deadline.

        Bounded on purpose: if an agent dies without replying, the tick still
        completes with what it has instead of hanging the loop forever.
        """
        collected: list[Message] = []
        loop = asyncio.get_running_loop()
        while len(collected) < expected:
            remaining = deadline - loop.time()
            if remaining <= 0:
                log.warning("join deadline hit: %d/%d replies (trace=%s)",
                            len(collected), expected, trace_id)
                break
            try:
                message = await asyncio.wait_for(self.results.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if message.trace_id != trace_id:
                continue  # a reply from a previous, abandoned tick
            if message.kind in kinds or message.kind is MessageKind.NO_CANDIDATE:
                collected.append(message)
        return collected

    async def tick(self, now: datetime | None = None) -> dict[str, Any] | None:
        """One pipeline pass. Returns the published payload, or None."""
        self.ticks += 1
        current = now or db.utc_now()
        trace_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.join_timeout_seconds

        # -- expire stale pending suggestions and learn from the silence
        expired = await asyncio.to_thread(
            db.mark_expired_pending_suggestions,
            timeout_minutes=self.policy.pending_expiry_minutes,
            now=current,
        )
        for suggestion in expired:
            payload = suggestion["payload"]
            await asyncio.to_thread(
                self.pattern_engine.reweight,
                {"pattern_ref": payload.get("pattern_ref"), "event_type": "ignored", "edits": None},
            )

        # -- stage 1: fan out to both pattern agents CONCURRENTLY
        tick_msg = Message(kind=MessageKind.TICK, trace_id=trace_id, payload={"now": current},
                           source="orchestrator")
        await self.ride_agent.send(tick_msg)
        await self.food_agent.send(tick_msg)

        replies = await self._collect(2, {MessageKind.CANDIDATE}, trace_id, deadline)
        candidates = [
            m.payload["candidate"] for m in replies if m.kind is MessageKind.CANDIDATE
        ]
        if not candidates:
            return None

        # -- stage 2: enrich with live context
        for candidate in candidates:
            await self.live_agent.send(
                Message(kind=MessageKind.ENRICHED, trace_id=trace_id,
                        payload={"candidate": candidate}, source="orchestrator")
            )
        enriched_msgs = await self._collect(len(candidates), {MessageKind.ENRICHED}, trace_id,
                                           deadline)

        # Enrichment is optional, so a candidate that did not come back enriched
        # must still be considered, keyed by pattern_ref so a partial failure
        # (one of two candidates timing out) degrades just that one instead of
        # silently dropping it. Losing a correct suggestion because a third-party
        # scrape was slow is the exact failure this pipeline exists to prevent.
        by_ref = {
            m.payload["candidate"].pattern_ref: m.payload["candidate"]
            for m in enriched_msgs
            if m.kind is MessageKind.ENRICHED
        }
        enriched = [by_ref.get(c.pattern_ref, c) for c in candidates]

        # -- stage 3: policy. Rides outrank food at equal confidence: a missed
        #    departure costs the user a late arrival, a missed meal prompt does not.
        enriched.sort(key=lambda c: (-c.confidence, 0 if c.domain == "ride" else 1))

        for candidate in enriched:
            await self.decision_agent.send(
                Message(kind=MessageKind.DECISION, trace_id=trace_id,
                        payload={"candidate": candidate}, source="orchestrator")
            )
        verdict_msgs = await self._collect(len(enriched), {MessageKind.DECISION}, trace_id, deadline)

        verdicts = [m.payload["verdict"] for m in verdict_msgs if m.kind is MessageKind.DECISION]
        for verdict in verdicts:
            if not verdict.fire:
                key = verdict.reason.split(":")[0]
                self.suppressed_reasons[key] = self.suppressed_reasons.get(key, 0) + 1

        # At most one notification per tick: the point of the system is to
        # interrupt rarely and correctly.
        winner = next((v for v in verdicts if v.fire), None)
        if not winner or not winner.candidate:
            return None

        self.fired += 1
        return await self._publish(winner.candidate, trace_id)

    async def _publish(self, candidate: Candidate, trace_id: str) -> dict[str, Any]:
        event = candidate.to_event()
        trigger_id = await asyncio.to_thread(
            db.insert_trigger_event,
            candidate.domain,
            candidate.trigger_reasons,
            candidate.confidence,
            candidate.fired_at,
            suppressed=False,
        )

        if self.suggestion_builder is not None:
            suggestion = await self.suggestion_builder.build(event)
        else:
            suggestion = {"reason_string": f"{candidate.domain} pattern match",
                          "pattern_ref": candidate.pattern_ref}

        suggestion_id = await asyncio.to_thread(
            db.insert_suggestion,
            trigger_id,
            candidate.domain,
            suggestion,
            suggestion["reason_string"],
            candidate.fired_at,
        )

        payload = {
            "event": {
                "id": trigger_id,
                "type": candidate.domain,
                "trigger_reason": candidate.trigger_reasons,
                "confidence": candidate.confidence,
                "pattern_ref": candidate.pattern_ref,
                "fired_at": candidate.fired_at.isoformat(),
                "suppressed": False,
                "suppression_reason": None,
                "trace_id": trace_id,
            },
            "suggestion": {
                "id": suggestion_id,
                "type": candidate.domain,
                "reason_string": suggestion["reason_string"],
                "payload": suggestion,
            },
        }
        if self.broadcaster is not None:
            await self.broadcaster.publish(payload)
        return payload

    def metrics(self) -> dict[str, Any]:
        return {
            "ticks": self.ticks,
            "fired": self.fired,
            "fire_rate": self.fired / self.ticks if self.ticks else 0.0,
            "suppressed_reasons": dict(self.suppressed_reasons),
            "agents": {agent.name: agent.metrics() for agent in self.agents},
            "policy": self.policy.as_dict(),
        }
