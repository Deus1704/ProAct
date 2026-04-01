"""Deterministic suggestion builder for ride and food suggestions."""

from __future__ import annotations

import asyncio
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from . import database as db
from . import food_client
from . import uber_client
from .data_fetcher import CachedResult, DataFetcher, DataUnavailable
from .reason_generator import generate_reason

log = logging.getLogger(__name__)
UTC = timezone.utc


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class SuggestionBuilder:
    def __init__(self, fetcher: DataFetcher | None = None) -> None:
        self.fetcher = fetcher or DataFetcher()
        self._geocode_cache: dict[str, tuple[float, float] | None] = {}

    async def build(self, trigger_event: dict[str, Any]) -> dict[str, Any]:
        if trigger_event["type"] == "ride":
            suggestion = await self.build_ride_suggestion(trigger_event)
        else:
            suggestion = await self.build_food_suggestion(trigger_event)
        suggestion["type"] = trigger_event["type"]
        reason_string = generate_reason(trigger_event, suggestion)
        suggestion["reason_string"] = reason_string
        return suggestion

    async def build_ride_suggestion(self, trigger_event: dict[str, Any]) -> dict[str, Any]:
        pattern_ref = trigger_event["pattern_ref"]
        _, pattern = db.get_pattern_row(pattern_ref)
        if not pattern:
            raise ValueError(f"Unknown ride pattern {pattern_ref}")

        cluster = db.get_destination_cluster(int(pattern["destination_id"])) if pattern.get("destination_id") else None
        history = db.get_rides_for_destination(int(pattern["destination_id"])) if pattern.get("destination_id") else db.get_ride_history(limit=20)
        last_ride = history[0] if history else {}
        usual_departure = self._scheduled_time(pattern["hour_bin"], trigger_event["fired_at"])
        departure_time = usual_departure - timedelta(minutes=trigger_event.get("early_departure_delta", 0))

        origin = trigger_event.get("origin_override") or self._choose_origin(last_ride)
        destination = {
            "lat": cluster["centroid_lat"] if cluster else last_ride.get("dest_lat"),
            "lng": cluster["centroid_lng"] if cluster else last_ride.get("dest_lng"),
            "label": cluster.get("label") if cluster else last_ride.get("dest_label"),
        }
        origin, destination = await self._ensure_location_coordinates(origin, destination)
        ride_options = await self.fetch_ride_options(origin, destination)

        recommended = ride_options[0] if ride_options else {
            "platform": "uber",
            "ride_type": pattern.get("ride_type"),
            "price": None,
            "eta": None,
            "surge_multiplier": 1.0,
            "live_data": False,
            "fallback_message": "Price unavailable — tap to open Uber",
        }
        if recommended.get("price") is None:
            recommended["deeplink"] = uber_client.build_deeplink(
                pickup_lat=origin.get("lat"),
                pickup_lng=origin.get("lng"),
                dropoff_lat=destination.get("lat"),
                dropoff_lng=destination.get("lng"),
                dropoff_address=destination.get("label"),
            )

        return {
            "pattern_ref": pattern_ref,
            "trigger_reasons": trigger_event.get("trigger_reason", []),
            "origin": origin,
            "destination_id": pattern.get("destination_id"),
            "destination_label": destination.get("label"),
            "destination": destination,
            "usual_departure_time": usual_departure.isoformat(),
            "recommended_departure_time": departure_time.isoformat(),
            "traffic_delta_minutes": trigger_event.get("early_departure_delta", 0),
            "ride_options": ride_options,
            "recommended_option": recommended,
            "surge_flag": bool((recommended.get("surge_multiplier") or 1.0) > 1.5),
        }

    async def build_food_suggestion(self, trigger_event: dict[str, Any]) -> dict[str, Any]:
        pattern_ref = trigger_event["pattern_ref"]
        _, pattern = db.get_pattern_row(pattern_ref)
        if not pattern:
            raise ValueError(f"Unknown order pattern {pattern_ref}")

        cuisine = pattern.get("cuisine")
        candidates = db.list_restaurant_patterns()
        if cuisine:
            cuisine_pref = [row for row in db.list_cuisine_by_day() if row["day_of_week"] == trigger_event["fired_at"].weekday()]
            if cuisine_pref:
                top_cuisine = cuisine_pref[0]["cuisine"]
                if top_cuisine:
                    cuisine = top_cuisine

        restaurant_id = pattern.get("restaurant_id") or (candidates[0]["restaurant_id"] if candidates else None)
        primary_order = db.get_recent_order_for_restaurant(restaurant_id) if restaurant_id else None
        status = await self.fetch_restaurant_status(restaurant_id, primary_order)

        if not primary_order:
            raise ValueError("No historical order found for selected restaurant.")

        alternatives = []
        if not self._restaurant_usable(status):
            alternatives = self._restaurant_alternatives(candidates, cuisine, restaurant_id)
            if alternatives:
                restaurant_id = alternatives[0]["restaurant_id"]
                primary_order = db.get_recent_order_for_restaurant(restaurant_id)
                status = await self.fetch_restaurant_status(restaurant_id, primary_order)

        items = self._last_items(primary_order, 3)
        estimated_price = primary_order.get("total_price")
        eta_label = None
        if isinstance(status, CachedResult):
            eta_label = f"as of {status.fetched_at}"

        return {
            "pattern_ref": pattern_ref,
            "trigger_reasons": trigger_event.get("trigger_reason", []),
            "restaurant_id": restaurant_id,
            "restaurant_name": primary_order.get("restaurant_name"),
            "cuisine": cuisine or primary_order.get("cuisine"),
            "items": items,
            "live_status": self._status_payload(status),
            "alternatives": alternatives,
            "estimated_price": estimated_price,
            "price_note": "Menu prices may have changed",
            "eta_label": eta_label,
        }

    async def fetch_ride_options(self, origin: dict[str, Any], destination: dict[str, Any]) -> list[dict[str, Any]]:
        if None in (origin.get("lat"), origin.get("lng"), destination.get("lat"), destination.get("lng")):
            return []

        async def primary() -> dict[str, Any]:
            result = await uber_client.scrape_live_estimates(
                float(origin["lat"]),
                float(origin["lng"]),
                float(destination["lat"]),
                float(destination["lng"]),
            )
            options = []
            for estimate in result.get("estimates") or []:
                low = estimate.get("low_estimate")
                high = estimate.get("high_estimate")
                price = high if high is not None else low
                options.append(
                    {
                        "platform": "uber",
                        "ride_type": estimate.get("ride_type"),
                        "price": price,
                        "eta": estimate.get("eta_minutes"),
                        "surge_multiplier": estimate.get("surge_multiplier") or 1.0,
                        "raw_price_text": estimate.get("price_text"),
                    }
                )
            return {"options": options, "page_url": "https://m.uber.com/", "snapshot_html": str(result)}

        async def fallback() -> dict[str, Any]:
            return {"options": []}

        fetched = await self.fetcher.fetch_with_fallback(
            primary,
            fallback,
            cache_key=f"ride_options:{origin['lat']}:{origin['lng']}:{destination['lat']}:{destination['lng']}",
            cache_ttl_minutes=20,
            platform="uber",
            page_url="https://m.uber.com/",
        )
        options_payload = fetched.data if isinstance(fetched, CachedResult) else fetched
        if options_payload is DataUnavailable:
            return []
        options = options_payload.get("options", []) if isinstance(options_payload, dict) else []
        for option in options:
            option["live_data"] = not isinstance(fetched, CachedResult)
            option["is_stale"] = isinstance(fetched, CachedResult) and fetched.is_stale
            if isinstance(fetched, CachedResult):
                option["stale_since"] = fetched.stale_since
        return sorted(
            options,
            key=lambda item: (
                (item.get("price") or math.inf) * (item.get("surge_multiplier") or 1.0),
                item.get("eta") or math.inf,
            ),
        )

    async def fetch_restaurant_status(
        self,
        restaurant_id: str | None,
        order: dict[str, Any] | None,
    ) -> dict[str, Any] | CachedResult | Any:
        if not restaurant_id or not order:
            return DataUnavailable

        async def primary() -> dict[str, Any]:
            result = await food_client.get_live_match_for_suggestion(
                restaurant_name=order.get("restaurant_name") or "",
                item_name=self._first_item_name(order),
                lat=None,
                lng=None,
            )
            if not result:
                return {"is_open": None, "current_eta": None, "item_availability": []}
            availability = []
            if result.get("item_name"):
                availability.append({"name": result["item_name"], "available": True})
            return {
                "is_open": result.get("is_open"),
                "current_eta": result.get("delivery_time_mins"),
                "item_availability": availability,
                "deeplink": result.get("deeplink"),
                "snapshot_html": str(result),
            }

        async def fallback() -> dict[str, Any]:
            return {
                "is_open": True,
                "current_eta": order.get("delivery_time_min"),
                "item_availability": [],
            }

        return await self.fetcher.fetch_with_fallback(
            primary,
            fallback,
            cache_key=f"restaurant_status:{restaurant_id}",
            cache_ttl_minutes=30,
            platform=str(order.get("platform") or "swiggy"),
            page_url="https://www.swiggy.com/",
        )

    async def fetch_route_travel_time(self, origin: dict[str, Any], destination: dict[str, Any]) -> float | None:
        if None in (origin.get("lat"), origin.get("lng"), destination.get("lat"), destination.get("lng")):
            return None

        google_key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
        tomtom_key = os.getenv("TOMTOM_API_KEY", "").strip()

        async def primary() -> dict[str, Any]:
            if google_key:
                response = requests.get(
                    "https://maps.googleapis.com/maps/api/distancematrix/json",
                    params={
                        "origins": f"{origin['lat']},{origin['lng']}",
                        "destinations": f"{destination['lat']},{destination['lng']}",
                        "departure_time": "now",
                        "key": google_key,
                    },
                    timeout=8,
                )
                response.raise_for_status()
                element = response.json()["rows"][0]["elements"][0]
                return {"duration_min": round(element["duration_in_traffic"]["value"] / 60, 2)}
            if tomtom_key:
                response = requests.get(
                    f"https://api.tomtom.com/routing/1/calculateRoute/{origin['lat']},{origin['lng']}:{destination['lat']},{destination['lng']}/json",
                    params={"key": tomtom_key, "traffic": "true"},
                    timeout=8,
                )
                response.raise_for_status()
                payload = response.json()
                return {"duration_min": round(payload["routes"][0]["summary"]["travelTimeInSeconds"] / 60, 2)}
            raise RuntimeError("No traffic API key configured.")

        async def fallback() -> dict[str, Any]:
            raise RuntimeError("No fallback traffic provider configured.")

        fetched = await self.fetcher.fetch_with_fallback(
            primary,
            fallback,
            cache_key=f"traffic:{origin['lat']}:{origin['lng']}:{destination['lat']}:{destination['lng']}",
            cache_ttl_minutes=15,
            platform="traffic",
        )
        if fetched is DataUnavailable:
            return None
        data = fetched.data if isinstance(fetched, CachedResult) else fetched
        return data.get("duration_min") if isinstance(data, dict) else None

    def _choose_origin(self, last_ride: dict[str, Any]) -> dict[str, Any]:
        return {
            "source": "last_known_origin",
            "label": last_ride.get("origin_label") or last_ride.get("pickup_address") or last_ride.get("title"),
            "lat": last_ride.get("origin_lat") if last_ride.get("origin_lat") is not None else last_ride.get("pickup_lat"),
            "lng": last_ride.get("origin_lng") if last_ride.get("origin_lng") is not None else last_ride.get("pickup_lng"),
        }

    async def _ensure_location_coordinates(
        self,
        origin: dict[str, Any],
        destination: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        origin = dict(origin)
        destination = dict(destination)

        origin_lat = self._coordinate_value(origin.get("lat"))
        origin_lng = self._coordinate_value(origin.get("lng"))
        destination_lat = self._coordinate_value(destination.get("lat"))
        destination_lng = self._coordinate_value(destination.get("lng"))

        origin_label = (origin.get("label") or "").strip()
        destination_label = (destination.get("label") or "").strip()

        tasks: list[tuple[str, asyncio.Task]] = []
        if (origin_lat is None or origin_lng is None) and origin_label and not self._is_generic_location_label(origin_label):
            tasks.append(("origin", asyncio.create_task(asyncio.to_thread(self._forward_geocode, origin_label))))
        if (destination_lat is None or destination_lng is None) and destination_label and not self._is_generic_location_label(destination_label):
            tasks.append(("destination", asyncio.create_task(asyncio.to_thread(self._forward_geocode, destination_label))))

        if tasks:
            results = await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
            for (target, _), result in zip(tasks, results):
                if isinstance(result, Exception) or not result:
                    continue
                lat, lng = result
                if target == "origin" and (origin_lat is None or origin_lng is None):
                    origin_lat, origin_lng = lat, lng
                if target == "destination" and (destination_lat is None or destination_lng is None):
                    destination_lat, destination_lng = lat, lng

        if origin_lat is not None and origin_lng is not None:
            origin["lat"] = origin_lat
            origin["lng"] = origin_lng
        if destination_lat is not None and destination_lng is not None:
            destination["lat"] = destination_lat
            destination["lng"] = destination_lng
        if origin_label:
            origin["label"] = origin_label
        if destination_label:
            destination["label"] = destination_label
        return origin, destination

    @staticmethod
    def _coordinate_value(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not (-90 <= number <= 90 or -180 <= number <= 180):
            return None
        return number

    @staticmethod
    def _is_generic_location_label(label: str | None) -> bool:
        text = str(label or "").strip().lower()
        if not text:
            return True
        return text in {
            "current location",
            "last known origin",
            "suggested destination",
            "unknown",
            "unknown pickup",
            "unknown destination",
            "not captured",
        }

    def _forward_geocode(self, label: str) -> tuple[float, float] | None:
        key = " ".join(label.strip().lower().split())
        if not key:
            return None
        if key in self._geocode_cache:
            return self._geocode_cache[key]
        try:
            response = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"format": "jsonv2", "limit": 1, "q": label},
                headers={"User-Agent": "proactive-assistant-suggestion-builder"},
                timeout=5,
            )
            response.raise_for_status()
            payload = response.json()
            coords = None
            if isinstance(payload, list) and payload:
                coords = (float(payload[0]["lat"]), float(payload[0]["lon"]))
        except Exception as exc:
            log.warning("Destination geocode failed for %r: %s", label, exc)
            coords = None
        self._geocode_cache[key] = coords
        return coords

    def _scheduled_time(self, hour_bin: int, reference: datetime) -> datetime:
        minute_of_day = int(hour_bin) * 15
        hour = minute_of_day // 60
        minute = minute_of_day % 60
        return reference.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def _last_items(self, order: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        try:
            items = __import__("json").loads(order.get("items_json") or "[]")
        except Exception:
            items = []
        return items[:limit] if isinstance(items, list) else []

    def _first_item_name(self, order: dict[str, Any]) -> str | None:
        items = self._last_items(order, 1)
        if not items:
            return None
        return items[0].get("name")

    def _restaurant_usable(self, status: Any) -> bool:
        payload = self._status_payload(status)
        if payload.get("is_open") is False:
            return False
        eta = payload.get("current_eta")
        return not isinstance(eta, (int, float)) or eta <= 60

    def _restaurant_alternatives(
        self,
        candidates: list[dict[str, Any]],
        cuisine: str | None,
        current_restaurant_id: str | None,
    ) -> list[dict[str, Any]]:
        results = []
        for candidate in candidates:
            if candidate["restaurant_id"] == current_restaurant_id:
                continue
            order = db.get_recent_order_for_restaurant(candidate["restaurant_id"])
            if not order:
                continue
            if cuisine and order.get("cuisine") and order.get("cuisine") != cuisine:
                continue
            results.append(
                {
                    "restaurant_id": candidate["restaurant_id"],
                    "restaurant_name": order.get("restaurant_name"),
                    "score": candidate["score"],
                }
            )
            if len(results) >= 3:
                break
        return results

    def _status_payload(self, status: Any) -> dict[str, Any]:
        if status is DataUnavailable:
            return {"is_open": None, "current_eta": None, "item_availability": [], "unavailable": True}
        if isinstance(status, CachedResult):
            payload = dict(status.data)
            payload["is_stale"] = status.is_stale
            payload["stale_since"] = status.stale_since
            return payload
        return status if isinstance(status, dict) else {"unavailable": True}
