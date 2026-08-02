"""Tests for the agent pipeline: message flow, failure isolation, and policy.

The behaviours worth testing here are the ones that only exist because of the
multi-agent restructure, concurrency, per-agent deadlines, and error isolation.
A test that only checked "a suggestion came out" would pass equally well against
the old sequential loop and would prove nothing about the change.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Redirect persistence to a scratch file before the package is imported.
os.environ.setdefault(
    "ASSISTANT_DB_PATH", os.path.join(tempfile.gettempdir(), "proact_test_boot.db")
)

from proactive_assistant_app import database as db  # noqa: E402
from proactive_assistant_app.agents import (  # noqa: E402
    Agent,
    Candidate,
    DecisionAgent,
    FoodPatternAgent,
    LiveContextAgent,
    Message,
    MessageKind,
    Orchestrator,
    RidePatternAgent,
    TriggerPolicy,
)

UTC = timezone.utc


@pytest.fixture()
def fresh_db(tmp_path):
    """Each test gets its own database, so tests cannot leak state into each other."""
    db.DB_PATH = str(tmp_path / "test.db")
    db.init_db()
    return db.DB_PATH


def _now() -> datetime:
    return datetime(2026, 3, 3, 9, 0, tzinfo=UTC)  # a Tuesday


def _candidate(domain: str = "ride", confidence: float = 0.9, **features) -> Candidate:
    return Candidate(
        domain=domain,
        pattern_ref=f"{'departure' if domain == 'ride' else 'order'}_patterns:1",
        confidence=confidence,
        trigger_reasons=["departure_window" if domain == "ride" else "order_window"],
        fired_at=_now(),
        hour_bin=36,  # 09:00
        features=features,
    )


# ---------------------------------------------------------------------------
# Policy: the DecisionAgent is the only place policy lives, so test it directly.
# ---------------------------------------------------------------------------
class TestDecisionPolicy:
    def _agent(self, policy: TriggerPolicy) -> DecisionAgent:
        return DecisionAgent(asyncio.Queue(), lambda: policy)

    def test_fires_when_nothing_blocks(self, fresh_db):
        agent = self._agent(TriggerPolicy())
        verdict = agent.evaluate(_candidate(confidence=0.9), _now())
        assert verdict.fire
        assert verdict.reason == "fire"

    def test_below_threshold_does_not_fire(self, fresh_db):
        # 0.30 is under both the ride threshold (0.60) and the soft tier (0.40).
        agent = self._agent(TriggerPolicy())
        verdict = agent.evaluate(_candidate(confidence=0.30), _now())
        assert not verdict.fire
        assert "below_threshold" in verdict.reason

    def test_soft_tier_lets_a_mid_confidence_ride_through(self, fresh_db):
        agent = self._agent(TriggerPolicy())
        verdict = agent.evaluate(_candidate(confidence=0.50), _now())
        assert verdict.fire
        assert verdict.candidate.features["soft"] is True

    def test_soft_tier_is_ride_only(self, fresh_db):
        # Food has no soft tier: 0.50 < 0.55 means silence.
        agent = self._agent(TriggerPolicy())
        verdict = agent.evaluate(_candidate(domain="food", confidence=0.50), _now())
        assert not verdict.fire

    def test_cooldown_blocks_a_second_trigger(self, fresh_db):
        policy = TriggerPolicy(ride_cooldown_minutes=45)
        agent = self._agent(policy)
        now = _now()

        # A trigger 10 minutes ago is inside a 45-minute cooldown.
        db.insert_trigger_event("ride", ["departure_window"], 0.9, now - timedelta(minutes=10))
        verdict = agent.evaluate(_candidate(), now)
        assert not verdict.fire
        assert "cooldown:ride" in verdict.reason

    def test_cooldown_expires(self, fresh_db):
        policy = TriggerPolicy(ride_cooldown_minutes=45)
        agent = self._agent(policy)
        now = _now()
        db.insert_trigger_event("ride", ["departure_window"], 0.9, now - timedelta(minutes=50))
        assert agent.evaluate(_candidate(), now).fire

    def test_cooldown_is_per_domain_by_default(self, fresh_db):
        """A recent RIDE trigger must not silence a FOOD suggestion when the
        global cooldown is off, they are independent product surfaces."""
        agent = self._agent(TriggerPolicy(global_cooldown_minutes=0))
        now = _now()
        db.insert_trigger_event("ride", ["departure_window"], 0.9, now - timedelta(minutes=5))
        assert agent.evaluate(_candidate(domain="food", confidence=0.9), now).fire

    def test_global_cooldown_blocks_across_domains(self, fresh_db):
        """…but with a global floor set, it must. A ride ping and a food ping
        seconds apart reads as spam even though each was individually in policy."""
        agent = self._agent(TriggerPolicy(global_cooldown_minutes=20))
        now = _now()
        db.insert_trigger_event("ride", ["departure_window"], 0.9, now - timedelta(minutes=5))
        verdict = agent.evaluate(_candidate(domain="food", confidence=0.9), now)
        assert not verdict.fire
        assert "cooldown:global" in verdict.reason

    def test_already_suppressed_pattern_is_silent(self, fresh_db):
        agent = self._agent(TriggerPolicy())
        verdict = agent.evaluate(_candidate(already_suppressed=True), _now())
        assert not verdict.fire
        assert "suppressed" in verdict.reason

    def test_dismissals_raise_the_bar_then_silence(self, fresh_db):
        """Two dismissals raise the threshold to 0.8; four suppress entirely."""
        policy = TriggerPolicy()
        agent = self._agent(policy)
        now = _now()
        ref = "departure_patterns:1"

        def add_dismissal(when: datetime) -> None:
            tid = db.insert_trigger_event("ride", ["x"], 0.9, when)
            sid = db.insert_suggestion(tid, "ride", {"pattern_ref": ref}, "r", when)
            with db.get_db() as conn:
                conn.execute(
                    "INSERT INTO dismissed_suggestions(suggestion_id, dismissed_at) VALUES (?, ?)",
                    (sid, when.isoformat()),
                )

        for i in range(2):
            add_dismissal(now - timedelta(days=i + 1))

        # 0.7 cleared the 0.6 bar before; with 2 dismissals the bar is 0.8.
        assert not agent.evaluate(_candidate(confidence=0.70), now).fire
        assert agent.evaluate(_candidate(confidence=0.85), now).fire

        for i in range(2, 4):
            add_dismissal(now - timedelta(days=i + 1))

        # At 4 dismissals even high confidence must stay quiet.
        verdict = agent.evaluate(_candidate(confidence=0.99), now)
        assert not verdict.fire
        assert "suppressed" in verdict.reason

    def test_dismissals_outside_the_lookback_do_not_count(self, fresh_db):
        """The lookback is a window, not a lifetime ban, a user who dismissed
        something months ago should get another chance."""
        policy = TriggerPolicy(dismissal_lookback_days=7)
        agent = self._agent(policy)
        now = _now()
        ref = "departure_patterns:1"
        for i in range(6):
            when = now - timedelta(days=30 + i)
            tid = db.insert_trigger_event("ride", ["x"], 0.9, when)
            sid = db.insert_suggestion(tid, "ride", {"pattern_ref": ref}, "r", when)
            with db.get_db() as conn:
                conn.execute(
                    "INSERT INTO dismissed_suggestions(suggestion_id, dismissed_at) VALUES (?, ?)",
                    (sid, when.isoformat()),
                )
        assert agent.evaluate(_candidate(confidence=0.9), now).fire


# ---------------------------------------------------------------------------
# Agent mechanics: the reason the restructure exists.
# ---------------------------------------------------------------------------
class BoomAgent(Agent):
    name = "boom"

    async def handle(self, message: Message) -> None:
        raise RuntimeError("deliberate failure")


class SlowAgent(Agent):
    name = "slow"

    async def handle(self, message: Message) -> None:
        await asyncio.sleep(10)  # far beyond its deadline


class TestAgentMechanics:
    @pytest.mark.asyncio
    async def test_a_throwing_agent_still_answers(self):
        """If a failed agent stayed silent, the orchestrator would burn its whole
        join deadline waiting for a reply that never comes."""
        outbox: asyncio.Queue = asyncio.Queue()
        agent = BoomAgent(outbox)
        await agent.start()
        await agent.send(Message(kind=MessageKind.TICK, trace_id="t1"))

        reply = await asyncio.wait_for(outbox.get(), timeout=2)
        assert reply.kind is MessageKind.NO_CANDIDATE
        assert reply.trace_id == "t1"
        assert agent.errors == 1
        await agent.stop()

    @pytest.mark.asyncio
    async def test_a_hung_agent_times_out_and_answers(self):
        outbox: asyncio.Queue = asyncio.Queue()
        agent = SlowAgent(outbox, timeout_seconds=0.2)
        await agent.start()
        await agent.send(Message(kind=MessageKind.TICK, trace_id="t2"))

        reply = await asyncio.wait_for(outbox.get(), timeout=3)
        assert reply.kind is MessageKind.NO_CANDIDATE
        assert agent.timeouts == 1
        await agent.stop()

    @pytest.mark.asyncio
    async def test_an_agent_survives_a_failure_and_keeps_working(self):
        """Error isolation means one bad message must not kill the consumer."""
        outbox: asyncio.Queue = asyncio.Queue()
        agent = BoomAgent(outbox)
        await agent.start()
        for i in range(3):
            await agent.send(Message(kind=MessageKind.TICK, trace_id=f"t{i}"))
        for _ in range(3):
            await asyncio.wait_for(outbox.get(), timeout=2)
        assert agent.errors == 3  # still alive after the first two
        await agent.stop()

    @pytest.mark.asyncio
    async def test_live_context_degrades_instead_of_dropping(self):
        """A broken third-party scrape must cost the enrichment, not the candidate."""

        class BrokenBuilder:
            async def fetch_route_travel_time(self, *a, **k):
                raise RuntimeError("playwright exploded")

            async def fetch_restaurant_status(self, *a, **k):
                raise RuntimeError("playwright exploded")

            def _status_payload(self, s):
                return {}

        outbox: asyncio.Queue = asyncio.Queue()
        agent = LiveContextAgent(outbox, BrokenBuilder(), enabled=True, timeout_seconds=2)
        await agent.start()
        original = _candidate(destination_id=1)
        await agent.send(
            Message(kind=MessageKind.ENRICHED, trace_id="t3", payload={"candidate": original})
        )

        reply = await asyncio.wait_for(outbox.get(), timeout=3)
        assert reply.kind is MessageKind.ENRICHED
        # The candidate came through intact, just without the traffic boost.
        assert reply.payload["candidate"].pattern_ref == original.pattern_ref
        assert "traffic_deviation" not in reply.payload["candidate"].trigger_reasons
        await agent.stop()


# ---------------------------------------------------------------------------
# Orchestrator end to end
# ---------------------------------------------------------------------------
class _NullEngine:
    def reweight(self, feedback):
        pass

    def run_full_extraction(self):
        return {}


class TestOrchestrator:
    def _orch(self, policy: TriggerPolicy | None = None) -> Orchestrator:
        return Orchestrator(
            pattern_engine=_NullEngine(),
            suggestion_builder=None,
            broadcaster=None,
            policy=policy or TriggerPolicy(),
            live_context_enabled=False,
            join_timeout_seconds=5.0,
        )

    @pytest.mark.asyncio
    async def test_tick_with_no_patterns_returns_none(self, fresh_db):
        orch = self._orch()
        await orch.start()
        try:
            assert await orch.tick(now=_now()) is None
            assert orch.ticks == 1
            assert orch.fired == 0
        finally:
            await orch.stop()

    @pytest.mark.asyncio
    async def test_both_pattern_agents_are_dispatched_each_tick(self, fresh_db):
        """Concurrency is the point of the restructure: both must be asked, every
        tick, rather than food only being reached if ride declined."""
        orch = self._orch()
        await orch.start()
        try:
            await orch.tick(now=_now())
            assert orch.ride_agent.processed == 1
            assert orch.food_agent.processed == 1
        finally:
            await orch.stop()

    @pytest.mark.asyncio
    async def test_at_most_one_notification_per_tick(self, fresh_db):
        """Even with both a ride and a food candidate available, one tick may
        interrupt the user at most once."""
        orch = self._orch()
        await orch.start()
        try:
            # Stub both pattern agents to always produce a strong candidate.
            async def ride_handle(message):
                await orch.results.put(
                    Message(MessageKind.CANDIDATE, message.trace_id,
                            {"candidate": _candidate("ride", 0.95)}, "ride_pattern")
                )

            async def food_handle(message):
                await orch.results.put(
                    Message(MessageKind.CANDIDATE, message.trace_id,
                            {"candidate": _candidate("food", 0.95)}, "food_pattern")
                )

            orch.ride_agent.handle = ride_handle
            orch.food_agent.handle = food_handle

            result = await orch.tick(now=_now())
            assert result is not None
            # Rides outrank food at equal confidence: a missed departure costs
            # more than a missed meal prompt.
            assert result["event"]["type"] == "ride"
            assert orch.fired == 1
        finally:
            await orch.stop()

    @pytest.mark.asyncio
    async def test_enrichment_timeout_does_not_drop_the_candidate(self, fresh_db):
        """A candidate whose live enrichment times out must still reach policy.
        Dropping it would mean losing a correct suggestion because a third-party
        scrape was slow, the exact failure this pipeline exists to prevent."""
        orch = self._orch()
        await orch.start()
        try:
            async def ride_handle(message):
                await orch.results.put(
                    Message(MessageKind.CANDIDATE, message.trace_id,
                            {"candidate": _candidate("ride", 0.95)}, "ride_pattern")
                )

            async def food_handle(message):
                await orch.results.put(
                    Message(MessageKind.NO_CANDIDATE, message.trace_id, {}, "food_pattern")
                )

            # Live enrichment hangs past its deadline, so the base class will
            # answer NO_CANDIDATE for it rather than an ENRICHED candidate.
            async def hang(message):
                await asyncio.sleep(10)

            orch.ride_agent.handle = ride_handle
            orch.food_agent.handle = food_handle
            orch.live_agent.handle = hang
            orch.live_agent.timeout_seconds = 0.2

            result = await orch.tick(now=_now())
            assert result is not None, "candidate was dropped when enrichment timed out"
            assert result["event"]["type"] == "ride"
            assert orch.live_agent.timeouts == 1
        finally:
            await orch.stop()

    @pytest.mark.asyncio
    async def test_metrics_expose_per_agent_state(self, fresh_db):
        orch = self._orch()
        await orch.start()
        try:
            await orch.tick(now=_now())
            m = orch.metrics()
            assert m["ticks"] == 1
            assert set(m["agents"]) == {
                "ride_pattern", "food_pattern", "live_context", "decision"
            }
            assert "policy" in m
        finally:
            await orch.stop()

    @pytest.mark.asyncio
    async def test_policy_can_be_swapped_between_ticks(self, fresh_db):
        """The sweep depends on this: agents read the CURRENT policy each tick
        rather than capturing one at construction."""
        orch = self._orch(TriggerPolicy(ride_cooldown_minutes=45))
        await orch.start()
        try:
            assert orch.decision_agent._policy().ride_cooldown_minutes == 45
            orch.set_policy(TriggerPolicy(ride_cooldown_minutes=5))
            assert orch.decision_agent._policy().ride_cooldown_minutes == 5
        finally:
            await orch.stop()


class TestTriggerPolicy:
    def test_policy_is_immutable(self):
        """Frozen so a sweep cannot leak state between points, and so a policy can
        be recorded verbatim next to the metrics it produced."""
        policy = TriggerPolicy()
        with pytest.raises(Exception):
            policy.ride_cooldown_minutes = 10  # type: ignore[misc]

    def test_defaults_match_the_documented_behaviour(self):
        p = TriggerPolicy()
        assert p.ride_cooldown_minutes == 45
        assert p.food_cooldown_minutes == 30
        assert p.ride_tolerance_minutes == 20
        assert p.food_tolerance_minutes == 15
        assert p.dismissals_before_suppress == 4
