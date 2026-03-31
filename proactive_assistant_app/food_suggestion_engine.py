"""Food suggestion engine based on synced Swiggy/Zomato order history."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from . import database as db

IST = timezone(timedelta(hours=5, minutes=30))


def _food_route_key(source: str, restaurant: str, item: str) -> str:
    return f"{(source or '').strip().lower()}|{(restaurant or '').strip().lower()}|{(item or '').strip().lower()}"


def get_suggestion(current_time: datetime | None = None) -> dict | None:
    now = current_time or datetime.now(IST)

    patterns = db.get_food_patterns()
    if not patterns:
        return _no_history_fallback(now)

    recent_suggestions = db.get_recent_food_interactions("suggest", hours=1)
    if len(recent_suggestions) >= 3:
        return None

    best: dict | None = None
    best_score = 0.0

    current_weekday = now.weekday()
    current_hour = now.hour + (now.minute / 60.0)

    for pattern in patterns:
        frequency = float(pattern.get("frequency") or 1.0)
        avg_hour = float(pattern.get("avg_hour") or 20.0)
        weekday = int(pattern.get("weekday") or current_weekday)

        day_bonus = 1.0 if weekday == current_weekday else 0.25
        diff = abs(avg_hour - current_hour)
        if diff > 12:
            diff = 24 - diff
        time_score = max(0.0, 1.0 - (diff / 4.5))
        frequency_score = min(1.0, frequency / 6.0)

        score = (frequency_score * 0.45) + (day_bonus * 0.35) + (time_score * 0.2)
        if score > best_score:
            best_score = score
            best = pattern

    if not best or best_score < 0.28:
        return None

    source = best.get("source_platform") or "swiggy"
    restaurant = best.get("restaurant_name") or "Recommended restaurant"
    item = best.get("item_name") or "Chef special"
    route_key = _food_route_key(source, restaurant, item)

    recent_confirms = db.get_recent_food_interactions("confirm", hours=4)
    for interaction in recent_confirms:
        if interaction.get("route_key") == route_key:
            return None

    recent_dismissals = db.get_recent_food_interactions("dismiss", hours=2)
    for interaction in recent_dismissals:
        if interaction.get("route_key") == route_key:
            return None

    if db.get_food_dismissal_count_for_route(route_key, hours=72) >= 4:
        return None

    eta_value = best.get("avg_eta")
    eta_minutes = int(round(eta_value)) if isinstance(eta_value, (int, float)) and eta_value > 0 else 28

    alternatives = _build_alternatives(patterns=patterns, base_route_key=route_key, weekday=current_weekday)

    suggestion = {
        "source_platform": source,
        "restaurant_name": restaurant,
        "item_name": item,
        "cuisine": best.get("cuisine") or "Mixed",
        "estimated_price": best.get("avg_price"),
        "eta_minutes": eta_minutes,
        "suggested_time": _format_hour(best.get("avg_hour")),
        "confidence": round(best_score, 3),
        "frequency": int(best.get("frequency") or 1),
        "route_key": route_key,
        "explanation": _build_explanation(best, now, eta_minutes),
        "alternatives": alternatives,
    }

    db.log_food_interaction("suggest", suggestion_payload=suggestion, route_key=route_key)
    return suggestion


def _build_explanation(pattern: dict, now: datetime, eta_minutes: int) -> str:
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    weekday = int(pattern.get("weekday") or now.weekday())
    frequency = int(pattern.get("frequency") or 1)
    item = pattern.get("item_name") or "this meal"
    restaurant = pattern.get("restaurant_name") or "this restaurant"

    return (
        f"You usually order {item} from {restaurant} around this time on "
        f"{day_names[weekday]}s ({frequency} similar orders). Current ETA is about {eta_minutes} minutes."
    )


def _build_alternatives(patterns: list[dict], base_route_key: str, weekday: int) -> list[dict]:
    choices = []
    seen = {base_route_key}

    for pattern in patterns:
        rk = _food_route_key(
            pattern.get("source_platform") or "swiggy",
            pattern.get("restaurant_name") or "",
            pattern.get("item_name") or "",
        )
        if rk in seen:
            continue
        if int(pattern.get("weekday") or weekday) != weekday:
            continue

        choices.append(
            {
                "source_platform": pattern.get("source_platform") or "swiggy",
                "restaurant_name": pattern.get("restaurant_name") or "Alternative",
                "item_name": pattern.get("item_name") or "Special",
                "estimated_price": pattern.get("avg_price"),
                "eta_minutes": int(round(pattern.get("avg_eta") or 32)),
                "route_key": rk,
            }
        )
        seen.add(rk)

        if len(choices) >= 2:
            break

    return choices


def _no_history_fallback(now: datetime) -> dict | None:
    """When there is no order history, suggest the first known order from the DB."""
    orders = db.get_food_order_history(limit=1, offset=0)
    if not orders:
        return None

    order = orders[0]
    source = order.get("source_platform") or "swiggy"
    restaurant = order.get("restaurant_name") or "Recommended restaurant"
    item = order.get("item_name") or "Chef special"
    price = order.get("price")
    route_key = _food_route_key(source, restaurant, item)

    return {
        "source_platform": source,
        "restaurant_name": restaurant,
        "item_name": item,
        "cuisine": order.get("cuisine") or "Unknown",
        "estimated_price": price,
        "eta_minutes": 30,
        "suggested_time": now.strftime("%I:%M %p"),
        "confidence": 0.4,
        "frequency": 1,
        "route_key": route_key,
        "explanation": f"You've ordered {item} from {restaurant} before — want to order again?",
        "alternatives": [],
        "is_fallback": True,
    }


def _format_hour(value) -> str:
    try:
        hour_float = float(value)
    except (TypeError, ValueError):
        hour_float = 20.0

    hour = int(math.floor(hour_float))
    minute = int(round((hour_float % 1) * 60))
    if minute == 60:
        hour += 1
        minute = 0
    hour = hour % 24

    period = "AM" if hour < 12 else "PM"
    display_hour = hour if 1 <= hour <= 12 else (12 if hour in (0, 12) else hour - 12)
    return f"{display_hour}:{minute:02d} {period}"



def get_pattern_summary() -> dict:
    patterns = db.get_food_patterns()
    total_orders = db.get_food_order_count()

    top = []
    for pattern in patterns[:12]:
        top.append(
            {
                "source_platform": pattern.get("source_platform"),
                "restaurant_name": pattern.get("restaurant_name"),
                "item_name": pattern.get("item_name"),
                "cuisine": pattern.get("cuisine"),
                "weekday": pattern.get("weekday"),
                "avg_time": _format_hour(pattern.get("avg_hour")),
                "frequency": int(pattern.get("frequency") or 1),
                "avg_price": pattern.get("avg_price"),
                "avg_eta": pattern.get("avg_eta"),
            }
        )

    return {
        "total_orders": total_orders,
        "top_patterns": top,
    }
