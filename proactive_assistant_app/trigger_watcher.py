"""Persistent asyncio trigger watcher for proactive suggestions."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from . import database as db
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
    def __init__(
        self,
        *,
        pattern_engine: PatternEngine,
        suggestion_builder: SuggestionBuilder,
        broadcaster: SuggestionBroadcaster,
        poll_interval_seconds: int = 60,
        ride_cooldown_minutes: int = 45,
        food_cooldown_minutes: int = 30,
    ) -> None:
        self.pattern_engine = pattern_engine
        self.suggestion_builder = suggestion_builder
        self.broadcaster = broadcaster
        self.poll_interval_seconds = poll_interval_seconds
        self.ride_cooldown_minutes = ride_cooldown_minutes
        self.food_cooldown_minutes = food_cooldown_minutes
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
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

    async def pulse(self, now: datetime | None = None) -> dict[str, Any] | None:
        current = now or db.utc_now()
        expired = db.mark_expired_pending_suggestions(timeout_minutes=10)
        for suggestion in expired:
            payload = suggestion["payload"]
            self.pattern_engine.reweight(
                {
                    "pattern_ref": payload.get("pattern_ref"),
                    "event_type": "ignored",
                    "edits": None,
                }
            )

        ride_soft = await self._check_ride(current, allow_soft=True)
        if ride_soft and not ride_soft.get("soft"):
            return await self._emit_trigger(ride_soft)

        food_event = await self._check_food(current)
        if food_event:
            return await self._emit_trigger(food_event)

        if ride_soft and ride_soft.get("soft"):
            return await self._emit_trigger(ride_soft)
        return None

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.pulse()
            except Exception as exc:
                log.exception("Trigger watcher cycle failed: %s", exc)
            await asyncio.sleep(self.poll_interval_seconds)

    async def _check_ride(self, now: datetime, allow_soft: bool = True) -> dict[str, Any] | None:
        last_trigger = db.get_last_trigger_time("ride")
        if last_trigger and (now - last_trigger) < timedelta(minutes=self.ride_cooldown_minutes):
            return None

        minute_of_day = now.hour * 60 + now.minute
        matches = db.find_matching_departure_patterns(now.weekday(), minute_of_day, tolerance_minutes=20)
        if not matches:
            return None

        matches.sort(key=lambda row: (-row["confidence"], -row["frequency"], row["last_seen"]))
        pattern = matches[0]
        threshold = 0.6
        soft = False
        if pattern["confidence"] < 0.6 and pattern["confidence"] >= 0.4 and allow_soft:
            soft = True
            threshold = 0.4
        dismissal_count = db.count_recent_dismissals_for_pattern(f"departure_patterns:{pattern['id']}", days=7)
        if dismissal_count >= 4 or pattern.get("suppressed"):
            db.insert_trigger_event("ride", ["departure_window"], pattern["confidence"], now, suppressed=True)
            return None
        if dismissal_count >= 2:
            threshold = max(threshold, 0.8)
        if pattern["confidence"] < threshold:
            return None

        center_minute = int(pattern["hour_bin"]) * 15
        if db.any_ride_booked_today_in_window(int(pattern["destination_id"]), center_minute, 20, now):
            db.insert_trigger_event("ride", ["departure_window"], pattern["confidence"], now, suppressed=True)
            return None

        cluster = db.get_destination_cluster(int(pattern["destination_id"])) if pattern.get("destination_id") else None
        rides = db.get_rides_for_destination(int(pattern["destination_id"])) if pattern.get("destination_id") else []
        last_ride = rides[0] if rides else {}
        origin = {"lat": last_ride.get("origin_lat"), "lng": last_ride.get("origin_lng")}
        destination = {
            "lat": cluster["centroid_lat"] if cluster else last_ride.get("dest_lat"),
            "lng": cluster["centroid_lng"] if cluster else last_ride.get("dest_lng"),
        }
        trigger_reasons = ["departure_window"]
        confidence = float(pattern["confidence"])
        early_departure_delta = 0
        if cluster:
            live_travel = await self.suggestion_builder.fetch_route_travel_time(origin, destination)
            historical = self._historical_avg_travel_time(rides)
            if live_travel and historical and live_travel > historical * 1.25:
                trigger_reasons.append("traffic_deviation")
                early_departure_delta = max(0, round(live_travel - historical))
                confidence = min(1.0, confidence + 0.1)

        return {
            "type": "ride",
            "trigger_reason": trigger_reasons,
            "confidence": confidence,
            "pattern_ref": f"departure_patterns:{pattern['id']}",
            "fired_at": now,
            "suppressed": False,
            "suppression_reason": None,
            "early_departure_delta": early_departure_delta,
            "soft": soft,
        }

    async def _check_food(self, now: datetime) -> dict[str, Any] | None:
        last_trigger = db.get_last_trigger_time("food")
        if last_trigger and (now - last_trigger) < timedelta(minutes=self.food_cooldown_minutes):
            return None

        minute_of_day = now.hour * 60 + now.minute
        matches = db.find_matching_order_patterns(now.weekday(), minute_of_day, tolerance_minutes=15)
        if not matches:
            return None

        matches.sort(key=lambda row: (-row["confidence"], row["last_seen"]))
        pattern = matches[0]
        dismissal_count = db.count_recent_dismissals_for_pattern(f"order_patterns:{pattern['id']}", days=7)
        threshold = 0.55
        if dismissal_count >= 4 or pattern.get("suppressed"):
            db.insert_trigger_event("food", ["order_window"], pattern["confidence"], now, suppressed=True)
            return None
        if dismissal_count >= 2:
            threshold = max(threshold, 0.8)
        if pattern["confidence"] < threshold:
            return None

        center_minute = int(pattern["hour_bin"]) * 15
        restaurant_id = pattern.get("restaurant_id")
        if restaurant_id and db.any_order_today_in_window(restaurant_id, center_minute, 15, now):
            db.insert_trigger_event("food", ["order_window"], pattern["confidence"], now, suppressed=True)
            return None

        trigger_reasons = ["order_window"]
        confidence = float(pattern["confidence"])
        early_delta = 0
        if restaurant_id:
            recent_order = db.get_recent_order_for_restaurant(restaurant_id)
            status = await self.suggestion_builder.fetch_restaurant_status(restaurant_id, recent_order)
            payload = self.suggestion_builder._status_payload(status)
            current_eta = payload.get("current_eta")
            historical = recent_order.get("delivery_time_min") if recent_order else None
            if (
                isinstance(current_eta, (int, float))
                and isinstance(historical, (int, float))
                and current_eta > historical * 1.3
            ):
                trigger_reasons.append("delivery_delay")
                early_delta = max(0, round(current_eta - historical))
                confidence = min(1.0, confidence + 0.1)

        return {
            "type": "food",
            "trigger_reason": trigger_reasons,
            "confidence": confidence,
            "pattern_ref": f"order_patterns:{pattern['id']}",
            "fired_at": now - timedelta(minutes=early_delta),
            "suppressed": False,
            "suppression_reason": None,
            "delivery_delay": early_delta > 0,
            "soft": False,
        }

    async def _emit_trigger(self, event: dict[str, Any]) -> dict[str, Any]:
        trigger_id = db.insert_trigger_event(
            event["type"],
            event["trigger_reason"],
            event["confidence"],
            event["fired_at"],
            suppressed=event.get("suppressed", False),
        )
        suggestion = await self.suggestion_builder.build(event)
        suggestion_id = db.insert_suggestion(trigger_id, event["type"], suggestion, suggestion["reason_string"], event["fired_at"])
        payload = {
            "event": {
                "id": trigger_id,
                "type": event["type"],
                "trigger_reason": event["trigger_reason"],
                "confidence": event["confidence"],
                "pattern_ref": event["pattern_ref"],
                "fired_at": event["fired_at"].isoformat(),
                "suppressed": False,
                "suppression_reason": None,
            },
            "suggestion": {
                "id": suggestion_id,
                "type": event["type"],
                "reason_string": suggestion["reason_string"],
                "payload": suggestion,
            },
        }
        await self.broadcaster.publish(payload)
        return payload

    def _historical_avg_travel_time(self, rides: list[dict[str, Any]]) -> float | None:
        durations = [float(ride["duration_min"]) for ride in rides if ride.get("duration_min") is not None]
        if not durations:
            return None
        return sum(durations) / len(durations)

