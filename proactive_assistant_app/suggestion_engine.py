"""Proactive suggestion engine powered by Groq LLM.

Uses ride history data from the DB and sends it to Groq for intelligent
pattern analysis, suggestion generation, and natural language explanations.
Falls back to rule-based logic if Groq is unavailable.
"""

import os
import json
import logging
import math
from datetime import datetime, timedelta, timezone

from groq import Groq
from dotenv import load_dotenv

from . import database as db
from . import uber_client

load_dotenv()
log = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
IST = timezone(timedelta(hours=5, minutes=30))


def _route_key(pickup: str, dropoff: str) -> str:
    return f"{(pickup or '').strip().lower()}|{(dropoff or '').strip().lower()}"


def get_suggestion(
    current_time: datetime = None,
    user_lat: float = None,
    user_lng: float = None,
    user_identifier: str | None = None,
) -> dict | None:
    """Main entry point: use OpenAI to analyze ride patterns and decide if a suggestion is needed."""
    now = current_time or datetime.now(IST)

    # Gather data for the LLM
    rides = db.get_ride_history(limit=50)
    routes = db.get_route_frequencies()
    if not routes:
        return _most_frequent_route_fallback(now, rides, user_lat, user_lng, user_identifier)

    recent_interactions = db.get_recent_interactions(hours=24)
    user_feedback = db.get_recent_ride_feedback_memories(user_identifier, limit=8) if user_identifier else []
    dismissed_route_keys = set(
        db.get_recent_dismissed_route_keys_for_user(user_identifier, hours=168)
    ) if user_identifier else set()

    # Annoyance avoidance (hard rules — don't send to LLM)
    recent_confirms = db.get_recent_interactions("confirm", hours=4)
    recent_suggestions = db.get_recent_interactions("suggest", hours=1)
    if len(recent_suggestions) >= 3:
        return None  # Cooldown

    # Build context for the LLM
    analysis = _ask_groq_for_suggestion(
        now,
        routes,
        rides,
        recent_interactions,
        recent_confirms,
        user_feedback,
        user_lat,
        user_lng,
    )

    if not analysis or not analysis.get("should_suggest"):
        return None

    # Build the suggestion from LLM output
    pickup = analysis.get("pickup", "Unknown")
    dropoff = analysis.get("dropoff", "Unknown")
    rk = _route_key(pickup, dropoff)

    if rk in dismissed_route_keys:
        return None

    # Check if this route was recently confirmed
    for c in recent_confirms:
        if c.get("route_key") == rk:
            return None

    # Check recent dismissals
    dismissals = db.get_dismissal_count_for_route(rk, hours=72)
    recent_dismissals = db.get_recent_interactions("dismiss", hours=2)
    for d in recent_dismissals:
        if d.get("route_key") == rk:
            return None  # Recently dismissed

    # Get coordinates from historical rides
    pickup_lat, pickup_lng, dropoff_lat, dropoff_lng = None, None, None, None
    for r in rides:
        if r.get("dropoff_address", "").lower() == dropoff.lower() and r.get("pickup_lat"):
            pickup_lat = r["pickup_lat"]
            pickup_lng = r["pickup_lng"]
            dropoff_lat = r.get("dropoff_lat")
            dropoff_lng = r.get("dropoff_lng")
            break

    suggestion = {
        "pickup": pickup,
        "dropoff": dropoff,
        "route_key": rk,
        "pickup_lat": pickup_lat,
        "pickup_lng": pickup_lng,
        "dropoff_lat": dropoff_lat,
        "dropoff_lng": dropoff_lng,
        "suggested_departure": analysis.get("suggested_departure", now.strftime("%I:%M %p")),
        "ride_type": analysis.get("ride_type", "UberX"),
        "estimated_price": analysis.get("estimated_price"),
        "price_range": None,
        "eta_minutes": None,
        "duration_minutes": analysis.get("estimated_duration"),
        "distance_miles": None,
        "surge_multiplier": 1.0,
        "traffic_delta_minutes": 0,
        "confidence": analysis.get("confidence", 0.5),
        "explanation": analysis.get("explanation", "Based on your ride history."),
        "live_data": False,
        "frequency": analysis.get("frequency", 1),
        "day_match": analysis.get("day_match", False),
        "deeplink": uber_client.build_deeplink(
            pickup_address=pickup,
            dropoff_address=dropoff,
            pickup_lat=pickup_lat,
            pickup_lng=pickup_lng,
            dropoff_lat=dropoff_lat,
            dropoff_lng=dropoff_lng,
        ),
    }

    db.log_interaction("suggest", suggestion_payload=suggestion, route_key=rk)
    return suggestion


def choose_best_current_prediction(
    candidates: list[dict],
    *,
    current_time: datetime | None = None,
    user_lat: float | None = None,
    user_lng: float | None = None,
) -> dict | None:
    """Pick the best current ride candidate using Groq, with deterministic fallback."""
    if not candidates:
        return None

    now = current_time or datetime.now(IST)
    rides = db.get_ride_history(limit=20, source_platform="uber")
    if not GROQ_API_KEY:
        return _fallback_pick_candidate(candidates)

    candidate_summaries = []
    for index, candidate in enumerate(candidates):
        option = candidate.get("recommended_option") or {}
        candidate_summaries.append(
            {
                "index": index,
                "destination": candidate.get("destination_label"),
                "departure_at": candidate.get("recommended_departure_time"),
                "traffic_delta_minutes": candidate.get("traffic_delta_minutes"),
                "price": option.get("price"),
                "price_text": option.get("raw_price_text"),
                "eta": option.get("eta"),
                "ride_type": option.get("ride_type"),
                "confidence": candidate.get("_candidate_score"),
                "trigger_reasons": candidate.get("trigger_reasons", []),
            }
        )

    recent_rides = [
        {
            "destination": ride.get("dest_label") or ride.get("dropoff_address"),
            "departure_time": ride.get("departure_time") or ride.get("request_timestamp"),
            "fare": ride.get("fare") if ride.get("fare") is not None else ride.get("price"),
        }
        for ride in rides[:12]
    ]

    location_context = None
    if user_lat is not None and user_lng is not None:
        location_context = {"lat": user_lat, "lng": user_lng}

    prompt = f"""Pick the single best ride suggestion to show right now.

Current time: {now.isoformat()}
Current location: {json.dumps(location_context)}

Candidate suggestions:
{json.dumps(candidate_summaries, indent=2, default=str)}

Recent verified Uber rides:
{json.dumps(recent_rides, indent=2, default=str)}

Rules:
- Choose a candidate only if it is a believable next ride for right now.
- Strongly prefer candidates whose departure time is close to now and whose destination appears repeatedly in recent history.
- Reject weak or placeholder candidates.
- Return JSON only.

{{
  "show_suggestion": true,
  "selected_index": 0,
  "explanation": "short factual explanation"
}}

Or:
{{
  "show_suggestion": false,
  "explanation": "why none of the candidates fit right now"
}}"""

    try:
        client = Groq(api_key=GROQ_API_KEY)
        response = client.chat.completions.create(
            model=MODEL,
            temperature=0.1,
            max_tokens=220,
            messages=[
                {"role": "system", "content": "You rank ride suggestions. Reply with strict JSON only."},
                {"role": "user", "content": prompt},
            ],
        )
        text = (response.choices[0].message.content or "").strip()
        parsed = _parse_json_response(text)
        if not parsed.get("show_suggestion"):
            return None
        index = int(parsed.get("selected_index", -1))
        if index < 0 or index >= len(candidates):
            return _fallback_pick_candidate(candidates)
        chosen = dict(candidates[index])
        explanation = str(parsed.get("explanation") or "").strip()
        if explanation:
            chosen["reason_string"] = explanation
        return chosen
    except Exception as exc:
        log.warning("Groq candidate ranking failed: %s", exc)
        return _fallback_pick_candidate(candidates)


def validate_prediction_with_llm(
    prediction: dict,
    current_time: datetime = None,
) -> dict:
    """Validate a prepared ride prediction with LLM before surfacing it in UI."""
    if not prediction:
        return {"valid": False, "score": 0.0, "reason": "empty_prediction"}

    if not GROQ_API_KEY:
        return {"valid": False, "score": 0.0, "reason": "missing_groq_api_key"}

    now = current_time or datetime.now(IST)
    routes = db.get_route_frequencies()
    rides = db.get_ride_history(limit=12)

    route_summaries = []
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    for r in routes[:12]:
        route_summaries.append(
            {
                "pickup": r.get("pickup_address"),
                "dropoff": r.get("dropoff_address"),
                "day": day_names[r.get("weekday", 0)] if isinstance(r.get("weekday"), int) else None,
                "avg_hour": r.get("avg_hour"),
                "frequency": r.get("frequency"),
            }
        )

    recent_rides = []
    for r in rides[:8]:
        recent_rides.append(
            {
                "pickup": r.get("pickup_address"),
                "dropoff": r.get("dropoff_address"),
                "request_timestamp": r.get("request_timestamp"),
                "ride_type": r.get("ride_type"),
            }
        )

    prompt = f"""Validate whether this ride recommendation should be shown to the user right now.

CURRENT TIME (IST): {now.strftime('%A %I:%M %p')}

PREDICTION TO VALIDATE:
{json.dumps(prediction, default=str)}

LEARNED ROUTE PATTERNS:
{json.dumps(route_summaries, default=str)}

RECENT RIDES:
{json.dumps(recent_rides, default=str)}

Rules:
- Return valid=true only if prediction clearly aligns with user day/time behavior and route history.
- If timing or destination seems weak/uncertain, return valid=false.
- Keep explanation short, specific, and factual.

Return JSON only:
{{
  "valid": true/false,
  "score": 0.0,
  "explanation": "short explanation"
}}"""

    try:
        client = Groq(api_key=GROQ_API_KEY)
        response = client.chat.completions.create(
            model=MODEL,
            temperature=0.1,
            max_tokens=180,
            messages=[
                {
                    "role": "system",
                    "content": "You validate commute prediction quality. Reply with strict JSON only.",
                },
                {"role": "user", "content": prompt},
            ],
        )
        text = (response.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        parsed = json.loads(text.strip())
        score = float(parsed.get("score", 0.0) or 0.0)
        valid = bool(parsed.get("valid")) and score >= 0.55
        return {
            "valid": valid,
            "score": max(0.0, min(1.0, score)),
            "explanation": str(parsed.get("explanation") or "").strip(),
            "reason": "accepted" if valid else "rejected_by_llm",
        }
    except Exception as exc:
        log.warning("Prediction validation failed: %s", exc)
        return {"valid": False, "score": 0.0, "reason": "validation_error"}


def _fallback_pick_candidate(candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    ranked = sorted(candidates, key=lambda item: float(item.get("_candidate_score") or 0.0), reverse=True)
    best = ranked[0]
    if float(best.get("_candidate_score") or 0.0) < 0.35:
        return None
    return dict(best)


def _parse_json_response(text: str) -> dict:
    content = (text or "").strip()
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(content[start : end + 1])
        raise


def _ask_groq_for_suggestion(
    now: datetime,
    routes: list[dict],
    rides: list[dict],
    interactions: list[dict],
    recent_confirms: list[dict],
    user_feedback: list[dict],
    user_lat: float = None,
    user_lng: float = None,
) -> dict | None:
    """Send ride data to OpenAI and get back a suggestion decision."""

    if not GROQ_API_KEY:
        log.warning("No GROQ_API_KEY set, falling back to rule-based engine")
        return _fallback_suggestion(now, routes, rides)

    # Convert to IST for display
    now_ist = now if now.tzinfo else now.replace(tzinfo=IST)
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    current_day = day_names[now_ist.weekday()]
    current_time_str = now_ist.strftime("%I:%M %p")

    # Prepare ride history summary
    route_summaries = []
    for r in routes[:15]:
        avg_hour = r.get("avg_hour", 0)
        h = int(avg_hour)
        m = int((avg_hour % 1) * 60)
        period = "AM" if h < 12 else "PM"
        dh = h if h <= 12 else h - 12
        if dh == 0:
            dh = 12

        route_summaries.append({
            "from": r["pickup_address"],
            "to": r["dropoff_address"],
            "day": day_names[r["weekday"]],
            "avg_time": f"{dh}:{m:02d} {period}",
            "frequency": r["frequency"],
            "avg_price": round(r["avg_price"], 0) if r.get("avg_price") else None,
            "avg_duration_min": round(r["avg_duration"], 0) if r.get("avg_duration") else None,
            "last_used": r.get("last_used"),
        })

    # Convert route avg_hour from UTC to IST
    for rs in route_summaries:
        if rs.get("avg_time"):
            # Re-compute avg_time in IST (add 5:30)
            pass  # Already computed from avg_hour which is stored from timestamps

    # Recent rides (last 10) — convert timestamps to IST
    recent_rides = []
    for r in rides[:10]:
        ts = r.get("request_timestamp", "")
        try:
            dt = datetime.fromisoformat(ts)
            dt_ist = dt + timedelta(hours=5, minutes=30)
            ts = dt_ist.strftime("%b %d, %I:%M %p IST")
        except (ValueError, TypeError):
            pass
        recent_rides.append({
            "to": r.get("dropoff_address"),
            "date": ts,
            "price": r.get("price"),
            "ride_type": r.get("ride_type"),
        })

    # Recent interactions
    interaction_summary = []
    for i in interactions[:10]:
        interaction_summary.append({
            "action": i.get("action_type"),
            "route": i.get("route_key"),
            "time": i.get("timestamp"),
        })

    user_feedback_summary = []
    for item in user_feedback[:8]:
        user_feedback_summary.append({
            "route": item.get("route_key"),
            "pickup": item.get("pickup_address"),
            "dropoff": item.get("dropoff_address"),
            "reason_code": item.get("reason_code"),
            "feedback_text": item.get("feedback_text"),
            "dismiss_count": item.get("dismiss_count"),
            "updated_at": item.get("updated_at"),
        })

    location_ctx = ""
    if user_lat and user_lng:
        location_ctx = f"\nUSER'S CURRENT GPS LOCATION: {user_lat}, {user_lng}"

    prompt = f"""You are a proactive ride assistant. Analyze the user's ride history and decide if you should suggest a ride right now.

CURRENT TIME: {current_day}, {current_time_str} IST (Indian Standard Time)
{location_ctx}

ROUTE PATTERNS (grouped by pickup, dropoff, and day):
{json.dumps(route_summaries, indent=2, default=str)}

RECENT RIDES (last 10):
{json.dumps(recent_rides, indent=2, default=str)}

RECENT USER INTERACTIONS (confirms, dismissals, edits):
{json.dumps(interaction_summary, indent=2, default=str)}

PER-USER DISMISSAL MEMORY:
{json.dumps(user_feedback_summary, indent=2, default=str)}

RULES:
- ONLY suggest a ride if the current time is CLOSE to the user's typical ride time (within ~1-2 hours). Do NOT suggest rides at odd hours (e.g., 3 AM when the user rides at 6 AM).
- If no pattern matches the current day+time, return should_suggest: false.
- Confidence should reflect how well the current day/time matches the pattern. A perfect day+time match = high confidence. Off by hours = low/zero confidence.
- If the user has GPS coordinates, use "Current Location" as pickup.
- Avoid routes the user recently dismissed, especially when the feedback says the destination or timing is wrong.
- All times are in IST.

Based on this data, decide:
1. Should you suggest a ride right now? The current day AND time must match the user's patterns.
2. If yes, which route is most likely?
3. What's your confidence level (0.0 to 1.0)?
4. Write a short, natural explanation for WHY you're suggesting this ride (reference specific patterns).

Respond with ONLY valid JSON (no markdown, no backticks):
{{
  "should_suggest": true/false,
  "pickup": "Current Location or address",
  "dropoff": "destination address",
  "ride_type": "UberX",
  "suggested_departure": "HH:MM AM/PM",
  "estimated_price": "₹XXX",
  "estimated_duration": minutes_as_number,
  "confidence": 0.0-1.0,
  "frequency": number_of_times_this_route,
  "day_match": true/false,
  "explanation": "Natural language explanation referencing the user's actual patterns and current time"
}}

If no ride should be suggested (wrong time of day, no matching pattern), return: {{"should_suggest": false, "reason": "why not"}}"""

    try:
        client = Groq(api_key=GROQ_API_KEY)
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "You are a proactive ride assistant AI. You analyze ride history patterns and decide when to suggest rides. Always respond with valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=500,
        )

        text = response.choices[0].message.content.strip()
        log.info("Groq response: %s", text[:300])

        # Parse JSON — handle potential markdown wrapping
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()

        result = json.loads(text)
        return result

    except Exception as e:
        log.error("Groq API call failed: %s", e)
        return _fallback_suggestion(now, routes, rides)


def _most_frequent_route_fallback(
    now: datetime,
    rides: list[dict],
    user_lat: float = None,
    user_lng: float = None,
    user_identifier: str | None = None,
) -> dict | None:
    """Suggest the most-frequently-taken route when no aggregated patterns exist yet.

    This runs unconditionally (no time matching) so at least one ride is
    always needed to produce a suggestion.
    """
    if not rides:
        return None

    # Count dropoff frequency from raw rides
    freq: dict[str, int] = {}
    for r in rides:
        dropoff = (r.get("dropoff_address") or "").strip()
        if dropoff:
            freq[dropoff] = freq.get(dropoff, 0) + 1

    if not freq:
        return None

    top_dropoff = max(freq, key=lambda k: freq[k])
    count = freq[top_dropoff]

    # Find a matching ride for coordinates
    pickup = "Current Location" if (user_lat and user_lng) else "Unknown"
    pickup_lat, pickup_lng, dropoff_lat, dropoff_lng = user_lat, user_lng, None, None
    for r in rides:
        if (r.get("dropoff_address") or "").strip() == top_dropoff:
            if not pickup_lat:
                pickup = r.get("pickup_address") or pickup
                pickup_lat = r.get("pickup_lat")
                pickup_lng = r.get("pickup_lng")
            dropoff_lat = r.get("dropoff_lat")
            dropoff_lng = r.get("dropoff_lng")
            break

    rk = _route_key(pickup, top_dropoff)
    if user_identifier and rk in set(db.get_recent_dismissed_route_keys_for_user(user_identifier, hours=168)):
        return None

    suggestion = {
        "pickup": pickup,
        "dropoff": top_dropoff,
        "route_key": rk,
        "pickup_lat": pickup_lat,
        "pickup_lng": pickup_lng,
        "dropoff_lat": dropoff_lat,
        "dropoff_lng": dropoff_lng,
        "suggested_departure": now.strftime("%I:%M %p"),
        "ride_type": "UberX",
        "estimated_price": None,
        "price_range": None,
        "eta_minutes": None,
        "duration_minutes": None,
        "distance_miles": None,
        "surge_multiplier": 1.0,
        "traffic_delta_minutes": 0,
        "confidence": min(0.3 + count * 0.1, 0.8),
        "explanation": f"You've ridden to {top_dropoff} {count} time{'s' if count != 1 else ''} before — want to book a ride?",
        "live_data": False,
        "frequency": count,
        "day_match": False,
        "deeplink": uber_client.build_deeplink(
            pickup_address=pickup,
            dropoff_address=top_dropoff,
            pickup_lat=pickup_lat,
            pickup_lng=pickup_lng,
            dropoff_lat=dropoff_lat,
            dropoff_lng=dropoff_lng,
        ),
        "is_fallback": True,
    }

    db.log_interaction("suggest", suggestion_payload=suggestion, route_key=rk)
    return suggestion


def _fallback_suggestion(now: datetime, routes: list[dict], rides: list[dict]) -> dict | None:

    """Rule-based fallback when Groq is unavailable."""
    weekday = now.weekday()
    hour = now.hour + now.minute / 60.0

    best = None
    best_score = -1

    for route in routes:
        pickup = route.get("pickup_address", "")
        dropoff = route.get("dropoff_address", "")
        route_weekday = route.get("weekday")
        avg_hour = route.get("avg_hour", 9)
        freq = route.get("frequency", 1)

        day_match = route_weekday == weekday
        diff = abs(avg_hour - hour)
        if diff > 12:
            diff = 24 - diff
        time_score = max(0, 1.0 - diff / 4)

        score = (min(freq / 5, 1.0) * 0.4) + (1.0 if day_match else 0.2) * 0.3 + time_score * 0.3

        if score > best_score:
            best_score = score
            avg_h = int(avg_hour)
            avg_m = int((avg_hour % 1) * 60)
            p = "AM" if avg_h < 12 else "PM"
            dh = avg_h if avg_h <= 12 else avg_h - 12
            if dh == 0:
                dh = 12

            day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            best = {
                "should_suggest": score > 0.3,
                "pickup": pickup,
                "dropoff": dropoff,
                "ride_type": route.get("ride_type", "UberX"),
                "suggested_departure": f"{dh}:{avg_m:02d} {p}",
                "estimated_price": f"₹{int(route['avg_price'])}" if route.get("avg_price") else None,
                "estimated_duration": round(route["avg_duration"]) if route.get("avg_duration") else None,
                "confidence": round(score, 3),
                "frequency": freq,
                "day_match": day_match,
                "explanation": f"You've taken this route {freq} times on {day_names[route_weekday]}s around {dh}:{avg_m:02d} {p}.",
            }

    return best


def get_pattern_summary() -> dict:
    """Return a summary of learned commute patterns for the UI."""
    routes = db.get_route_frequencies()
    total_rides = db.get_ride_count()
    patterns = []

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    for r in routes[:10]:
        avg_hour = r.get("avg_hour", 0)
        hour_int = int(avg_hour)
        minute_int = int((avg_hour % 1) * 60)
        period = "AM" if hour_int < 12 else "PM"
        display_hour = hour_int if hour_int <= 12 else hour_int - 12
        if display_hour == 0:
            display_hour = 12

        patterns.append({
            "pickup": r["pickup_address"],
            "dropoff": r["dropoff_address"],
            "day": day_names[r["weekday"]],
            "weekday": r["weekday"],
            "avg_time": f"{display_hour}:{minute_int:02d} {period}",
            "frequency": r["frequency"],
            "avg_price": round(r["avg_price"], 0) if r.get("avg_price") else None,
            "avg_duration": round(r["avg_duration"], 0) if r.get("avg_duration") else None,
            "ride_type": r.get("ride_type", "UberX"),
        })

    return {
        "total_rides": total_rides,
        "unique_routes": len(routes),
        "top_patterns": patterns,
    }


def get_upcoming_rides(current_time: datetime = None) -> list[dict]:
    """Return probable rides for the rest of today based on learned patterns."""
    now = current_time or datetime.now(IST)
    weekday = now.weekday()
    current_hour = now.hour + now.minute / 60.0

    routes = db.get_route_frequencies()
    if not routes:
        return []

    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_short = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    upcoming = []

    for route in routes:
        if route["weekday"] != weekday:
            continue
        avg_hour = route.get("avg_hour", 0)
        if avg_hour <= current_hour or avg_hour > current_hour + 10:
            continue

        freq = route.get("frequency", 1)
        h = int(avg_hour)
        m = int((avg_hour % 1) * 60)
        period = "AM" if h < 12 else "PM"
        dh = h if h <= 12 else h - 12
        if dh == 0:
            dh = 12

        confidence = round(min(freq / 5, 1.0) * 0.7 + 0.3, 2)

        upcoming.append({
            "pickup": route["pickup_address"],
            "dropoff": route["dropoff_address"],
            "route_key": _route_key(route["pickup_address"], route["dropoff_address"]),
            "expected_time": f"{dh}:{m:02d} {period}",
            "expected_hour": avg_hour,
            "frequency": freq,
            "avg_price": round(route["avg_price"], 0) if route.get("avg_price") else None,
            "avg_duration": round(route["avg_duration"], 0) if route.get("avg_duration") else None,
            "ride_type": route.get("ride_type", "UberX"),
            "confidence": confidence,
            "day": day_short[weekday],
        })

    upcoming.sort(key=lambda x: x["expected_hour"])

    # Enrich with coordinates
    rides = db.get_ride_history(limit=100)
    for item in upcoming:
        for r in rides:
            if (r.get("dropoff_address", "").lower() == item["dropoff"].lower()
                    and r.get("pickup_lat")):
                item["pickup_lat"] = r["pickup_lat"]
                item["pickup_lng"] = r["pickup_lng"]
                item["dropoff_lat"] = r.get("dropoff_lat")
                item["dropoff_lng"] = r.get("dropoff_lng")
                break

    return upcoming
