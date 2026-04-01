"""Uber integration via headless Playwright with screenshot streaming.

The user interacts with Uber's login page through a remote browser viewer
rendered in our frontend. Clicks and keystrokes are relayed to the headless browser.

After login, the session cookies are persisted and used for scraping
ride history and live estimates.
"""

import json
import logging
import os
import base64
import re
import asyncio
from datetime import datetime, timedelta
from urllib.parse import urlencode
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from . import database as db

log = logging.getLogger(__name__)

SESSION_DIR = os.path.join(os.path.dirname(__file__), "browser_session")
COOKIES_FILE = os.path.join(SESSION_DIR, "cookies.json")

# Global browser state
_browser: Browser | None = None
_context: BrowserContext | None = None
_login_page: Page | None = None
_popup_page: Page | None = None
_scrape_page: Page | None = None  # Persistent page kept alive for background scraping
_playwright = None


# ── Browser Lifecycle ────────────────────────────────────────────────────────

async def _ensure_browser() -> BrowserContext:
    """Start headless browser and restore session if cookies exist."""
    global _browser, _context, _playwright

    if _context:
        try:
            await _context.pages
            return _context
        except Exception:
            _context = None
            _browser = None

    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    _context = await _browser.new_context(
        viewport={"width": 420, "height": 820},
        user_agent="Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
        is_mobile=True,
        has_touch=True,
        timezone_id="Asia/Kolkata",
    )

    # Restore cookies if saved
    if os.path.exists(COOKIES_FILE):
        try:
            with open(COOKIES_FILE, "r") as f:
                cookies = json.load(f)
            await _context.add_cookies(cookies)
            log.info("Restored %d cookies from saved session", len(cookies))
        except Exception as e:
            log.warning("Failed to restore cookies: %s", e)

    return _context


async def _save_cookies():
    """Persist current browser cookies to disk."""
    if not _context:
        return
    os.makedirs(SESSION_DIR, exist_ok=True)
    cookies = await _context.cookies()
    with open(COOKIES_FILE, "w") as f:
        json.dump(cookies, f)
    log.info("Saved %d cookies", len(cookies))


async def close_browser():
    """Shut down the browser."""
    global _browser, _context, _login_page, _playwright, _scrape_page
    if _login_page:
        try:
            await _login_page.close()
        except Exception:
            pass
        _login_page = None
    if _scrape_page:
        try:
            await _scrape_page.close()
        except Exception:
            pass
        _scrape_page = None
    if _context:
        await _save_cookies()
        await _context.close()
        _context = None
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None


# ── Login Flow (Screenshot-Streamed) ─────────────────────────────────────────

def _active_page() -> Page | None:
    """Return the popup page if open, otherwise the main login page."""
    global _popup_page, _login_page
    if _popup_page:
        try:
            # Check if popup is still open
            _ = _popup_page.url
            return _popup_page
        except Exception:
            _popup_page = None
    return _login_page


async def start_login() -> dict:
    """Open Uber login page in a headless browser.
    Returns the first screenshot for the frontend to display.
    """
    global _login_page, _popup_page
    try:
        ctx = await _ensure_browser()

        # Close old pages if they exist
        if _login_page:
            try:
                await _login_page.close()
            except Exception:
                pass
        _popup_page = None

        _login_page = await ctx.new_page()

        # Listen for popups (Google OAuth opens in a popup)
        def on_popup(popup):
            global _popup_page
            _popup_page = popup
            log.info("Popup opened: %s", popup.url)

        _login_page.on("popup", on_popup)

        await _login_page.goto("https://auth.uber.com/v2/", wait_until="domcontentloaded", timeout=30000)
        await _login_page.wait_for_timeout(2000)

        screenshot = await _login_page.screenshot(type="png")
        b64 = base64.b64encode(screenshot).decode("utf-8")

        db.set_setting("login_status", "in_progress")
        return {
            "status": "login_started",
            "screenshot": b64,
            "url": _login_page.url,
            "message": "Login page loaded. Interact with it below.",
        }
    except Exception as e:
        log.error("Failed to start login: %s", e)
        return {"status": "error", "message": str(e)}


async def get_screenshot() -> dict:
    """Take a screenshot of the active page (popup or main)."""
    global _popup_page
    page = _active_page()
    if not page:
        return {"status": "no_page", "screenshot": None}

    try:
        await page.wait_for_timeout(300)
        screenshot = await page.screenshot(type="png")
        b64 = base64.b64encode(screenshot).decode("utf-8")
        url = page.url

        logged_in = await _check_if_logged_in()

        return {
            "status": "logged_in" if logged_in else "in_progress",
            "screenshot": b64,
            "url": url,
            "logged_in": logged_in,
            "is_popup": page == _popup_page,
        }
    except Exception as e:
        # Popup may have closed — fall back to main page
        _popup_page = None
        log.warning("Screenshot failed (popup may have closed): %s", e)
        if _login_page:
            try:
                screenshot = await _login_page.screenshot(type="png")
                b64 = base64.b64encode(screenshot).decode("utf-8")
                logged_in = await _check_if_logged_in()
                return {
                    "status": "logged_in" if logged_in else "in_progress",
                    "screenshot": b64,
                    "url": _login_page.url,
                    "logged_in": logged_in,
                }
            except Exception:
                pass
        return {"status": "error", "message": str(e)}


async def browser_click(x: int, y: int) -> dict:
    """Click at coordinates on the active page."""
    global _popup_page
    page = _active_page()
    if not page:
        return {"status": "no_page"}

    try:
        await page.mouse.click(x, y)
        await page.wait_for_timeout(1000)

        # After click, check if a popup appeared or if we need to switch
        active = _active_page()
        screenshot = await active.screenshot(type="png")
        b64 = base64.b64encode(screenshot).decode("utf-8")
        logged_in = await _check_if_logged_in()

        return {
            "status": "logged_in" if logged_in else "ok",
            "screenshot": b64,
            "url": active.url,
            "logged_in": logged_in,
            "is_popup": active == _popup_page,
        }
    except Exception as e:
        log.error("Click failed: %s", e)
        _popup_page = None
        if _login_page:
            try:
                screenshot = await _login_page.screenshot(type="png")
                b64 = base64.b64encode(screenshot).decode("utf-8")
                logged_in = await _check_if_logged_in()
                return {"status": "logged_in" if logged_in else "ok", "screenshot": b64, "url": _login_page.url, "logged_in": logged_in}
            except Exception:
                pass
        return {"status": "error", "message": str(e)}


async def browser_type(text: str) -> dict:
    """Type text into the currently focused element."""
    page = _active_page()
    if not page:
        return {"status": "no_page"}

    try:
        await page.keyboard.type(text, delay=50)
        await page.wait_for_timeout(300)

        screenshot = await page.screenshot(type="png")
        b64 = base64.b64encode(screenshot).decode("utf-8")

        return {
            "status": "ok",
            "screenshot": b64,
            "url": page.url,
        }
    except Exception as e:
        log.error("Type failed: %s", e)
        return {"status": "error", "message": str(e)}


async def browser_key(key: str) -> dict:
    """Press a special key (Enter, Tab, Backspace, etc.)."""
    global _popup_page
    page = _active_page()
    if not page:
        return {"status": "no_page"}

    try:
        await page.keyboard.press(key)
        await page.wait_for_timeout(1000)

        active = _active_page()
        screenshot = await active.screenshot(type="png")
        b64 = base64.b64encode(screenshot).decode("utf-8")
        logged_in = await _check_if_logged_in()

        return {
            "status": "logged_in" if logged_in else "ok",
            "screenshot": b64,
            "url": active.url,
            "logged_in": logged_in,
        }
    except Exception as e:
        log.error("Key press failed: %s", e)
        _popup_page = None
        if _login_page:
            try:
                screenshot = await _login_page.screenshot(type="png")
                b64 = base64.b64encode(screenshot).decode("utf-8")
                logged_in = await _check_if_logged_in()
                return {"status": "logged_in" if logged_in else "ok", "screenshot": b64, "url": _login_page.url, "logged_in": logged_in}
            except Exception:
                pass
        return {"status": "error", "message": str(e)}


async def browser_clear_and_type(text: str) -> dict:
    """Select all text in focused input and replace with new text."""
    page = _active_page()
    if not page:
        return {"status": "no_page"}

    try:
        await page.keyboard.press("Control+a")
        await page.keyboard.type(text, delay=50)
        await page.wait_for_timeout(300)

        screenshot = await page.screenshot(type="png")
        b64 = base64.b64encode(screenshot).decode("utf-8")

        return {
            "status": "ok",
            "screenshot": b64,
            "url": page.url,
        }
    except Exception as e:
        log.error("Clear and type failed: %s", e)
        return {"status": "error", "message": str(e)}


async def _check_if_logged_in() -> bool:
    """Check if user has completed Uber login."""
    global _login_page
    if not _login_page:
        return False

    try:
        url = _login_page.url
        # After login, Uber redirects away from auth.uber.com
        if "riders.uber.com" in url or ("m.uber.com" in url and "auth" not in url):
            await _save_cookies()
            db.set_setting("uber_connected", "true")
            db.set_setting("login_status", "logged_in")
            db.set_setting("login_time", datetime.utcnow().isoformat())
            return True

        # Check cookies for session markers
        cookies = await _login_page.context.cookies(["https://uber.com", "https://auth.uber.com"])
        cookie_names = {c["name"] for c in cookies}
        if "sid" in cookie_names or "csid" in cookie_names:
            await _save_cookies()
            db.set_setting("uber_connected", "true")
            db.set_setting("login_status", "logged_in")
            db.set_setting("login_time", datetime.utcnow().isoformat())
            return True

        return False
    except Exception:
        return False


async def finish_login() -> dict:
    """Called after login is confirmed. Navigates to riders.uber.com and saves session."""
    global _login_page
    if not _login_page:
        return {"status": "error", "message": "No browser page"}

    try:
        await _login_page.goto("https://riders.uber.com/trips", wait_until="domcontentloaded", timeout=20000)
        await _login_page.wait_for_timeout(2000)
        await _save_cookies()

        screenshot = await _login_page.screenshot(type="png")
        b64 = base64.b64encode(screenshot).decode("utf-8")

        return {
            "status": "ok",
            "screenshot": b64,
            "url": _login_page.url,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── Ride History Scraping ────────────────────────────────────────────────────

ACTIVITIES_QUERY = """query Activities($cityID: Int, $endTimeMs: Float, $includePast: Boolean = true, $includeUpcoming: Boolean = true, $limit: Int = 5, $nextPageToken: String, $orderTypes: [RVWebCommonActivityOrderType!] = [RIDES, TRAVEL], $profileType: RVWebCommonActivityProfileType = PERSONAL, $startTimeMs: Float) {
  activities(cityID: $cityID) {
    cityID
    past(
      endTimeMs: $endTimeMs
      limit: $limit
      nextPageToken: $nextPageToken
      orderTypes: $orderTypes
      profileType: $profileType
      startTimeMs: $startTimeMs
    ) @include(if: $includePast) {
      activities {
        ...RVWebCommonActivityFragment
        __typename
      }
      nextPageToken
      __typename
    }
    upcoming @include(if: $includeUpcoming) {
      activities {
        ...RVWebCommonActivityFragment
        __typename
      }
      __typename
    }
    __typename
  }
}

fragment RVWebCommonActivityFragment on RVWebCommonActivity {
  buttons {
    isDefault
    startEnhancerIcon
    text
    url
    __typename
  }
  cardURL
  description
  imageURL {
    light
    dark
    __typename
  }
  subtitle
  title
  uuid
  __typename
}"""


async def scrape_ride_history(max_pages: int = 10) -> dict:
    """Scrape ride history via Uber's GraphQL API at riders.uber.com/graphql.

    Uses the exact same Activities query that riders.uber.com/trips uses.
    Paginates with nextPageToken to fetch all history.
    The scraping page is kept alive between calls for persistent background use.
    """
    global _scrape_page
    ctx = await _ensure_browser()

    # Reuse the persistent scrape page instead of opening/closing one each time.
    if not _scrape_page or _scrape_page.is_closed():
        _scrape_page = await ctx.new_page()
        log.info("Created persistent Uber scrape page")

    page = _scrape_page

    try:
        # Navigate to riders.uber.com to be on the right domain
        log.info("Navigating to riders.uber.com")
        await page.goto("https://riders.uber.com/trips", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)

        # Check if redirected to login – navigate back to a neutral page and mark disconnected
        if "auth.uber.com" in page.url:
            db.set_setting("uber_connected", "false")
            return {"synced": 0, "error": "Session expired. Please log in again."}

        # Intercept CSRF token from the page's own requests
        csrf_token = None

        async def capture_csrf(request):
            nonlocal csrf_token
            headers = request.headers
            for name in ["x-csrf-token", "csrf-token", "x-xsrf-token"]:
                if name in headers and headers[name]:
                    csrf_token = headers[name]
                    log.info("Captured CSRF from request header: %s", csrf_token[:30])

        page.on("request", capture_csrf)

        # Reload to capture the CSRF from the page's own GraphQL calls
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        # Also try extracting from the page DOM/JS
        if not csrf_token:
            csrf_token = await page.evaluate("""() => {
            // Check meta tags
            const meta = document.querySelector('meta[name="csrf-token"]') ||
                         document.querySelector('meta[name="_csrf"]') ||
                         document.querySelector('meta[name="csrf"]');
            if (meta) return meta.getAttribute('content');

            // Check cookies
            const cookies = document.cookie.split(';');
            for (const c of cookies) {
                const [name, val] = c.trim().split('=');
                if (name && (name.toLowerCase().includes('csrf') || name.toLowerCase().includes('xsrf'))) {
                    return val;
                }
            }

            // Check window.__CSRF or similar globals
            if (window.__CSRF_TOKEN__) return window.__CSRF_TOKEN__;
            if (window.__csrf) return window.__csrf;

            // Check for token in script tags
            const scripts = document.querySelectorAll('script');
            for (const s of scripts) {
                const text = s.textContent || '';
                const match = text.match(/csrf[_-]?token["']?\\s*[:=]\\s*["']([^"']+)["']/i);
                if (match) return match[1];
            }

            return null;
        }""")
        log.info("CSRF token: %s", csrf_token[:20] + "..." if csrf_token else "None (will try without)")

        # Fetch trips via GraphQL, paginating through all results
        all_trips = []
        next_token = None

        for page_num in range(max_pages):
            log.info("Fetching trips page %d (token: %s)", page_num + 1, next_token[:20] if next_token else "None")

            variables = {
                "includePast": True,
                "includeUpcoming": page_num == 0,
                "limit": 50,
                "orderTypes": ["RIDES", "TRAVEL"],
                "profileType": "PERSONAL",
            }
            if next_token:
                variables["nextPageToken"] = next_token

            result = await page.evaluate("""async (vars) => {
                try {
                    const headers = {'Content-Type': 'application/json'};
                    if (vars.csrf) {
                        headers['x-csrf-token'] = vars.csrf;
                    }
                    const resp = await fetch('/graphql', {
                        method: 'POST',
                        headers: headers,
                        body: JSON.stringify({
                            operationName: 'Activities',
                            variables: vars.variables,
                            query: vars.query,
                        }),
                        credentials: 'include',
                    });
                    return await resp.json();
                } catch(e) {
                    return {error: e.message};
                }
            }""", {"variables": variables, "query": ACTIVITIES_QUERY, "csrf": csrf_token})

            if not result or result.get("error"):
                log.error("GraphQL request failed: %s", result)
                break

            past = result.get("data", {}).get("activities", {}).get("past", {})
            activities = past.get("activities", [])
            next_token = past.get("nextPageToken")

            if not activities:
                log.info("No more trips on page %d", page_num + 1)
                break

            all_trips.extend(activities)
            log.info("Fetched %d trips on page %d (total: %d)", len(activities), page_num + 1, len(all_trips))

            if not next_token:
                log.info("No more pages")
                break

        # Parse and store trips
        synced = 0
        for trip in all_trips:
            ride = _parse_activity_trip(trip)
            if ride:
                db.insert_ride(ride)
                synced += 1

        # Page intentionally NOT closed — kept alive for background price/ETA polling.
        log.info("Uber scrape page kept alive. Tabs open: %d", len(ctx.pages))

        if synced > 0:
            db.set_setting("last_sync_time", datetime.utcnow().isoformat())
            db.set_setting("uber_history_synced", "true")

        return {"synced": synced, "total_found": len(all_trips)}

    except Exception as e:
        log.error("Ride history scraping failed: %s", e)
        # Don't close the page on error — just mark the scrape page stale so
        # the next call will create a fresh one if needed.
        if _scrape_page and _scrape_page.is_closed():
            _scrape_page = None
        return {"synced": 0, "error": str(e)}


def _parse_activity_trip(trip: dict) -> dict | None:
    """Parse a single trip from the Activities GraphQL response.

    Each trip has: title (destination), subtitle (date/time), description (price),
    uuid, cardURL, buttons, imageURL.
    """
    try:
        title = trip.get("title", "")  # Destination name
        subtitle = trip.get("subtitle", "")  # e.g., "Mar 28 • 3:53 AM"
        description = trip.get("description", "")  # e.g., "₹145.87" or "₹0.00 • Canceled"
        uuid = trip.get("uuid", "")
        card_url = trip.get("cardURL", "")

        if not uuid:
            return None

        # Filter out Uber Eats orders that share the same backend graph
        if "ubereats" in card_url.lower() or "eats.uber" in card_url.lower():
            return None
        
        # Also check buttons for "ubereats" link
        buttons = trip.get("buttons", [])
        for btn in buttons:
            if "ubereats" in (btn.get("url") or "").lower():
                return None
            if btn.get("text", "").lower() == "reorder":
                return None

        # Parse price from description
        price = None
        canceled = "cancel" in description.lower()
        price_match = re.search(r'[₹$€£]\s*([\d,.]+)', description)
        if price_match and not canceled:
            price = float(price_match.group(1).replace(",", ""))

        # Parse date/time from subtitle (e.g., "Mar 28 • 3:53 AM")
        ts = datetime.utcnow().isoformat()
        date_match = re.match(r'(\w{3}\s+\d{1,2})\s*[•·]\s*(\d{1,2}:\d{2}\s*(?:AM|PM))', subtitle)
        if date_match:
            date_str = date_match.group(1)
            time_str = date_match.group(2)
            current_year = datetime.utcnow().year
            try:
                parsed = datetime.strptime(f"{date_str} {current_year} {time_str}", "%b %d %Y %I:%M %p")
                if parsed > datetime.utcnow():
                    parsed = parsed.replace(year=current_year - 1)
                ts = parsed.isoformat()
            except ValueError:
                pass

        # Extract coordinates from the static map image URL
        pickup_lat, pickup_lng = None, None
        image_url = trip.get("imageURL", {})
        if isinstance(image_url, dict):
            light_url = image_url.get("light", "")
            lat_match = re.search(r'lat%3A([\d.]+)', light_url)
            lng_match = re.search(r'lng%3A([\d.]+)', light_url)
            if lat_match and lng_match:
                pickup_lat = float(lat_match.group(1))
                pickup_lng = float(lng_match.group(1))

        return {
            "external_ride_id": uuid,
            "source_platform": "uber",
            "pickup_address": "Unknown pickup",
            "dropoff_address": title,
            "pickup_lat": pickup_lat,
            "pickup_lng": pickup_lng,
            "request_timestamp": ts,
            "ride_type": "UberX",
            "price": price,
            "duration_minutes": None,
            "distance_miles": None,
            "raw_payload": trip,
        }

    except Exception as e:
        log.warning("Failed to parse trip: %s", e)
        return None


def _parse_trips_from_text(text: str) -> list[dict]:
    """Parse trip data from the m.uber.com page text.

    The format we see is like:
        Sabarmati BG Railway Station
        Mar 28 • 3:53 AM
        ₹0.00 • Canceled
        Help
        Indian Institute Of Technology Gandhinagar (IIT Gandhinagar)
        Mar 23 • 6:10 AM
        ₹145.87
        Help
    """
    rides = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # Find the "Past" section
    past_idx = None
    for i, line in enumerate(lines):
        if line == "Past":
            past_idx = i
            break

    if past_idx is None:
        log.warning("Could not find 'Past' section in page text")
        return rides

    # Parse trips after "Past" section
    # Skip filter lines like "Personal", "All Trips"
    i = past_idx + 1
    while i < len(lines) and lines[i] in ("Personal", "All Trips", "Business"):
        i += 1

    # Now parse trip blocks
    current_year = datetime.utcnow().year

    while i < len(lines):
        line = lines[i]

        # Skip non-trip lines
        if line in ("Help", "More", "Reserve ride", "Upcoming", "You have no upcoming trips"):
            i += 1
            continue

        # Try to detect a trip block:
        # Line 1: Destination name
        # Line 2: Date • Time (e.g., "Mar 28 • 3:53 AM")
        # Line 3: Price, optionally "• Canceled" (e.g., "₹145.87" or "₹0.00 • Canceled")
        # Line 4: "Help"

        destination = line

        # Check if next line looks like a date
        if i + 1 < len(lines):
            date_line = lines[i + 1]
            date_match = re.match(r'(\w{3}\s+\d{1,2})\s*[•·]\s*(\d{1,2}:\d{2}\s*(?:AM|PM))', date_line)

            if date_match:
                date_str = date_match.group(1)
                time_str = date_match.group(2)

                # Parse price from next line
                price = None
                canceled = False
                ride_type = "UberX"

                if i + 2 < len(lines):
                    price_line = lines[i + 2]
                    price_match = re.search(r'[₹$€£]\s*([\d,.]+)', price_line)
                    if price_match:
                        price = float(price_match.group(1).replace(",", ""))
                    canceled = "cancel" in price_line.lower()

                # Build timestamp
                try:
                    ts = datetime.strptime(f"{date_str} {current_year} {time_str}", "%b %d %Y %I:%M %p")
                    # If the date is in the future, it's from last year
                    if ts > datetime.utcnow():
                        ts = ts.replace(year=current_year - 1)
                    ts_str = ts.isoformat()
                except ValueError:
                    ts_str = datetime.utcnow().isoformat()

                ride_id = f"uber-{date_str.replace(' ', '')}-{time_str.replace(' ', '').replace(':', '')}"

                rides.append({
                    "external_ride_id": ride_id,
                    "source_platform": "uber",
                    "pickup_address": "Unknown pickup",
                    "dropoff_address": destination,
                    "request_timestamp": ts_str,
                    "ride_type": ride_type,
                    "price": price if not canceled else None,
                    "duration_minutes": None,
                    "distance_miles": None,
                    "raw_payload": {
                        "destination": destination,
                        "date": date_str,
                        "time": time_str,
                        "price_line": lines[i + 2] if i + 2 < len(lines) else None,
                        "canceled": canceled,
                    },
                })
                
                # Check if it was actually an eats order (sometimes they include "Order" instead of "Ride")
                if "eats" in destination.lower() or "delivery" in destination.lower():
                    rides.pop()

                log.info("Parsed trip: %s on %s %s - ₹%s%s",
                         destination, date_str, time_str, price, " (canceled)" if canceled else "")

                # Skip past this trip block (destination + date + price + Help)
                i += 4
                continue

        i += 1

    log.info("Parsed %d trips from page text", len(rides))
    return rides


def _find_trip_list(data, depth: int = 0) -> list | None:
    """Recursively search a nested dict/list for an array of trip-like objects.

    Strict matching: requires at least 2 trip-specific keys to avoid matching
    vehicles, promotions, locations, etc.
    """
    if depth > 8:
        return None

    if isinstance(data, list):
        if len(data) > 0 and isinstance(data[0], dict):
            first = data[0]
            keys = set(first.keys())

            # Must have at least 2 of these trip-specific keys
            trip_indicators = {"uuid", "tripUUID", "request_id", "beginTripTime",
                              "dropoffTime", "vehicleViewName", "fare", "waypoints",
                              "receipt", "clientFare", "status", "title", "subtitle",
                              "moneyInfo", "timeInfo"}
            # Must NOT have these non-trip keys
            non_trip_indicators = {"bearing", "etaInMin", "etaStringShort",
                                  "addressLine1", "categories", "confidence", "provider",
                                  "promotionUuid", "displayDate", "restrictions",
                                  "currencyCode", "nearbyVehicles", "savedPlacesMeta"}

            matches = trip_indicators & keys
            anti_matches = non_trip_indicators & keys

            if len(matches) >= 2 and len(anti_matches) == 0:
                return data

        for item in data:
            result = _find_trip_list(item, depth + 1)
            if result:
                return result

    elif isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, (dict, list)):
                result = _find_trip_list(val, depth + 1)
                if result:
                    return result

    return None


def _parse_api_trips(data: dict) -> list[dict]:
    """Parse trip data from Uber's internal API/GraphQL responses."""
    rides = []

    # Recursively search for arrays of objects that look like trips
    trip_list = _find_trip_list(data)

    if not trip_list:
        return rides

    for item in trip_list:
        if not isinstance(item, dict):
            continue

        trip_id = item.get("uuid") or item.get("id") or item.get("tripUUID") or item.get("request_id")
        if not trip_id:
            continue

        # Parse timestamps
        ts = (item.get("requestTime") or item.get("startTime") or
              item.get("request_time") or item.get("beginTripTime") or
              datetime.utcnow().isoformat())
        if isinstance(ts, (int, float)):
            # Unix timestamp (seconds or milliseconds)
            if ts > 1e12:
                ts = ts / 1000
            ts = datetime.utcfromtimestamp(ts).isoformat()

        # Parse addresses
        pickup = (item.get("pickupAddress") or item.get("pickup", {}).get("address") or
                  item.get("startCity", {}).get("name") if isinstance(item.get("startCity"), dict) else
                  item.get("startCity") or "Unknown")
        dropoff = (item.get("dropoffAddress") or item.get("dropoff", {}).get("address") or
                   item.get("destination") or "Unknown")

        # Parse coordinates
        pickup_lat = item.get("pickupLat") or (item.get("pickup", {}).get("latitude") if isinstance(item.get("pickup"), dict) else None)
        pickup_lng = item.get("pickupLng") or (item.get("pickup", {}).get("longitude") if isinstance(item.get("pickup"), dict) else None)
        dropoff_lat = item.get("dropoffLat") or (item.get("dropoff", {}).get("latitude") if isinstance(item.get("dropoff"), dict) else None)
        dropoff_lng = item.get("dropoffLng") or (item.get("dropoff", {}).get("longitude") if isinstance(item.get("dropoff"), dict) else None)

        # Parse price
        price = item.get("fare") or item.get("clientFare") or item.get("totalFare")
        if isinstance(price, dict):
            price = price.get("total") or price.get("amount")
        if isinstance(price, str):
            nums = re.findall(r'[\d,.]+', price)
            price = float(nums[0].replace(",", "")) if nums else None

        # Parse ride type
        ride_type = item.get("vehicleViewName") or item.get("productType") or item.get("product_id") or "UberX"

        # Parse duration
        duration = item.get("duration") or item.get("tripDuration")
        if duration and duration > 300:  # Likely in seconds
            duration = round(duration / 60, 1)

        # Parse distance
        distance = item.get("distance") or item.get("tripDistance")
        if distance and isinstance(distance, (int, float)):
            if distance > 100:  # Likely in meters
                distance = round(distance / 1609.34, 1)

        rides.append({
            "external_ride_id": str(trip_id),
            "source_platform": "uber",
            "pickup_address": str(pickup),
            "dropoff_address": str(dropoff),
            "pickup_lat": pickup_lat,
            "pickup_lng": pickup_lng,
            "dropoff_lat": dropoff_lat,
            "dropoff_lng": dropoff_lng,
            "request_timestamp": ts,
            "ride_type": str(ride_type),
            "price": price if isinstance(price, (int, float)) else None,
            "duration_minutes": duration,
            "distance_miles": distance,
            "raw_payload": item,
        })

    log.info("Parsed %d rides from API response", len(rides))
    return rides


async def _scrape_trip_detail(page: Page, trip_id: str) -> dict | None:
    """Scrape details of a single trip."""
    try:
        url = f"https://riders.uber.com/trips/{trip_id}"
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)

        detail = await page.evaluate("""() => {
            const body = document.body.innerText || '';

            const priceMatch = body.match(/[₹$€£]\\s*[\\d,.]+/);
            const dateMatch = body.match(/(\\w+\\s+\\d{1,2},?\\s+\\d{4}|\\d{1,2}[\\/-]\\d{1,2}[\\/-]\\d{2,4})/);
            const timeMatch = body.match(/(\\d{1,2}:\\d{2}\\s*(?:AM|PM|am|pm))/);
            const durationMatch = body.match(/(\\d+)\\s*min/);
            const distanceMatch = body.match(/(\\d+\\.?\\d*)\\s*(?:km|mi|miles)/i);

            // Look for addresses
            const lines = body.split('\\n').map(l => l.trim()).filter(l => l);
            let pickup = null, dropoff = null;
            for (let i = 0; i < lines.length; i++) {
                const lower = lines[i].toLowerCase();
                if ((lower.includes('pickup') || lower.includes('pick up') || lower.includes('pick-up')) && i + 1 < lines.length) {
                    pickup = lines[i + 1];
                } else if ((lower.includes('dropoff') || lower.includes('drop off') || lower.includes('drop-off') || lower.includes('destination')) && i + 1 < lines.length) {
                    dropoff = lines[i + 1];
                }
            }

            return {
                raw_text: body.substring(0, 3000),
                price: priceMatch ? priceMatch[0] : null,
                date: dateMatch ? dateMatch[0] : null,
                time: timeMatch ? timeMatch[0] : null,
                duration_minutes: durationMatch ? parseInt(durationMatch[1]) : null,
                distance_text: distanceMatch ? distanceMatch[0] : null,
                pickup: pickup,
                dropoff: dropoff,
            };
        }""")

        if not detail:
            return None

        # Parse price
        price = None
        if detail.get("price"):
            nums = re.findall(r'[\d,.]+', detail["price"])
            if nums:
                price = float(nums[0].replace(",", ""))

        # Parse timestamp
        ts = datetime.utcnow().isoformat()
        if detail.get("date"):
            try:
                date_str = detail["date"]
                if detail.get("time"):
                    date_str += " " + detail["time"]
                for fmt in ["%B %d, %Y %I:%M %p", "%B %d %Y %I:%M %p", "%b %d, %Y %I:%M %p",
                           "%B %d, %Y", "%m/%d/%Y", "%d/%m/%Y"]:
                    try:
                        ts = datetime.strptime(date_str.strip(), fmt).isoformat()
                        break
                    except ValueError:
                        continue
            except Exception:
                pass

        # Extract ride type
        raw = detail.get("raw_text", "")
        ride_type = "UberX"
        for rt in ["UberXL", "Uber Go", "UberGo", "Uber Auto", "UberAuto", "Premier",
                    "Uber Black", "UberPool", "Uber Moto", "Uber Intercity", "UberX"]:
            if rt.lower() in raw.lower():
                ride_type = rt
                break

        # Parse distance
        dist = None
        if detail.get("distance_text"):
            nums = re.findall(r'[\d.]+', detail["distance_text"])
            if nums:
                val = float(nums[0])
                dist = round(val * 0.621371, 1) if "km" in detail["distance_text"].lower() else round(val, 1)

        return {
            "external_ride_id": trip_id,
            "source_platform": "uber",
            "pickup_address": detail.get("pickup") or "Unknown pickup",
            "dropoff_address": detail.get("dropoff") or "Unknown dropoff",
            "request_timestamp": ts,
            "ride_type": ride_type,
            "price": price,
            "duration_minutes": detail.get("duration_minutes"),
            "distance_miles": dist,
            "raw_payload": {"trip_id": trip_id, "scraped": detail},
        }

    except Exception as e:
        log.warning("Failed to scrape trip %s: %s", trip_id, e)
        return None


# ── Live Estimates Scraping ──────────────────────────────────────────────────

async def scrape_live_estimates(
    pickup_lat: float, pickup_lng: float,
    dropoff_lat: float, dropoff_lng: float,
) -> dict:
    """Scrape live estimates from m.uber.com."""
    result = {
        "live": False,
        "estimates": [],
        "error": None,
    }

    ctx = await _ensure_browser()
    page = await ctx.new_page()

    captured_responses = []

    async def handle_response(response):
        url = response.url
        if "estimates" in url or "products" in url or "fare" in url:
            try:
                body = await response.json()
                captured_responses.append({"url": url, "data": body})
            except Exception:
                pass

    page.on("response", handle_response)

    try:
        pickup_obj = json.dumps({"latitude": pickup_lat, "longitude": pickup_lng})
        drop_obj = json.dumps({"latitude": dropoff_lat, "longitude": dropoff_lng})
        uber_url = f"https://m.uber.com/looking?pickup={pickup_obj}&drop[0]={drop_obj}"

        await page.goto(uber_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(8000)

        # Extract from DOM
        estimates = await page.evaluate("""() => {
            const results = [];
            const body = document.body.innerText || '';
            const priceMatches = body.match(/[₹$€£]\\s*[\\d,.]+/g);
            if (priceMatches) {
                results.push({type: 'prices_found', prices: priceMatches});
            }
            return results;
        }""")

        if captured_responses:
            result["live"] = True
            result["network_data"] = captured_responses
            result["estimates"] = _extract_live_estimate_candidates(captured_responses)
            if result["estimates"]:
                result["best_estimate"] = _select_best_live_estimate(result["estimates"])

        if estimates:
            result["dom_data"] = estimates
            if not result["live"]:
                result["live"] = True
            if not result.get("estimates"):
                result["estimates"] = _extract_dom_estimate_candidates(estimates)
                if result["estimates"]:
                    result["best_estimate"] = _select_best_live_estimate(result["estimates"])

        await page.close()
        return result

    except Exception as e:
        log.error("Live estimate scraping failed: %s", e)
        result["error"] = str(e)
        try:
            await page.close()
        except Exception:
            pass
        return result


def _extract_live_estimate_candidates(captured_responses: list[dict]) -> list[dict]:
    candidates: list[dict] = []
    seen: set[tuple] = set()

    for item in captured_responses:
        payload = item.get("data")
        if payload is None:
            continue
        for candidate in _walk_estimate_nodes(payload):
            key = (
                candidate.get("ride_type") or "",
                candidate.get("price_text") or "",
                candidate.get("eta_minutes"),
            )
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)

    return candidates


def _walk_estimate_nodes(node) -> list[dict]:
    matches: list[dict] = []

    if isinstance(node, dict):
        candidate = _candidate_from_dict(node)
        if candidate:
            matches.append(candidate)

        for value in node.values():
            matches.extend(_walk_estimate_nodes(value))
        return matches

    if isinstance(node, list):
        for value in node:
            matches.extend(_walk_estimate_nodes(value))

    return matches


def _candidate_from_dict(node: dict) -> dict | None:
    name = _first_string(
        node,
        [
            "display_name",
            "displayName",
            "product_display_name",
            "productName",
            "product_name",
            "vehicleViewDisplayName",
            "name",
            "title",
        ],
    )
    price_text = _first_price_text(
        node,
        [
            "estimate",
            "priceString",
            "fareString",
            "fare_display",
            "formatted_total_fare",
            "formattedTotalFare",
            "upfront_fare",
            "price",
        ],
    )

    low_estimate = _first_number(
        node,
        ["lowEstimate", "low_estimate", "fareLower", "minimum_fare", "min_price"],
    )
    high_estimate = _first_number(
        node,
        ["highEstimate", "high_estimate", "fareUpper", "maximum_fare", "max_price"],
    )
    eta_minutes = _extract_eta_minutes(node)

    if not any([price_text, low_estimate, high_estimate, eta_minutes]):
        return None

    if not name:
        name = "Uber"

    if not price_text and low_estimate is not None and high_estimate is not None:
        price_text = f"₹{int(round(low_estimate))}-₹{int(round(high_estimate))}"
    elif not price_text and high_estimate is not None:
        price_text = f"₹{int(round(high_estimate))}"

    return {
        "ride_type": name,
        "price_text": price_text,
        "low_estimate": low_estimate,
        "high_estimate": high_estimate,
        "eta_minutes": eta_minutes,
    }


def _extract_dom_estimate_candidates(dom_estimates: list[dict]) -> list[dict]:
    candidates: list[dict] = []
    for entry in dom_estimates:
        if not isinstance(entry, dict):
            continue
        prices = entry.get("prices")
        if not isinstance(prices, list):
            continue
        for price in prices:
            if not isinstance(price, str):
                continue
            candidates.append(
                {
                    "ride_type": "Uber",
                    "price_text": price.strip(),
                    "low_estimate": None,
                    "high_estimate": _price_to_number(price),
                    "eta_minutes": None,
                }
            )
    return candidates


def _select_best_live_estimate(estimates: list[dict]) -> dict | None:
    usable = [estimate for estimate in estimates if isinstance(estimate, dict)]
    if not usable:
        return None

    def score(item: dict) -> tuple[int, float, float]:
        has_name = 0 if (item.get("ride_type") or "").lower() in {"uber", ""} else 1
        eta = item.get("eta_minutes")
        high = item.get("high_estimate")
        eta_score = float(eta) if isinstance(eta, (int, float)) else 9999.0
        high_score = float(high) if isinstance(high, (int, float)) else 9999.0
        return (-has_name, eta_score, high_score)

    return sorted(usable, key=score)[0]


def _first_string(node: dict, keys: list[str]) -> str | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_price_text(node: dict, keys: list[str]) -> str | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str):
            match = re.search(r"[₹$€£]\s*[\d,]+(?:\.\d+)?(?:\s*[-–]\s*[₹$€£]?\s*[\d,]+(?:\.\d+)?)?", value)
            if match:
                return match.group(0).replace(" ", "")
        if isinstance(value, (int, float)):
            return f"₹{int(round(float(value)))}"
    return None


def _first_number(node: dict, keys: list[str]) -> float | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            parsed = _price_to_number(value)
            if parsed is not None:
                return parsed
    return None


def _extract_eta_minutes(node: dict) -> int | None:
    keys = [
        "pickup_estimate",
        "pickupEstimate",
        "pickup_estimate_minutes",
        "eta_minutes",
        "eta",
        "waitTime",
        "wait_time",
    ]
    for key in keys:
        value = node.get(key)
        if isinstance(value, (int, float)):
            numeric = float(value)
            if "eta" == key and numeric > 120:
                return int(round(numeric / 60.0))
            return int(round(numeric))
        if isinstance(value, str):
            match = re.search(r"(\d+)", value)
            if match:
                return int(match.group(1))
    return None


def _price_to_number(value: str) -> float | None:
    text = re.sub(r"[^0-9.]", "", str(value))
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ── Deep Link Builder ────────────────────────────────────────────────────────

def build_deeplink(
    pickup_address: str = None,
    dropoff_address: str = None,
    pickup_lat: float = None,
    pickup_lng: float = None,
    dropoff_lat: float = None,
    dropoff_lng: float = None,
) -> str:
    """Build an Uber deep link for ride handoff."""
    base = "https://m.uber.com/looking"
    params = {}

    pickup_obj = {}
    if pickup_address:
        pickup_obj["addressLine1"] = pickup_address
    if pickup_lat and pickup_lng:
        pickup_obj["latitude"] = pickup_lat
        pickup_obj["longitude"] = pickup_lng
    if pickup_obj:
        params["pickup"] = json.dumps(pickup_obj)

    drop_obj = {}
    if dropoff_address:
        drop_obj["addressLine1"] = dropoff_address
    if dropoff_lat and dropoff_lng:
        drop_obj["latitude"] = dropoff_lat
        drop_obj["longitude"] = dropoff_lng
    if drop_obj:
        params["drop[0]"] = json.dumps(drop_obj)

    if params:
        return f"{base}?{urlencode(params)}"
    return base


# ── Connection Status ────────────────────────────────────────────────────────

def get_connection_status() -> dict:
    connected = db.get_setting("uber_connected") == "true"
    synced = db.get_setting("uber_history_synced") == "true"
    login_status = db.get_setting("login_status") or "not_started"
    login_time = db.get_setting("login_time")
    has_cookies = os.path.exists(COOKIES_FILE)

    return {
        "connected": connected and has_cookies,
        "history_synced": synced,
        "login_status": login_status,
        "login_time": login_time,
        "last_sync": db.get_setting("last_sync_time"),
        "has_session": has_cookies,
    }
