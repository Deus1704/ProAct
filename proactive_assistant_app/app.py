"""FastAPI app for the deterministic proactive assistant backend."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from pydantic import BaseModel
import requests
from collections import defaultdict

from . import database as db
from . import food_client
from . import suggestion_engine
from . import uber_client
from .data_fetcher import get_scraper_health
from .pattern_engine import PatternEngine
from .suggestion_builder import SuggestionBuilder
from .trigger_watcher import SuggestionBroadcaster, TriggerWatcher

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
load_dotenv()

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
AUTH_COOKIE_NAME = "proactive_assistant_auth"

pattern_engine = PatternEngine()
suggestion_builder = SuggestionBuilder()
broadcaster = SuggestionBroadcaster()
trigger_watcher = TriggerWatcher(
    pattern_engine=pattern_engine,
    suggestion_builder=suggestion_builder,
    broadcaster=broadcaster,
)

FREE_GEOCODE_CACHE: dict[str, tuple[float, float] | None] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    pattern_engine.run_full_extraction()
    await trigger_watcher.start()
    try:
        yield
    finally:
        await trigger_watcher.stop()
        await food_client.close_browser()
        await uber_client.close_browser()


app = FastAPI(title="Proactive Assistant", version="2.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _login_path() -> str:
    return "/login"


def _error_response(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


def _rounded_live_context_key(lat: float, lng: float) -> str:
    return f"live_context_{lat:.2f}_{lng:.2f}"


def _parse_live_context_cache(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw:
        return None

    data = raw.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None

    fetched_at = str(raw.get("fetched_at") or data.get("fetched_at") or "").strip()
    if not fetched_at:
        return None

    try:
        fetched_dt = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return None

    age_minutes = (db.utc_now() - fetched_dt).total_seconds() / 60.0
    if age_minutes > 10:
        return None

    return data


def _weather_label(weather_code: Any) -> str | None:
    try:
        code = int(weather_code)
    except (TypeError, ValueError):
        return None

    if code == 0:
        return "Clear sky"
    if 1 <= code <= 3:
        return "Partly cloudy"
    if code in (45, 48):
        return "Fog"
    if 51 <= code <= 67:
        return "Rain"
    if 71 <= code <= 77:
        return "Snow"
    if 80 <= code <= 82:
        return "Showers"
    if code == 95:
        return "Thunderstorm"
    return None


def _air_quality_label(aqi: Any) -> str | None:
    try:
        value = float(aqi)
    except (TypeError, ValueError):
        return None

    if value <= 20:
        return "Good"
    if value <= 40:
        return "Fair"
    if value <= 60:
        return "Moderate"
    if value <= 80:
        return "Poor"
    if value <= 100:
        return "Very Poor"
    return "Hazardous"


def _cache_live_context_payload(lat: float, lng: float) -> dict[str, Any] | None:
    cached = db.get_scraper_cache(_rounded_live_context_key(lat, lng))
    return _parse_live_context_cache(cached)


async def _request_json(url: str, *, params: dict[str, Any], headers: dict[str, str] | None = None, timeout: int = 6) -> dict[str, Any]:
    def _sync_request() -> dict[str, Any]:
        response = requests.get(url, params=params, headers=headers, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Expected JSON object")
        return data

    return await asyncio.to_thread(_sync_request)


async def _fetch_weather_context(lat: float, lng: float, cache: dict[str, Any] | None) -> dict[str, Any]:
    try:
        payload = await _request_json(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lng,
                "current": "temperature_2m,weather_code,windspeed_10m",
            },
        )
        current = payload.get("current") or {}
        temperature = current.get("temperature_2m")
        weather_code = current.get("weathercode", current.get("weather_code"))
        wind_speed = current.get("windspeed_10m", current.get("wind_speed_10m"))
        return {
            "temperature": None if temperature is None else f"{round(float(temperature))}°C",
            "conditions": _weather_label(weather_code),
            "wind_speed": None if wind_speed is None else f"{round(float(wind_speed))} km/h",
            "success": True,
        }
    except Exception as exc:
        log.warning("Weather lookup failed for %.2f, %.2f: %s", lat, lng, exc)
        if cache:
            return {
                "temperature": cache.get("temperature"),
                "conditions": cache.get("conditions"),
                "wind_speed": cache.get("wind_speed"),
                "success": False,
            }
        return {"temperature": None, "conditions": None, "wind_speed": None, "success": False}


async def _fetch_air_quality_context(lat: float, lng: float, cache: dict[str, Any] | None) -> dict[str, Any]:
    try:
        payload = await _request_json(
            "https://air-quality-api.open-meteo.com/v1/air-quality",
            params={
                "latitude": lat,
                "longitude": lng,
                "current": "european_aqi,pm2_5",
            },
        )
        current = payload.get("current") or {}
        aqi_value = current.get("european_aqi")
        return {
            "air_quality": _air_quality_label(aqi_value),
            "aqi_value": None if aqi_value is None else int(round(float(aqi_value))),
            "success": True,
        }
    except Exception as exc:
        log.warning("Air quality lookup failed for %.2f, %.2f: %s", lat, lng, exc)
        if cache:
            return {
                "air_quality": cache.get("air_quality"),
                "aqi_value": cache.get("aqi_value"),
                "success": False,
            }
        return {"air_quality": None, "aqi_value": None, "success": False}


async def _fetch_geocode_context(lat: float, lng: float, cache: dict[str, Any] | None) -> dict[str, Any]:
    try:
        payload = await _request_json(
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "lat": lat,
                "lon": lng,
                "format": "json",
            },
            headers={"User-Agent": "ProactiveAssistant/1.0"},
        )
        address = payload.get("address") or {}
        locality = address.get("city") or address.get("town") or address.get("suburb")
        state_name = address.get("state")
        position = ", ".join(part for part in [locality, state_name] if part)
        return {
            "position": position or None,
            "success": True,
        }
    except Exception as exc:
        log.warning("Reverse geocode failed for %.2f, %.2f: %s", lat, lng, exc)
        if cache:
            return {
                "position": cache.get("position"),
                "success": False,
            }
        return {"position": None, "success": False}


def _encode_auth_cookie(profile: dict[str, Any]) -> str:
    payload = json.dumps(profile, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_auth_cookie(raw_cookie: Optional[str]) -> Optional[dict[str, Any]]:
    if not raw_cookie:
        return None
    try:
        padded = raw_cookie + "=" * (-len(raw_cookie) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        profile = json.loads(decoded)
    except Exception:
        return None
    if not isinstance(profile, dict):
        return None
    identifier = str(profile.get("identifier") or "").strip()
    if not identifier:
        return None
    return {
        "identifier": identifier,
        "firstName": str(profile.get("firstName") or "").strip(),
        "fullName": str(profile.get("fullName") or "").strip(),
    }


def _auth_profile_from_request(request: Request) -> Optional[dict[str, Any]]:
    return _decode_auth_cookie(request.cookies.get(AUTH_COOKIE_NAME))


def _set_auth_cookie(response: JSONResponse, profile: dict[str, Any]) -> None:
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=_encode_auth_cookie(profile),
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=60 * 60 * 24 * 30,
        path="/",
    )


def _pending_suggestion_response(suggestion: dict[str, Any] | None) -> dict[str, Any]:
    if not suggestion:
        return {"suggestion": None}
    return {
        "suggestion": {
            "id": suggestion["id"],
            "type": suggestion["type"],
            "reason_string": suggestion["reason_string"],
            "shown_at": suggestion["shown_at"],
            "outcome": suggestion["outcome"],
            "payload": suggestion["payload"],
        }
    }


def _serialize_ride_row(row: dict[str, Any]) -> dict[str, Any]:
    platform = row.get("platform") or row.get("source_platform") or "uber"
    return {
        **row,
        "source_platform": platform,
        "request_timestamp": row.get("request_timestamp") or row.get("departure_time"),
        "pickup_address": row.get("pickup_address") or row.get("origin_label"),
        "dropoff_address": row.get("dropoff_address") or row.get("dest_label"),
        "price": row.get("price") if row.get("price") is not None else row.get("fare"),
        "pickup_lat": row.get("pickup_lat") if row.get("pickup_lat") is not None else row.get("origin_lat"),
        "pickup_lng": row.get("pickup_lng") if row.get("pickup_lng") is not None else row.get("origin_lng"),
        "dropoff_lat": row.get("dropoff_lat") if row.get("dropoff_lat") is not None else row.get("dest_lat"),
        "dropoff_lng": row.get("dropoff_lng") if row.get("dropoff_lng") is not None else row.get("dest_lng"),
    }


class LoginPayload(BaseModel):
    identifier: str
    full_name: str = ""


class GoogleAuthPayload(BaseModel):
    credential: str


class ClickPayload(BaseModel):
    x: int
    y: int


class TypePayload(BaseModel):
    text: str


class KeyPayload(BaseModel):
    key: str


class ConfirmSuggestionPayload(BaseModel):
    edited: bool = False
    edits: dict[str, Any] | None = None


class LegacyRideActionPayload(BaseModel):
    route_key: str | None = None
    pickup: str | None = None
    dropoff: str | None = None
    ride_type: str | None = None
    reason: str | None = None


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _auth_profile_from_request(request):
        return RedirectResponse(url="/", status_code=303)
    with open(os.path.join(STATIC_DIR, "login.html"), "r", encoding="utf-8") as handle:
        return HTMLResponse(content=handle.read())


@app.get("/", response_class=HTMLResponse)
@app.get("/rides", response_class=HTMLResponse)
@app.get("/food", response_class=HTMLResponse)
def serve_index(request: Request):
    if not _auth_profile_from_request(request):
        return RedirectResponse(url=_login_path(), status_code=303)
    with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as handle:
        return HTMLResponse(content=handle.read())


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    return HTMLResponse("<html><body><h1>Privacy Policy</h1><p>Local-only assistant data stays in SQLite.</p></body></html>")


@app.get("/api/auth/status")
def auth_status(request: Request):
    profile = _auth_profile_from_request(request)
    return {"authenticated": bool(profile), "profile": profile}


@app.post("/api/auth/login")
def auth_login(payload: LoginPayload):
    identifier = payload.identifier.strip()
    if not identifier:
        return _error_response("Email or phone number is required.")
    full_name = payload.full_name.strip()
    first_name = full_name.split()[0] if full_name else identifier.split("@")[0].split(".")[0]
    profile = {"identifier": identifier, "firstName": first_name.title() or "There", "fullName": full_name}
    response = JSONResponse(content={"ok": True, "redirect_to": "/", "profile": profile})
    _set_auth_cookie(response, profile)
    return response


@app.get("/api/auth/google/config")
def auth_google_config():
    client_id = (os.getenv("GOOGLE_CLIENT_ID") or "").strip()
    return {"enabled": bool(client_id), "client_id": client_id}


@app.post("/api/auth/google")
def auth_google(payload: GoogleAuthPayload):
    credential = payload.credential.strip()
    if not credential:
        return _error_response("Google credential is required.")

    client_id = (os.getenv("GOOGLE_CLIENT_ID") or "").strip()
    if not client_id:
        return _error_response("Google sign-in is not configured.", status_code=503)

    try:
        token_info = id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            client_id,
        )
    except ValueError:
        return _error_response("Invalid Google credential.", status_code=401)

    identifier = str(token_info.get("email") or token_info.get("sub") or "").strip()
    if not identifier:
        return _error_response("Unable to read Google profile.", status_code=401)

    full_name = str(token_info.get("name") or "").strip()
    first_name = str(token_info.get("given_name") or "").strip()
    if not first_name:
        first_name = (full_name.split()[0] if full_name else identifier.split("@")[0].split(".")[0]).title() or "There"

    profile = {
        "identifier": identifier,
        "firstName": first_name,
        "fullName": full_name,
    }
    response = JSONResponse(content={"ok": True, "redirect_to": "/", "profile": profile})
    _set_auth_cookie(response, profile)
    return response


@app.post("/api/auth/logout")
def auth_logout():
    response = JSONResponse(content={"ok": True, "redirect_to": _login_path()})
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return response


@app.get("/api/context/live")
async def get_live_context(
    lat: float | None = Query(None),
    lng: float | None = Query(None),
):
    if lat is None or lng is None:
        return _error_response("lat and lng required", status_code=400)

    if float(lat) == 0.0 and float(lng) == 0.0:
        return _error_response("invalid coordinates", status_code=400)

    cache_key = _rounded_live_context_key(float(lat), float(lng))
    cache_row = db.get_scraper_cache(cache_key)
    cache = _parse_live_context_cache(cache_row)

    weather_task = _fetch_weather_context(float(lat), float(lng), cache)
    air_task = _fetch_air_quality_context(float(lat), float(lng), cache)
    geo_task = _fetch_geocode_context(float(lat), float(lng), cache)

    weather, air_quality, geocode = await asyncio.gather(weather_task, air_task, geo_task)

    sources = {
        "weather": bool(weather.get("success")),
        "air_quality": bool(air_quality.get("success")),
        "geocode": bool(geocode.get("success")),
    }
    has_live_success = any(sources.values())
    fetched_at = db.utc_now().isoformat()
    if not has_live_success and cache_row and cache_row.get("fetched_at"):
        fetched_at = str(cache_row["fetched_at"])

    response = {
        "position": geocode.get("position"),
        "temperature": weather.get("temperature"),
        "conditions": weather.get("conditions"),
        "air_quality": air_quality.get("air_quality"),
        "aqi_value": air_quality.get("aqi_value"),
        "wind_speed": weather.get("wind_speed"),
        "fetched_at": fetched_at,
        "sources": sources,
    }

    if has_live_success:
        db.upsert_scraper_cache(
            cache_key,
            response,
            fetched_at=db.utc_now(),
            is_stale=False,
        )

    return response


@app.get("/api/uber/status")
def uber_status():
    return uber_client.get_connection_status()


@app.post("/api/uber/login")
async def uber_login():
    return await uber_client.start_login()


@app.get("/api/uber/screenshot")
async def uber_screenshot():
    return await uber_client.get_screenshot()


@app.post("/api/uber/click")
async def uber_click(payload: ClickPayload):
    return await uber_client.browser_click(payload.x, payload.y)


@app.post("/api/uber/type")
async def uber_type(payload: TypePayload):
    return await uber_client.browser_type(payload.text)


@app.post("/api/uber/key")
async def uber_key(payload: KeyPayload):
    return await uber_client.browser_key(payload.key)


@app.post("/api/uber/finish-login")
async def uber_finish_login():
    return await uber_client.finish_login()


@app.get("/api/food/status")
def food_status(provider: Optional[str] = Query(None)):
    try:
        return food_client.get_connection_status(provider=provider)
    except Exception as exc:
        return _error_response(str(exc))


@app.post("/api/food/login/{provider}")
async def food_login(provider: str):
    try:
        return await food_client.start_login(provider)
    except Exception as exc:
        return _error_response(str(exc))


@app.get("/api/food/screenshot")
async def food_screenshot():
    return await food_client.get_screenshot()


@app.post("/api/food/click")
async def food_click(payload: ClickPayload):
    return await food_client.browser_click(payload.x, payload.y)


@app.post("/api/food/type")
async def food_type(payload: TypePayload):
    return await food_client.browser_type(payload.text)


@app.post("/api/food/key")
async def food_key(payload: KeyPayload):
    return await food_client.browser_key(payload.key)


@app.post("/api/food/finish-login/{provider}")
async def food_finish_login(provider: str):
    try:
        return await food_client.finish_login(provider)
    except Exception as exc:
        return _error_response(str(exc))


@app.get("/api/history/sync")
async def history_sync(provider: Optional[str] = Query(None)):
    ride_result = {"synced": 0, "total_found": 0}
    food_result = {"synced": 0, "total_found": 0}
    ride_errors: list[str] = []
    food_errors: list[str] = []

    try:
        ride_result = await uber_client.scrape_ride_history()
    except Exception as exc:
        ride_errors.append(str(exc))

    try:
        food_result = await food_client.sync_order_history(provider="swiggy" if provider in (None, "swiggy") else provider)
    except Exception as exc:
        food_errors.append(str(exc))

    extraction = pattern_engine.run_full_extraction()
    return {
        "rides": ride_result,
        "food": food_result,
        "ride_errors": ride_errors,
        "food_errors": food_errors,
        "patterns": extraction,
    }


@app.post("/api/uber/sync-history")
async def sync_uber_history():
    result = await uber_client.scrape_ride_history()
    pattern_engine.run_full_extraction()
    return result


@app.get("/api/uber/history")
def uber_history(limit: int = Query(50, ge=1, le=250), offset: int = Query(0, ge=0)):
    rows = db.get_ride_history(limit=limit, offset=offset, source_platform="uber")
    return {
        "rides": [_serialize_ride_row(row) for row in rows],
        "total": db.get_ride_count(source_platform="uber"),
    }


@app.post("/api/food/sync-history")
async def sync_food_history(provider: Optional[str] = Query(None)):
    result = await food_client.sync_order_history(provider=provider or "swiggy")
    pattern_engine.run_full_extraction()
    return result


@app.get("/api/rides/history")
def rides_history(limit: int = Query(50, ge=1, le=250), offset: int = Query(0, ge=0), source: Optional[str] = Query(None)):
    rows = db.get_ride_history(limit=limit, offset=offset, source_platform=source)
    return {"rides": [_serialize_ride_row(row) for row in rows], "total": db.get_ride_count(source_platform=source)}


@app.get("/api/food/history")
def food_history(limit: int = Query(50, ge=1, le=250), offset: int = Query(0, ge=0), source: Optional[str] = Query(None)):
    return {"orders": db.get_food_order_history(limit=limit, offset=offset, source_platform=source), "total": db.get_food_order_count(source_platform=source)}


@app.get("/api/food/top-restaurants")
async def food_top_restaurants(lat: Optional[float] = Query(None), lng: Optional[float] = Query(None)):
    try:
        return await food_client.scrape_top_restaurants(lat=lat, lng=lng)
    except Exception as exc:
        log.warning("Top restaurant scrape failed: %s", exc)
        return {"restaurants": [], "error": str(exc)}


def _next_occurrence(day_of_week: int, hour_bin: int) -> datetime:
    now = db.utc_now()
    minute_of_day = int(hour_bin) * 15
    hour = minute_of_day // 60
    minute = minute_of_day % 60
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta_days = (day_of_week - now.weekday()) % 7
    candidate = candidate + timedelta(days=delta_days)
    if candidate < now:
        candidate = candidate + timedelta(days=7)
    return candidate


def _format_pattern_time(hour_bin: int) -> str:
    minute_of_day = int(hour_bin) * 15
    hour = minute_of_day // 60
    minute = minute_of_day % 60
    return f"{hour:02d}:{minute:02d}"


def _minute_distance(left: int, right: int) -> int:
    diff = abs(left - right)
    return min(diff, (24 * 60) - diff)


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=db.UTC)
    return parsed.astimezone(db.UTC)


def _to_coordinate(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not (-90 <= number <= 90 or -180 <= number <= 180):
        return None
    return number


def _is_generic_location_label(label: str | None) -> bool:
    text = str(label or "").strip().lower()
    if not text:
        return True
    generic_values = {
        "current location",
        "last known origin",
        "suggested destination",
        "unknown",
        "unknown pickup",
        "unknown destination",
        "not captured",
    }
    return text in generic_values


def _geocode_free_nominatim(label: str) -> tuple[float, float] | None:
    key = " ".join(label.strip().lower().split())
    if not key:
        return None
    if key in FREE_GEOCODE_CACHE:
        return FREE_GEOCODE_CACHE[key]
    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"format": "jsonv2", "limit": 1, "q": label},
            headers={"User-Agent": "proactive-assistant-map-geocoder"},
            timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
        coords = None
        if isinstance(payload, list) and payload:
            lat = float(payload[0]["lat"])
            lng = float(payload[0]["lon"])
            coords = (lat, lng)
    except Exception as exc:
        log.warning("Suggestion geocode failed for %r: %s", label, exc)
        coords = None
    FREE_GEOCODE_CACHE[key] = coords
    return coords


async def _enrich_suggestion_coordinates(
    suggestion: dict[str, Any],
    *,
    user_lat: float | None,
    user_lng: float | None,
) -> dict[str, Any]:
    origin = suggestion.get("origin") if isinstance(suggestion.get("origin"), dict) else {}
    destination = suggestion.get("destination") if isinstance(suggestion.get("destination"), dict) else {}

    destination_label = destination.get("label") or suggestion.get("destination_label")
    origin_label = origin.get("label")

    origin_lat = _to_coordinate(origin.get("lat"))
    origin_lng = _to_coordinate(origin.get("lng"))
    dest_lat = _to_coordinate(destination.get("lat"))
    dest_lng = _to_coordinate(destination.get("lng"))

    if user_lat is not None and user_lng is not None:
        origin_lat = _to_coordinate(user_lat)
        origin_lng = _to_coordinate(user_lng)

    geocode_tasks: list[tuple[str, asyncio.Task]] = []
    if (origin_lat is None or origin_lng is None) and origin_label and not _is_generic_location_label(origin_label):
        geocode_tasks.append(("origin", asyncio.create_task(asyncio.to_thread(_geocode_free_nominatim, origin_label))))
    if (dest_lat is None or dest_lng is None) and destination_label and not _is_generic_location_label(destination_label):
        geocode_tasks.append(("destination", asyncio.create_task(asyncio.to_thread(_geocode_free_nominatim, destination_label))))

    if geocode_tasks:
        results = await asyncio.gather(*(task for _, task in geocode_tasks), return_exceptions=True)
        for (target, _), result in zip(geocode_tasks, results):
            if isinstance(result, Exception) or not result:
                continue
            lat, lng = result
            if target == "origin" and (origin_lat is None or origin_lng is None):
                origin_lat, origin_lng = lat, lng
            if target == "destination" and (dest_lat is None or dest_lng is None):
                dest_lat, dest_lng = lat, lng

    if destination_label:
        destination["label"] = destination_label
    if origin_label:
        origin["label"] = origin_label
    if origin_lat is not None and origin_lng is not None:
        origin["lat"] = origin_lat
        origin["lng"] = origin_lng
    if dest_lat is not None and dest_lng is not None:
        destination["lat"] = dest_lat
        destination["lng"] = dest_lng

    suggestion["origin"] = origin
    suggestion["destination"] = destination
    suggestion["destination_label"] = destination_label
    return suggestion


async def _hydrate_suggestion_live_quote(
    suggestion: dict[str, Any],
    *,
    user_lat: float | None,
    user_lng: float | None,
) -> dict[str, Any]:
    suggestion = await _enrich_suggestion_coordinates(suggestion, user_lat=user_lat, user_lng=user_lng)

    origin = suggestion.get("origin") if isinstance(suggestion.get("origin"), dict) else {}
    destination = suggestion.get("destination") if isinstance(suggestion.get("destination"), dict) else {}
    origin_lat = _to_coordinate(origin.get("lat"))
    origin_lng = _to_coordinate(origin.get("lng"))
    dest_lat = _to_coordinate(destination.get("lat"))
    dest_lng = _to_coordinate(destination.get("lng"))
    destination_label = destination.get("label") or suggestion.get("destination_label")

    if None in (origin_lat, origin_lng, dest_lat, dest_lng):
        return suggestion

    live_options = await suggestion_builder.fetch_ride_options(
        {"lat": origin_lat, "lng": origin_lng, "label": origin.get("label")},
        {"lat": dest_lat, "lng": dest_lng, "label": destination_label},
    )
    if not live_options:
        return suggestion

    suggestion["ride_options"] = live_options
    prior_option = suggestion.get("recommended_option") if isinstance(suggestion.get("recommended_option"), dict) else {}
    preferred_type = (prior_option.get("ride_type") or "").strip().lower()
    recommended = next(
        (
            option
            for option in live_options
            if preferred_type and str(option.get("ride_type") or "").strip().lower() == preferred_type
        ),
        live_options[0],
    )
    recommended = dict(recommended)
    recommended["deeplink"] = uber_client.build_deeplink(
        pickup_lat=origin_lat,
        pickup_lng=origin_lng,
        dropoff_lat=dest_lat,
        dropoff_lng=dest_lng,
        dropoff_address=destination_label,
    )
    suggestion["recommended_option"] = recommended
    suggestion["surge_flag"] = bool((recommended.get("surge_multiplier") or 1.0) > 1.5)
    return suggestion


def _has_strict_live_recommendation(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    option = payload.get("recommended_option") or {}
    return bool(
        option.get("live_data")
        and option.get("ride_type")
        and option.get("price") is not None
        and option.get("eta") is not None
    )


async def _build_next_ride_prediction() -> dict[str, Any] | None:
    pending = db.get_latest_pending_suggestion()
    if pending and pending["type"] == "ride":
        payload = dict(pending["payload"])
        payload["reason_string"] = pending["reason_string"]
        payload["suggestion_id"] = pending["id"]
        payload["prediction_kind"] = "triggered"
        return payload

    patterns = db.list_departure_patterns()
    if not patterns:
        return None

    ranked = sorted(
        patterns,
        key=lambda row: (_next_occurrence(row["day_of_week"], row["hour_bin"]), -float(row["confidence"])),
    )
    pattern = ranked[0]
    event = {
        "type": "ride",
        "trigger_reason": ["forecast_pattern"],
        "confidence": float(pattern["confidence"]),
        "pattern_ref": f"departure_patterns:{pattern['id']}",
        "fired_at": _next_occurrence(pattern["day_of_week"], pattern["hour_bin"]),
        "suppressed": False,
        "suppression_reason": None,
        "early_departure_delta": 0,
    }
    payload = await suggestion_builder.build_ride_suggestion(event)
    payload["reason_string"] = payload.get("reason_string") or "Next likely ride based on your past departures."
    payload["prediction_kind"] = "forecast"
    payload["suggestion_id"] = None
    return payload


async def _build_ranked_ride_candidates(user_lat: float | None = None, user_lng: float | None = None) -> list[dict[str, Any]]:
    now = db.utc_now()
    minute_of_day = now.hour * 60 + now.minute
    patterns = [row for row in db.list_departure_patterns() if row["day_of_week"] == now.weekday()]
    if not patterns:
        return await _build_history_based_ride_candidates(now, user_lat=user_lat, user_lng=user_lng)

    scored_patterns: list[tuple[float, dict[str, Any], int]] = []
    for pattern in patterns:
        pattern_minute = int(pattern["hour_bin"]) * 15
        minute_diff = _minute_distance(pattern_minute, minute_of_day)
        if minute_diff > 180:
            continue
        time_score = max(0.0, 1.0 - (minute_diff / 180.0))
        score = (
            float(pattern.get("confidence") or 0.0) * 0.55
            + min(float(pattern.get("frequency") or 0.0) / 5.0, 1.0) * 0.25
            + time_score * 0.20
        )
        scored_patterns.append((score, pattern, minute_diff))

    scored_patterns.sort(key=lambda item: (-item[0], item[2], -float(item[1].get("frequency") or 0.0)))
    candidates: list[dict[str, Any]] = []
    for score, pattern, minute_diff in scored_patterns[:4]:
        event = {
            "type": "ride",
            "trigger_reason": ["current_time_match"],
            "confidence": float(pattern["confidence"]),
            "pattern_ref": f"departure_patterns:{pattern['id']}",
            "fired_at": now,
            "suppressed": False,
            "suppression_reason": None,
            "early_departure_delta": 0,
            "origin_override": {
                "source": "current_location" if user_lat is not None and user_lng is not None else "last_known_origin",
                "lat": user_lat,
                "lng": user_lng,
            } if user_lat is not None and user_lng is not None else None,
        }
        payload = await suggestion_builder.build_ride_suggestion(event)
        payload["reason_string"] = payload.get("reason_string") or "Matched against your verified Uber ride timing."
        payload["prediction_kind"] = "forecast"
        payload["suggestion_id"] = None
        payload["_candidate_score"] = round(score, 4)
        payload["_minute_diff"] = minute_diff
        candidates.append(payload)
    return candidates


async def _build_history_based_ride_candidates(
    now: datetime,
    *,
    user_lat: float | None = None,
    user_lng: float | None = None,
) -> list[dict[str, Any]]:
    rides = db.get_ride_history(limit=100, source_platform="uber")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ride in rides:
        label = (ride.get("dest_label") or "").strip()
        if not label:
            continue
        grouped[label].append(ride)

    candidates: list[dict[str, Any]] = []
    minute_of_day = now.hour * 60 + now.minute
    for label, entries in grouped.items():
        if len(entries) < 2:
            continue
        day_matches = 0
        same_day_entries: list[tuple[dict[str, Any], datetime, int]] = []
        all_diffs: list[int] = []
        fares: list[float] = []
        ride_types: dict[str, int] = defaultdict(int)
        for ride in entries:
            departure = _parse_iso_dt(ride.get("departure_time"))
            if not departure:
                continue
            departure_minute = departure.hour * 60 + departure.minute
            minute_diff = _minute_distance(departure_minute, minute_of_day)
            all_diffs.append(minute_diff)
            if departure.weekday() == now.weekday():
                day_matches += 1
                same_day_entries.append((ride, departure, minute_diff))
            fare = ride.get("fare")
            if isinstance(fare, (int, float)):
                fares.append(float(fare))
            ride_type = ride.get("ride_type")
            if ride_type:
                ride_types[str(ride_type)] += 1

        if not all_diffs:
            continue

        best_entry = min(same_day_entries, key=lambda item: item[2])[0] if same_day_entries else entries[0]
        best_diff = min((item[2] for item in same_day_entries), default=min(all_diffs))
        if best_diff > 240:
            continue

        time_score = max(0.0, 1.0 - (best_diff / 240.0))
        frequency_score = min(len(entries) / 5.0, 1.0)
        day_score = min(day_matches / max(len(entries), 1), 1.0)
        score = round((frequency_score * 0.45) + (time_score * 0.35) + (day_score * 0.20), 4)
        if score < 0.3:
            continue

        avg_fare = round(sum(fares) / len(fares), 2) if fares else None
        top_ride_type = max(ride_types, key=ride_types.get) if ride_types else None
        recommended_departure = now.isoformat()
        origin_lat = user_lat if user_lat is not None else best_entry.get("origin_lat")
        origin_lng = user_lng if user_lng is not None else best_entry.get("origin_lng")
        origin_label = best_entry.get("origin_label")
        candidate = {
            "pattern_ref": None,
            "trigger_reasons": ["history_similarity"],
            "origin": {
                "source": "current_location" if user_lat is not None and user_lng is not None else "last_known_origin",
                "label": origin_label,
                "lat": origin_lat,
                "lng": origin_lng,
            },
            "destination_id": None,
            "destination_label": label,
            "destination": {
                "label": label,
                "lat": best_entry.get("dest_lat"),
                "lng": best_entry.get("dest_lng"),
            },
            "usual_departure_time": recommended_departure,
            "recommended_departure_time": recommended_departure,
            "traffic_delta_minutes": 0,
            "ride_options": [],
            "recommended_option": {
                "platform": "uber",
                "ride_type": top_ride_type,
                "price": avg_fare,
                "eta": None,
                "surge_multiplier": 1.0,
                "raw_price_text": None,
                "live_data": False,
                "deeplink": uber_client.build_deeplink(
                    pickup_lat=origin_lat,
                    pickup_lng=origin_lng,
                    dropoff_lat=best_entry.get("dest_lat"),
                    dropoff_lng=best_entry.get("dest_lng"),
                    dropoff_address=label,
                ),
            },
            "surge_flag": False,
            "reason_string": f"This destination appears in your verified Uber history {len(entries)} times and aligns best with the current time.",
            "prediction_kind": "history_ranked",
            "suggestion_id": None,
            "_candidate_score": score,
            "_minute_diff": best_diff,
        }
        candidate = await _hydrate_suggestion_live_quote(candidate, user_lat=user_lat, user_lng=user_lng)
        candidates.append(candidate)

    candidates.sort(key=lambda item: float(item.get("_candidate_score") or 0.0), reverse=True)
    return candidates[:4]


@app.get("/api/patterns/summary")
def patterns_summary():
    return {
        "departure_patterns": db.list_departure_patterns(include_suppressed=True),
        "destination_patterns": db.list_destination_clusters(),
        "order_patterns": db.list_order_patterns(include_suppressed=True),
        "restaurant_patterns": db.list_restaurant_patterns(),
        "cuisine_by_day": db.list_cuisine_by_day(),
    }


@app.get("/api/ride/patterns")
def ride_patterns():
    summary = patterns_summary()
    return {
        "top_patterns": [
            {
                "dropoff": (cluster.get("label") if cluster else None) or f"Destination {item['id']}",
                "expected_time": _format_pattern_time(item.get("hour_bin", 0)),
                "frequency": item.get("frequency"),
                "confidence": item.get("confidence"),
                "day_of_week": item.get("day_of_week"),
            }
            for item in summary["departure_patterns"][:6]
            for cluster in [db.get_destination_cluster(item["destination_id"]) if item.get("destination_id") else None]
        ],
        "departure_patterns": summary["departure_patterns"],
        "destination_patterns": summary["destination_patterns"],
    }


@app.get("/api/food/patterns")
def food_patterns():
    summary = patterns_summary()
    return {
        "top_patterns": [
            {
                "restaurant_name": recent_order.get("restaurant_name") if recent_order else "Swiggy pick",
                "cuisine": item.get("cuisine"),
                "confidence": item.get("confidence"),
            }
            for item in summary["order_patterns"][:6]
            for recent_order in [db.get_recent_order_for_restaurant(item["restaurant_id"]) if item.get("restaurant_id") else None]
        ],
        "order_patterns": summary["order_patterns"],
        "restaurant_patterns": summary["restaurant_patterns"],
        "cuisine_by_day": summary["cuisine_by_day"],
    }


@app.get("/api/suggestions/current")
def current_suggestion():
    return _pending_suggestion_response(db.get_latest_pending_suggestion())


@app.get("/api/ride/suggestion")
async def current_ride_suggestion(
    lat: float | None = Query(None),
    lng: float | None = Query(None),
):
    uber_status_payload = uber_client.get_connection_status()
    if not uber_status_payload.get("connected"):
        return {"suggestion": None}

    candidates = await _build_ranked_ride_candidates(user_lat=lat, user_lng=lng)
    if not candidates:
        return {"suggestion": None}

    strict_candidates = [candidate for candidate in candidates if _has_strict_live_recommendation(candidate)]
    using_strict_live_data = bool(strict_candidates)
    ranked_candidates = strict_candidates if strict_candidates else candidates
    if not using_strict_live_data:
        log.info("Strict Uber live quote unavailable; falling back to best-fit LLM/history candidate")

    suggestion = suggestion_engine.choose_best_current_prediction(
        ranked_candidates,
        current_time=db.utc_now(),
        user_lat=lat,
        user_lng=lng,
    )
    if not suggestion:
        return {"suggestion": None}

    suggestion = await _hydrate_suggestion_live_quote(suggestion, user_lat=lat, user_lng=lng)

    suggestion["llm_validated"] = True
    suggestion["validation_score"] = suggestion.get("_candidate_score")
    suggestion["used_strict_live_data"] = using_strict_live_data
    return {"suggestion": suggestion}


@app.get("/api/food/suggestion")
def current_food_suggestion():
    suggestion = db.get_latest_pending_suggestion()
    if suggestion and suggestion["type"] == "food":
        return {"suggestion": _pending_suggestion_response(suggestion)["suggestion"]}
    return {"suggestion": None}


@app.post("/api/suggestions/{suggestion_id}/confirm")
def confirm_suggestion(suggestion_id: int, payload: ConfirmSuggestionPayload):
    suggestion = db.get_suggestion(suggestion_id)
    if not suggestion:
        raise HTTPException(status_code=404, detail="Suggestion not found.")
    updated = db.update_suggestion_outcome(suggestion_id, "confirmed", edits=payload.edits or {})
    db_payload = updated["payload"]
    pattern_engine.reweight(
        {
            "pattern_ref": db_payload.get("pattern_ref"),
            "event_type": "confirmed",
            "edits": payload.edits or {},
        }
    )
    return {"confirmed": True, "suggestion": _pending_suggestion_response(updated)["suggestion"]}


@app.post("/api/suggestions/{suggestion_id}/dismiss")
def dismiss_suggestion(suggestion_id: int):
    suggestion = db.get_suggestion(suggestion_id)
    if not suggestion:
        raise HTTPException(status_code=404, detail="Suggestion not found.")
    updated = db.update_suggestion_outcome(suggestion_id, "dismissed")
    db_payload = updated["payload"]
    pattern_engine.reweight(
        {
            "pattern_ref": db_payload.get("pattern_ref"),
            "event_type": "dismissed",
            "edits": None,
        }
    )
    return {"dismissed": True, "suggestion_id": suggestion_id}


@app.post("/api/ride/confirm")
def legacy_confirm_ride(payload: LegacyRideActionPayload):
    pending = db.get_latest_pending_suggestion()
    if pending and pending["type"] == "ride":
        updated = db.update_suggestion_outcome(pending["id"], "confirmed")
        db_payload = updated["payload"]
        pattern_engine.reweight(
            {
                "pattern_ref": db_payload.get("pattern_ref"),
                "event_type": "confirmed",
                "edits": None,
            }
        )
        deeplink = db_payload.get("recommended_option", {}).get("deeplink")
        return {"confirmed": True, "deeplink": deeplink}
    return {"confirmed": True, "deeplink": None}


@app.post("/api/ride/dismiss")
def legacy_dismiss_ride(payload: LegacyRideActionPayload):
    pending = db.get_latest_pending_suggestion()
    if pending and pending["type"] == "ride":
        updated = db.update_suggestion_outcome(pending["id"], "dismissed")
        db_payload = updated["payload"]
        pattern_engine.reweight(
            {
                "pattern_ref": db_payload.get("pattern_ref"),
                "event_type": "dismissed",
                "edits": None,
            }
        )
    return {"dismissed": True}


@app.post("/api/food/confirm")
def legacy_confirm_food(payload: dict[str, Any]):
    pending = db.get_latest_pending_suggestion()
    if pending and pending["type"] == "food":
        updated = db.update_suggestion_outcome(pending["id"], "confirmed")
        db_payload = updated["payload"]
        pattern_engine.reweight(
            {
                "pattern_ref": db_payload.get("pattern_ref"),
                "event_type": "confirmed",
                "edits": None,
            }
        )
    return {"confirmed": True}


@app.post("/api/food/dismiss")
def legacy_dismiss_food(payload: dict[str, Any]):
    pending = db.get_latest_pending_suggestion()
    if pending and pending["type"] == "food":
        updated = db.update_suggestion_outcome(pending["id"], "dismissed")
        db_payload = updated["payload"]
        pattern_engine.reweight(
            {
                "pattern_ref": db_payload.get("pattern_ref"),
                "event_type": "dismissed",
                "edits": None,
            }
        )
    return {"dismissed": True}


@app.get("/api/health")
def api_health():
    return health()


@app.get("/api/trigger/log")
def trigger_log():
    return {"events": db.list_trigger_events(limit=20)}


@app.get("/health")
def health():
    return {"platforms": get_scraper_health()}


@app.websocket("/ws/suggestions")
async def suggestions_ws(websocket: WebSocket):
    await websocket.accept()
    queue = await broadcaster.connect()
    try:
        pending = db.get_latest_pending_suggestion()
        if pending:
            await websocket.send_json(_pending_suggestion_response(pending))
        while True:
            payload = await queue.get()
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        broadcaster.disconnect(queue)
    except Exception:
        broadcaster.disconnect(queue)
        raise
