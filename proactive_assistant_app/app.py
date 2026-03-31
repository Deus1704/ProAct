"""FastAPI application: serves frontend and provider integrations."""

import logging
import os
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import database as db
from . import food_client
from . import food_suggestion_engine
from . import ola_client
from . import uber_client
from . import suggestion_engine

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="Proactive Ride Assistant", version="1.0.0")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Startup / Shutdown ────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    db.init_db()
    log.info("Database initialized.")


@app.on_event("shutdown")
async def shutdown():
    await food_client.close_browser()
    await uber_client.close_browser()
    log.info("Browser closed.")


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
@app.get("/rides", response_class=HTMLResponse)
@app.get("/food", response_class=HTMLResponse)
def serve_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    with open(index_path, "r") as f:
        return HTMLResponse(content=f.read())


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    return HTMLResponse(content="""<!DOCTYPE html>
<html><head><title>Privacy Policy</title></head><body>
<h1>Privacy Policy</h1>
<p>This is a local development application for a proactive ride assistant.
No user data is shared with third parties. All data is stored locally in SQLite.</p>
</body></html>""")


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "ride_count": db.get_ride_count(),
        "food_order_count": db.get_food_order_count(),
        "uber_connected": db.get_setting("uber_connected") == "true",
        "ola_connected": db.get_setting("ola_connected") == "true",
        "swiggy_connected": db.get_setting("swiggy_connected") == "true",
        "zomato_connected": db.get_setting("zomato_connected") == "true",
    }


def _error_response(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


# ── Uber Login (Browser-based with screenshot streaming) ─────────────────────

@app.get("/api/uber/status")
def uber_status():
    return uber_client.get_connection_status()


@app.post("/api/uber/login")
async def uber_login():
    """Start headless browser login — returns first screenshot."""
    return await uber_client.start_login()


@app.get("/api/uber/screenshot")
async def uber_screenshot():
    """Get current screenshot of the login page."""
    return await uber_client.get_screenshot()


class ClickPayload(BaseModel):
    x: int
    y: int


@app.post("/api/uber/click")
async def uber_click(payload: ClickPayload):
    """Click at coordinates on the login page."""
    return await uber_client.browser_click(payload.x, payload.y)


class TypePayload(BaseModel):
    text: str


@app.post("/api/uber/type")
async def uber_type(payload: TypePayload):
    """Type text into the focused input on the login page."""
    return await uber_client.browser_type(payload.text)


class KeyPayload(BaseModel):
    key: str


@app.post("/api/uber/key")
async def uber_key(payload: KeyPayload):
    """Press a special key (Enter, Tab, Backspace, etc.)."""
    return await uber_client.browser_key(payload.key)


@app.post("/api/uber/finish-login")
async def uber_finish_login():
    """Navigate to riders.uber.com after login is confirmed."""
    return await uber_client.finish_login()


# ── Uber Sync (Scraping) ─────────────────────────────────────────────────────

@app.post("/api/uber/sync-history")
async def sync_history():
    """Scrape ride history from riders.uber.com."""
    result = await uber_client.scrape_ride_history()
    return result


@app.get("/api/uber/history")
def get_history(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    rides = db.get_ride_history(limit=limit, offset=offset, source_platform="uber")
    return {"rides": rides, "total": db.get_ride_count(source_platform="uber")}


@app.get("/api/rides/history")
def get_all_history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    source: Optional[str] = Query(None),
):
    rides = db.get_ride_history(limit=limit, offset=offset, source_platform=source)
    return {"rides": rides, "total": db.get_ride_count(source_platform=source)}


@app.get("/api/uber/deeplink")
def get_deeplink(
    pickup: str = Query(None),
    dropoff: str = Query(None),
    pickup_lat: float = Query(None),
    pickup_lng: float = Query(None),
    dropoff_lat: float = Query(None),
    dropoff_lng: float = Query(None),
):
    url = uber_client.build_deeplink(
        pickup_address=pickup, dropoff_address=dropoff,
        pickup_lat=pickup_lat, pickup_lng=pickup_lng,
        dropoff_lat=dropoff_lat, dropoff_lng=dropoff_lng,
    )
    return {"deeplink": url}


@app.get("/api/uber/estimates")
async def get_estimates(
    pickup_lat: float = Query(...),
    pickup_lng: float = Query(...),
    dropoff_lat: float = Query(...),
    dropoff_lng: float = Query(...),
):
    return await uber_client.scrape_live_estimates(
        pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
    )


# ── Ola OAuth + Sync ────────────────────────────────────────────────────────

@app.get("/api/ola/status")
def ola_status():
    return ola_client.get_connection_status()


@app.get("/api/ola/login")
def ola_login():
    try:
        return RedirectResponse(url=ola_client.build_oauth_url(), status_code=307)
    except ola_client.OlaConfigError as exc:
        return _error_response(str(exc))


@app.get("/auth/ola/callback", response_class=HTMLResponse)
def ola_callback():
    return HTMLResponse(
        content="""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Ola Connection</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #0d1117; color: #f0f6fc; display: grid; place-items: center; min-height: 100vh; margin: 0; }
    .card { width: min(460px, calc(100vw - 32px)); background: #161b22; border: 1px solid #30363d; border-radius: 18px; padding: 24px; }
    .muted { color: #8b949e; }
  </style>
</head>
<body>
  <div class="card">
    <h1 style="margin-top:0;font-size:1.2rem">Connecting Ola</h1>
    <p class="muted" id="status">Validating your Ola access token...</p>
  </div>
  <script>
    const statusEl = document.getElementById("status");
    const hash = new URLSearchParams(window.location.hash.slice(1));
    const accessToken = hash.get("access_token");
    const expiresIn = hash.get("expires_in");
    const state = hash.get("state");

    if (!accessToken) {
      statusEl.textContent = "Ola did not return an access token. Please close this window and try again.";
    } else {
      fetch("/api/ola/token", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          access_token: accessToken,
          expires_in: expiresIn ? Number(expiresIn) : null,
          state: state
        })
      })
      .then(async (response) => ({ok: response.ok, data: await response.json()}))
      .then(({ok, data}) => {
        statusEl.textContent = ok ? "Ola connected successfully. You can close this window." : (data.error || "Failed to connect Ola.");
        if (window.opener && window.location.origin === window.opener.location.origin) {
          window.opener.postMessage({type: "ola_oauth_result", ok, data}, window.location.origin);
        }
        if (ok) {
          setTimeout(() => window.close(), 1200);
        }
      })
      .catch(() => {
        statusEl.textContent = "Failed to reach the local app. Make sure the server is still running.";
      });
    }
  </script>
</body>
</html>"""
    )


class OlaTokenPayload(BaseModel):
    access_token: str
    expires_in: Optional[int] = None
    state: Optional[str] = None


@app.post("/api/ola/token")
def ola_token(payload: OlaTokenPayload):
    try:
        return ola_client.store_access_token(
            access_token=payload.access_token,
            expires_in=payload.expires_in,
            state=payload.state,
        )
    except (ola_client.OlaConfigError, ola_client.OlaAPIError) as exc:
        return _error_response(str(exc))


@app.post("/api/ola/sync-history")
def ola_sync_history():
    try:
        return ola_client.sync_ride_history()
    except (ola_client.OlaConfigError, ola_client.OlaAPIError) as exc:
        return _error_response(str(exc))


# ── Food Providers (Swiggy / Zomato) ───────────────────────────────────────

@app.get("/api/food/status")
def food_status(provider: Optional[str] = Query(None)):
    try:
        return food_client.get_connection_status(provider=provider)
    except ValueError as exc:
        return _error_response(str(exc))


@app.post("/api/food/login/{provider}")
async def food_login(provider: str):
    try:
        return await food_client.start_login(provider)
    except ValueError as exc:
        return _error_response(str(exc))



class CookieLoginPayload(BaseModel):
    cookie: str

@app.post("/api/food/login/zomato/cookie")
async def food_login_zomato_cookie(payload: CookieLoginPayload):
    try:
        return await food_client.zomato_cookie_login(payload.cookie)
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
    except ValueError as exc:
        return _error_response(str(exc))


@app.post("/api/food/sync-history")
async def food_sync_history(provider: Optional[str] = Query(None)):
    try:
        return await food_client.sync_order_history(provider=provider)
    except ValueError as exc:
        return _error_response(str(exc))


@app.get("/api/food/history")
def food_history(
    limit: int = Query(50, ge=1, le=250),
    offset: int = Query(0, ge=0),
    source: Optional[str] = Query(None),
):
    return {
        "orders": db.get_food_order_history(limit=limit, offset=offset, source_platform=source),
        "total": db.get_food_order_count(source_platform=source),
    }


@app.get("/api/food/suggestion")
def food_suggestion():
    suggestion = food_suggestion_engine.get_suggestion()
    if not suggestion:
        return {"suggestion": None, "message": "No food suggestion right now."}
    return {"suggestion": suggestion}


@app.get("/api/food/patterns")
def food_patterns():
    return food_suggestion_engine.get_pattern_summary()


class FoodConfirmPayload(BaseModel):
    route_key: str
    source_platform: Optional[str] = None
    restaurant_name: Optional[str] = None
    item_name: Optional[str] = None


@app.post("/api/food/confirm")
def food_confirm(payload: FoodConfirmPayload):
    db.log_food_interaction(
        "confirm",
        suggestion_payload=payload.model_dump(),
        route_key=payload.route_key,
    )
    return {"confirmed": True}


class FoodDismissPayload(BaseModel):
    route_key: str
    reason: Optional[str] = None


@app.post("/api/food/dismiss")
def food_dismiss(payload: FoodDismissPayload):
    db.log_food_interaction(
        "dismiss",
        suggestion_payload={"reason": payload.reason},
        route_key=payload.route_key,
    )
    return {"dismissed": True}


# ── Suggestion Flow ──────────────────────────────────────────────────────────

@app.get("/api/ride/suggestion")
def get_suggestion(
    lat: float = Query(None),
    lng: float = Query(None),
):
    suggestion = suggestion_engine.get_suggestion(user_lat=lat, user_lng=lng)
    if not suggestion:
        return {"suggestion": None, "message": "No ride suggestion at this time."}
    return {"suggestion": suggestion}


@app.get("/api/ride/upcoming")
def get_upcoming():
    return {"upcoming": suggestion_engine.get_upcoming_rides()}


@app.get("/api/ride/patterns")
def get_patterns():
    return suggestion_engine.get_pattern_summary()


class ConfirmPayload(BaseModel):
    route_key: str
    pickup: Optional[str] = None
    dropoff: Optional[str] = None
    ride_type: Optional[str] = None


@app.post("/api/ride/confirm")
def confirm_ride(payload: ConfirmPayload):
    db.log_interaction(
        "confirm",
        suggestion_payload=payload.model_dump(),
        route_key=payload.route_key,
    )
    deeplink = uber_client.build_deeplink(
        pickup_address=payload.pickup,
        dropoff_address=payload.dropoff,
    )
    return {"confirmed": True, "deeplink": deeplink}


class DismissPayload(BaseModel):
    route_key: str
    reason: Optional[str] = None


@app.post("/api/ride/dismiss")
def dismiss_ride(payload: DismissPayload):
    db.log_interaction(
        "dismiss",
        suggestion_payload={"reason": payload.reason},
        route_key=payload.route_key,
    )
    return {"dismissed": True}


class EditPayload(BaseModel):
    route_key: str
    edited_fields: dict


@app.post("/api/ride/edit")
def edit_ride(payload: EditPayload):
    db.log_interaction(
        "edit",
        suggestion_payload=payload.model_dump(),
        edited_fields=payload.edited_fields,
        route_key=payload.route_key,
    )
    deeplink = uber_client.build_deeplink(
        pickup_address=payload.edited_fields.get("pickup"),
        dropoff_address=payload.edited_fields.get("dropoff"),
    )
    return {"edited": True, "deeplink": deeplink}
