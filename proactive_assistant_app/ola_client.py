"""Ola partner API integration for OAuth and ride history sync.

Uses Ola's documented partner OAuth flow and the `/v1/bookings/my_rides`
endpoint. No mock data is generated here: the client only works when real
partner credentials and a real user access token are available.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import requests

from . import database as db

log = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://devapi.olacabs.com"
DEFAULT_SCOPE = "profile booking"
BOOKINGS_PER_PAGE = 3


class OlaConfigError(ValueError):
    """Raised when required Ola partner configuration is missing."""


class OlaAPIError(RuntimeError):
    """Raised when Ola returns an API or transport failure."""


def _api_base() -> str:
    return os.getenv("OLA_API_BASE", DEFAULT_API_BASE).rstrip("/")


def _partner_token() -> str:
    token = os.getenv("OLA_APP_TOKEN", "").strip()
    if not token:
        raise OlaConfigError("Missing OLA_APP_TOKEN. Add your Ola partner token to .env.")
    return token


def _client_id() -> str:
    client_id = os.getenv("OLA_CLIENT_ID", "").strip()
    if not client_id:
        raise OlaConfigError("Missing OLA_CLIENT_ID. Add your Ola client ID to .env.")
    return client_id


def _redirect_uri() -> str:
    redirect_uri = os.getenv("OLA_REDIRECT_URI", "").strip()
    if not redirect_uri:
        raise OlaConfigError("Missing OLA_REDIRECT_URI. Add your Ola OAuth redirect URI to .env.")
    return redirect_uri


def _oauth_scope() -> str:
    return os.getenv("OLA_OAUTH_SCOPE", DEFAULT_SCOPE).strip() or DEFAULT_SCOPE


def _access_token() -> str:
    token = db.get_setting("ola_access_token", "")
    if not token:
        raise OlaAPIError("Ola is not connected yet. Complete the OAuth flow first.")
    return token


def has_partner_config() -> bool:
    """Return whether the app has the minimum Ola partner configuration."""
    return bool(
        os.getenv("OLA_CLIENT_ID", "").strip()
        and os.getenv("OLA_APP_TOKEN", "").strip()
        and os.getenv("OLA_REDIRECT_URI", "").strip()
    )


def get_connection_status() -> dict[str, Any]:
    """Return connection state for the UI."""
    return {
        "configured": has_partner_config(),
        "connected": bool(db.get_setting("ola_access_token")),
        "history_synced": db.get_setting("ola_history_synced") == "true",
        "last_sync_time": db.get_setting("ola_last_sync_time"),
        "token_expires_at": db.get_setting("ola_access_token_expires_at"),
    }


def build_oauth_url() -> str:
    """Build the documented Ola OAuth authorization URL."""
    state = secrets.token_urlsafe(16)
    db.set_setting("ola_oauth_state", state)
    query = urlencode(
        {
            "response_type": "token",
            "client_id": _client_id(),
            "redirect_uri": _redirect_uri(),
            "scope": _oauth_scope(),
            "state": state,
        }
    )
    return f"{_api_base()}/oauth2/authorize?{query}"


def store_access_token(
    access_token: str,
    expires_in: int | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    """Persist a user access token after validating it against Ola."""
    if not access_token:
        raise OlaAPIError("No access token received from Ola.")

    expected_state = db.get_setting("ola_oauth_state")
    if expected_state and state and expected_state != state:
        raise OlaAPIError("OAuth state mismatch. Please retry the Ola login flow.")

    probe = fetch_my_rides_page(page=1, access_token=access_token)
    db.set_setting("ola_access_token", access_token)
    db.set_setting("ola_connected", "true")

    if expires_in:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        db.set_setting("ola_access_token_expires_at", expires_at.isoformat())

    return {
        "connected": True,
        "bookings_visible": len(probe.get("bookings", [])),
    }


def fetch_my_rides_page(page: int = 1, access_token: str | None = None) -> dict[str, Any]:
    """Fetch one page from Ola's documented my_rides endpoint."""
    headers = {
        "Authorization": f"Bearer {access_token or _access_token()}",
        "X-APP-TOKEN": _partner_token(),
        "Content-Type": "application/json",
    }
    response = requests.get(
        f"{_api_base()}/v1/bookings/my_rides",
        params={"page": page},
        headers=headers,
        timeout=30,
    )

    if response.status_code == 401:
        db.set_setting("ola_connected", "false")
        raise OlaAPIError(
            "Ola access token is invalid or expired. Reconnect the account and try again."
        )

    if response.status_code >= 400:
        raise OlaAPIError(f"Ola my_rides failed with HTTP {response.status_code}: {response.text[:300]}")

    try:
        payload = response.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise OlaAPIError("Ola my_rides returned a non-JSON response.") from exc

    if not isinstance(payload, dict):
        raise OlaAPIError("Unexpected Ola response shape from my_rides.")
    return payload


def sync_ride_history(max_pages: int = 20) -> dict[str, Any]:
    """Sync paginated ride history from Ola's documented my_rides endpoint."""
    total_synced = 0
    total_found = 0

    for page in range(1, max_pages + 1):
        payload = fetch_my_rides_page(page=page)
        rides = parse_my_rides_response(payload)
        if not rides:
            break

        total_found += len(rides)
        for ride in rides:
            db.insert_ride(ride)
            total_synced += 1

        if len(payload.get("bookings", [])) < BOOKINGS_PER_PAGE:
            break

    if total_synced:
        db.set_setting("ola_history_synced", "true")
        db.set_setting("ola_last_sync_time", datetime.now(timezone.utc).isoformat())

    return {"synced": total_synced, "total_found": total_found}


def parse_my_rides_response(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Map Ola `my_rides` bookings to the app's internal ride schema."""
    bookings = payload.get("bookings", [])
    if not isinstance(bookings, list):
        return []

    rides: list[dict[str, Any]] = []
    for booking in bookings:
        if not isinstance(booking, dict):
            continue
        ride = _parse_booking(booking)
        if ride:
            rides.append(ride)
    return rides


def _parse_booking(booking: dict[str, Any]) -> dict[str, Any] | None:
    booking_id = str(booking.get("booking_id", "")).strip()
    if not booking_id:
        return None

    return {
        "external_ride_id": booking_id,
        "source_platform": "ola",
        "pickup_address": _first_present(
            booking,
            "pickup_address",
            "pickup_address_line1",
            "pickup_location",
        ),
        "dropoff_address": _first_present(
            booking,
            "drop_address",
            "dropoff_address",
            "drop_address_line1",
            "drop_location",
        ),
        "pickup_lat": _clean_coordinate(booking.get("pickup_lat")),
        "pickup_lng": _clean_coordinate(booking.get("pickup_lng")),
        "dropoff_lat": _clean_coordinate(booking.get("drop_lat")),
        "dropoff_lng": _clean_coordinate(booking.get("drop_lng")),
        "request_timestamp": _parse_booking_time(booking.get("booking_time")),
        "ride_type": booking.get("category") or booking.get("cab_type") or "ola",
        "price": _maybe_float(
            booking.get("bill_amount")
            or booking.get("fare")
            or booking.get("amount")
        ),
        "duration_minutes": _maybe_float(
            booking.get("ride_time")
            or booking.get("ride_duration")
            or booking.get("travel_time")
        ),
        "distance_miles": _kilometres_to_miles(
            _maybe_float(booking.get("distance") or booking.get("ride_distance"))
        ),
        "raw_payload": booking,
    }


def _first_present(booking: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = booking.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _maybe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_coordinate(value: Any) -> float | None:
    coordinate = _maybe_float(value)
    if coordinate in (None, 0.0):
        return None
    return coordinate


def _kilometres_to_miles(distance_km: float | None) -> float | None:
    if distance_km is None:
        return None
    return distance_km * 0.621371


def _parse_booking_time(value: Any) -> str:
    if not value:
        return datetime.now(timezone.utc).isoformat()

    raw_value = str(value).strip()
    normalized = re.sub(r"\s+[A-Z]{2,4}\s+([+-]\d{2}:\d{2})$", r" \1", raw_value)

    try:
        parsed = datetime.strptime(normalized, "%a, %d %b %Y %H:%M:%S %z")
        return parsed.isoformat()
    except ValueError:
        log.warning("Could not parse Ola booking_time: %s", raw_value)
        return datetime.now(timezone.utc).isoformat()
