"""Deterministic pattern extraction and feedback reweighting."""

from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from . import database as db

log = logging.getLogger(__name__)
UTC = timezone.utc


@dataclass
class DeparturePattern:
    day_of_week: int
    hour_bin: int
    destination_id: int | None
    platform: str | None
    ride_type: str | None
    frequency: int
    confidence: float
    last_seen: str


@dataclass
class DestinationPattern:
    id: int
    centroid_lat: float
    centroid_lng: float
    label: str | None
    member_count: int
    last_visited: str
    score: float


@dataclass
class OrderPattern:
    day_of_week: int
    hour_bin: int
    cuisine_preference: str | None
    restaurant_id: str | None
    confidence: float
    last_seen: str


@dataclass
class RestaurantPattern:
    restaurant_id: str
    platform: str | None
    score: float
    reorder_rate: float
    avg_order_value: float | None
    avg_items: float
    last_ordered: str


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _hour_bin(dt: datetime) -> int:
    return ((dt.hour * 60) + dt.minute) // 15


def _days_since(value: str, now: datetime) -> int:
    delta = now - _parse_dt(value)
    return max(0, delta.days)


def _distance_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * radius * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class PatternEngine:
    """Deterministic pattern extraction for ride and food histories."""

    def __init__(self) -> None:
        self._reverse_geocode_cache: dict[tuple[float, float], str | None] = {}
        self._forward_geocode_cache: dict[str, tuple[float, float] | None] = {}

    def run_full_extraction(self) -> dict[str, Any]:
        ride_history = db.get_ride_history(limit=5000)
        order_history = db.get_food_order_history(limit=5000)

        destination_patterns = self.extract_destination_clusters(ride_history)
        destination_rows = [
            {
                "id": item.id,
                "centroid_lat": item.centroid_lat,
                "centroid_lng": item.centroid_lng,
                "label": item.label,
                "member_count": item.member_count,
                "last_visited": item.last_visited,
            }
            for item in destination_patterns
        ]
        db.replace_destination_clusters(destination_rows)

        departure_patterns = self.extract_departure_windows(ride_history)
        db.replace_departure_patterns([item.__dict__ for item in departure_patterns])

        order_patterns, cuisine_rows = self.extract_order_windows(order_history)
        db.replace_order_patterns(
            [
                {
                    "day_of_week": item.day_of_week,
                    "hour_bin": item.hour_bin,
                    "cuisine": item.cuisine_preference,
                    "restaurant_id": item.restaurant_id,
                    "confidence": item.confidence,
                    "last_seen": item.last_seen,
                }
                for item in order_patterns
            ]
        )
        db.replace_cuisine_by_day(cuisine_rows)

        restaurant_patterns = self.extract_restaurant_preferences(order_history)
        db.replace_restaurant_patterns(
            [
                {
                    "restaurant_id": item.restaurant_id,
                    "platform": item.platform,
                    "score": item.score,
                    "reorder_rate": item.reorder_rate,
                    "avg_order_value": item.avg_order_value,
                    "last_ordered": item.last_ordered,
                }
                for item in restaurant_patterns
            ]
        )

        return {
            "rides": len(ride_history),
            "orders": len(order_history),
            "departure_patterns": len(departure_patterns),
            "destination_clusters": len(destination_patterns),
            "order_patterns": len(order_patterns),
            "restaurant_patterns": len(restaurant_patterns),
        }

    def extract_departure_windows(self, ride_history: list[dict[str, Any]]) -> list[DeparturePattern]:
        clusters = db.list_destination_clusters()
        cluster_ids_by_ride = self._match_clusters_for_rides(ride_history, clusters)

        by_day: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for ride in ride_history:
            departure = _parse_dt(ride["departure_time"])
            by_day[departure.weekday()].append(ride)

        pattern_rows: list[DeparturePattern] = []
        next_id = 1
        for day, rides in by_day.items():
            total = len(rides)
            counts: dict[tuple[int, int | None, str | None, str | None], dict[str, Any]] = {}
            for ride in rides:
                departure = _parse_dt(ride["departure_time"])
                key = (
                    _hour_bin(departure),
                    cluster_ids_by_ride.get(ride["id"]),
                    ride.get("platform"),
                    ride.get("ride_type"),
                )
                bucket = counts.setdefault(
                    key,
                    {"count": 0, "last_seen": departure},
                )
                bucket["count"] += 1
                if departure > bucket["last_seen"]:
                    bucket["last_seen"] = departure

            for (hour_bin, destination_id, platform, ride_type), meta in counts.items():
                count = meta["count"]
                if count < 3:
                    continue
                pattern_rows.append(
                    DeparturePattern(
                        day_of_week=day,
                        hour_bin=hour_bin,
                        destination_id=destination_id,
                        platform=platform,
                        ride_type=ride_type,
                        frequency=count,
                        confidence=round(count / total, 4),
                        last_seen=meta["last_seen"].isoformat(),
                    )
                )
                next_id += 1
        return pattern_rows

    def extract_destination_clusters(self, ride_history: list[dict[str, Any]]) -> list[DestinationPattern]:
        clusters: list[dict[str, Any]] = []
        now = db.utc_now()

        for ride in ride_history:
            lat = ride.get("dest_lat")
            lng = ride.get("dest_lng")
            label = self._normalize_label(ride.get("dest_label") or ride.get("dropoff_address"))
            if lat is None or lng is None:
                if not label:
                    continue
                geocoded = self._forward_geocode(label)
                if not geocoded:
                    continue
                lat, lng = geocoded
            assigned = None
            for cluster in clusters:
                same_label = bool(label and cluster.get("label_key") == label)
                distance = _distance_meters(lat, lng, cluster["centroid_lat"], cluster["centroid_lng"])
                if same_label or distance <= 300:
                    assigned = cluster
                    break
            if assigned is None:
                assigned = {
                    "rides": [],
                    "centroid_lat": lat,
                    "centroid_lng": lng,
                    "labels": Counter(),
                    "label_key": label,
                    "last_visited": ride["departure_time"],
                }
                clusters.append(assigned)
            assigned["rides"].append(ride)
            if ride.get("dest_label"):
                assigned["labels"][ride["dest_label"]] += 1
            elif ride.get("dropoff_address"):
                assigned["labels"][ride["dropoff_address"]] += 1
            if ride["departure_time"] > assigned["last_visited"]:
                assigned["last_visited"] = ride["departure_time"]
            points = []
            for item in assigned["rides"]:
                if item.get("dest_lat") is not None and item.get("dest_lng") is not None:
                    points.append((item["dest_lat"], item["dest_lng"]))
                    continue
                item_label = self._normalize_label(item.get("dest_label") or item.get("dropoff_address"))
                if item_label:
                    geocoded_item = self._forward_geocode(item_label)
                    if geocoded_item:
                        points.append(geocoded_item)
            if points:
                assigned["centroid_lat"] = sum(point[0] for point in points) / len(points)
                assigned["centroid_lng"] = sum(point[1] for point in points) / len(points)

        patterns: list[DestinationPattern] = []
        for index, cluster in enumerate(clusters, start=1):
            label = cluster["labels"].most_common(1)[0][0] if cluster["labels"] else self._reverse_geocode(
                cluster["centroid_lat"], cluster["centroid_lng"]
            )
            last_seen = cluster["last_visited"]
            recency_weight = math.exp(-(_days_since(last_seen, now) / 30))
            patterns.append(
                DestinationPattern(
                    id=index,
                    centroid_lat=cluster["centroid_lat"],
                    centroid_lng=cluster["centroid_lng"],
                    label=label,
                    member_count=len(cluster["rides"]),
                    last_visited=last_seen,
                    score=round(len(cluster["rides"]) * recency_weight, 4),
                )
            )
        patterns.sort(key=lambda item: (-item.score, -item.member_count, item.id))
        return patterns

    def extract_order_windows(self, order_history: list[dict[str, Any]]) -> tuple[list[OrderPattern], list[dict[str, Any]]]:
        by_day: dict[int, list[dict[str, Any]]] = defaultdict(list)
        cuisine_by_day: dict[tuple[int, str], int] = defaultdict(int)

        for order in order_history:
            ordered = _parse_dt(order["ordered_at"])
            day = ordered.weekday()
            by_day[day].append(order)
            cuisine = (order.get("cuisine") or "").strip()
            if cuisine:
                cuisine_by_day[(day, cuisine)] += 1

        patterns: list[OrderPattern] = []
        cuisine_rows: list[dict[str, Any]] = []
        for day, orders in by_day.items():
            total = len(orders)
            buckets: dict[tuple[int, str | None, str | None], dict[str, Any]] = {}
            for order in orders:
                ordered = _parse_dt(order["ordered_at"])
                key = (
                    _hour_bin(ordered),
                    order.get("cuisine"),
                    order.get("restaurant_id"),
                )
                bucket = buckets.setdefault(key, {"count": 0, "last_seen": ordered})
                bucket["count"] += 1
                if ordered > bucket["last_seen"]:
                    bucket["last_seen"] = ordered
            for (hour_bin, cuisine, restaurant_id), meta in buckets.items():
                count = meta["count"]
                if count < 3:
                    continue
                patterns.append(
                    OrderPattern(
                        day_of_week=day,
                        hour_bin=hour_bin,
                        cuisine_preference=cuisine,
                        restaurant_id=restaurant_id,
                        confidence=round(count / total, 4),
                        last_seen=meta["last_seen"].isoformat(),
                    )
                )
        for (day, cuisine), count in cuisine_by_day.items():
            total = len(by_day[day]) or 1
            cuisine_rows.append(
                {
                    "day_of_week": day,
                    "cuisine": cuisine,
                    "frequency": count,
                    "confidence": round(count / total, 4),
                }
            )
        cuisine_rows.sort(key=lambda row: (row["day_of_week"], -row["confidence"], -row["frequency"]))
        return patterns, cuisine_rows

    def extract_restaurant_preferences(self, order_history: list[dict[str, Any]]) -> list[RestaurantPattern]:
        grouped: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
        now = db.utc_now()
        total_orders = max(1, len(order_history))

        for order in order_history:
            restaurant_id = order.get("restaurant_id")
            if not restaurant_id:
                continue
            grouped[(restaurant_id, order.get("platform"))].append(order)

        patterns: list[RestaurantPattern] = []
        for (restaurant_id, platform), orders in grouped.items():
            orders.sort(key=lambda item: item["ordered_at"], reverse=True)
            last_ordered = orders[0]["ordered_at"]
            avg_order_value = sum((item.get("total_price") or 0.0) for item in orders) / len(orders)
            avg_items = sum(len(self._load_items(item.get("items_json"))) for item in orders) / len(orders)
            reorder_rate = len(orders) / total_orders
            recency_weight = math.exp(-(_days_since(last_ordered, now) / 30))
            score = len(orders) * recency_weight
            patterns.append(
                RestaurantPattern(
                    restaurant_id=restaurant_id,
                    platform=platform,
                    score=round(score, 4),
                    reorder_rate=round(reorder_rate, 4),
                    avg_order_value=round(avg_order_value, 2) if avg_order_value else None,
                    avg_items=round(avg_items, 2),
                    last_ordered=last_ordered,
                )
            )
        patterns.sort(key=lambda item: (-item.score, -item.reorder_rate, item.restaurant_id))
        return patterns

    def reweight(self, feedback: dict[str, Any]) -> None:
        pattern_id = feedback.get("pattern_ref")
        if not pattern_id:
            return
        table, row = db.get_pattern_row(pattern_id)
        if not row:
            return

        now = db.utc_now()
        outcome = feedback.get("event_type")
        edits = feedback.get("edits") or {}

        if outcome == "confirmed":
            db.update_pattern_state(
                table,
                int(row["id"]),
                frequency_delta=1,
                last_seen=now,
            )
            db.insert_pattern_feedback(pattern_id, "confirmed", 1.0, now)
            if edits:
                overrides = {}
                if table == "departure_patterns" and edits.get("destination_id"):
                    overrides["destination_id"] = edits["destination_id"]
                if table == "order_patterns" and edits.get("restaurant_id"):
                    overrides["restaurant_id"] = edits["restaurant_id"]
                if overrides:
                    overrides["last_seen"] = now.isoformat()
                    db.insert_pattern_variant(table, row, overrides)
            return

        if outcome in {"dismissed", "ignored"}:
            dismissal_count = int(row.get("dismissal_count") or 0) + 1
            suppressed = dismissal_count >= 4
            db.update_pattern_state(
                table,
                int(row["id"]),
                dismissal_count_delta=1,
                suppressed=suppressed,
            )
            db.insert_pattern_feedback(pattern_id, outcome, -1.0, now)

    def _match_clusters_for_rides(
        self,
        ride_history: list[dict[str, Any]],
        clusters: list[dict[str, Any]],
    ) -> dict[int, int]:
        mapping: dict[int, int] = {}
        for ride in ride_history:
            lat = ride.get("dest_lat")
            lng = ride.get("dest_lng")
            label_key = self._normalize_label(ride.get("dest_label") or ride.get("dropoff_address"))
            if lat is None or lng is None:
                if not label_key:
                    continue
                for cluster in clusters:
                    cluster_label = self._normalize_label(cluster.get("label"))
                    if cluster_label and cluster_label == label_key:
                        mapping[ride["id"]] = cluster["id"]
                        break
                continue
            best_id = None
            best_distance = float("inf")
            for cluster in clusters:
                distance = _distance_meters(lat, lng, cluster["centroid_lat"], cluster["centroid_lng"])
                if distance < best_distance:
                    best_distance = distance
                    best_id = cluster["id"]
            if best_id is not None and best_distance <= 400:
                mapping[ride["id"]] = best_id
        return mapping

    def _reverse_geocode(self, lat: float, lng: float) -> str | None:
        key = (round(lat, 4), round(lng, 4))
        if key in self._reverse_geocode_cache:
            return self._reverse_geocode_cache[key]
        try:
            response = requests.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={
                    "format": "jsonv2",
                    "lat": lat,
                    "lon": lng,
                    "zoom": 16,
                },
                headers={"User-Agent": "proactive-assistant-pattern-engine"},
                timeout=5,
            )
            response.raise_for_status()
            payload = response.json()
            label = payload.get("name") or payload.get("display_name", "").split(",")[0].strip() or None
        except Exception as exc:
            log.warning("Reverse geocode failed for %.5f, %.5f: %s", lat, lng, exc)
            label = None
        self._reverse_geocode_cache[key] = label
        return label

    def _forward_geocode(self, label: str) -> tuple[float, float] | None:
        key = self._normalize_label(label)
        if not key:
            return None
        if key in self._forward_geocode_cache:
            return self._forward_geocode_cache[key]
        try:
            response = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "format": "jsonv2",
                    "limit": 1,
                    "q": label,
                },
                headers={"User-Agent": "proactive-assistant-pattern-engine"},
                timeout=5,
            )
            response.raise_for_status()
            payload = response.json()
            if payload:
                coords = (float(payload[0]["lat"]), float(payload[0]["lon"]))
            else:
                coords = None
        except Exception as exc:
            log.warning("Forward geocode failed for %r: %s", label, exc)
            coords = None
        self._forward_geocode_cache[key] = coords
        return coords

    def _normalize_label(self, value: str | None) -> str | None:
        if not value:
            return None
        text = " ".join(str(value).strip().lower().split())
        return text or None

    def _load_items(self, items_json: str | None) -> list[dict[str, Any]]:
        if not items_json:
            return []
        try:
            data = items_json if isinstance(items_json, list) else __import__("json").loads(items_json)
        except Exception:
            return []
        return data if isinstance(data, list) else []
