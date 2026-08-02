"""Persistent asyncio trigger watcher for proactive suggestions."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import datetime, timezone
from typing import Any

from . import database as db
from .agents import Orchestrator, TriggerPolicy
from .pattern_engine import PatternEngine
from .suggestion_builder import SuggestionBuilder

log = logging.getLogger(__name__)
UTC = timezone.utc


def _parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class SuggestionBroadcaster:
    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()

    async def connect(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._queues.add(queue)
        return queue

    def disconnect(self, queue: asyncio.Queue) -> None:
        self._queues.discard(queue)

    async def publish(self, payload: dict[str, Any]) -> None:
        for queue in list(self._queues):
            await queue.put(payload)


class TriggerWatcher:
    """Timing shell around the agent pipeline.

    This class owns *when* evaluation happens; `Orchestrator` owns *what* it
    decides. All decision logic used to live here, duplicated from nothing --
    which meant there was no way to evaluate it offline without reimplementing
    it (and then evaluating the reimplementation). It now delegates to the same
    Orchestrator the evaluation harness drives, so the measured policy and the
    shipped policy are the same code.
    """

    def __init__(
        self,
        *,
        pattern_engine: PatternEngine,
        suggestion_builder: SuggestionBuilder,
        broadcaster: SuggestionBroadcaster,
        poll_interval_seconds: int = 60,
        ride_cooldown_minutes: int | None = None,
        food_cooldown_minutes: int | None = None,
        policy: TriggerPolicy | None = None,
    ) -> None:
        self.pattern_engine = pattern_engine
        self.suggestion_builder = suggestion_builder
        self.broadcaster = broadcaster
        self.poll_interval_seconds = poll_interval_seconds

        base = policy or TriggerPolicy()
        # Keep the older explicit cooldown kwargs working.
        overrides: dict[str, Any] = {}
        if ride_cooldown_minutes is not None:
            overrides["ride_cooldown_minutes"] = ride_cooldown_minutes
        if food_cooldown_minutes is not None:
            overrides["food_cooldown_minutes"] = food_cooldown_minutes
        if overrides:
            base = dataclasses.replace(base, **overrides)

        self.orchestrator = Orchestrator(
            pattern_engine=pattern_engine,
            suggestion_builder=suggestion_builder,
            broadcaster=broadcaster,
            policy=base,
        )
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def policy(self) -> TriggerPolicy:
        return self.orchestrator.policy

    @property
    def ride_cooldown_minutes(self) -> int:
        return self.orchestrator.policy.ride_cooldown_minutes

    @property
    def food_cooldown_minutes(self) -> int:
        return self.orchestrator.policy.food_cooldown_minutes

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        await self.orchestrator.start()
        self._task = asyncio.create_task(self._run_loop(), name="trigger-watcher")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.orchestrator.stop()

    async def pulse(self, now: datetime | None = None) -> dict[str, Any] | None:
        return await self.orchestrator.tick(now)

    def metrics(self) -> dict[str, Any]:
        return self.orchestrator.metrics()

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.pulse()
            except Exception as exc:
                log.exception("Trigger watcher cycle failed: %s", exc)
            await asyncio.sleep(self.poll_interval_seconds)
