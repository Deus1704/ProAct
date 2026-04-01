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
SCRAPE_DEBUG_DIR = os.path.join(os.path.dirname(__file__), "scrape_debug")
UBER_HISTORY_DEBUG_PATH = os.path.join(SCRAPE_DEBUG_DIR, "uber_history_last.json")

# Global browser state
_browser: Browser | None = None
_context: BrowserContext | None = None
_login_page: Page | None = None
_popup_page: Page | None = None
_scrape_page: Page | None = None  # Persistent page kept alive for background scraping
_playwright = None

KNOWN_RIDE_TYPES = [
    "UberXL",
    "Uber Black",
    "Uber Intercity",
    "Uber Auto",
    "UberAuto",
    "Uber Moto",
    "UberPool",
    "Premier",
    "Uber Go",
    "UberGo",
    "UberX",
]


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


async def _ensure_scrape_page(ctx: BrowserContext) -> Page:
    """Return a live page for history scraping, recreating it if needed."""
    global _scrape_page
    if _scrape_page and not _scrape_page.is_closed():
        return _scrape_page
    _scrape_page = await ctx.new_page()
    log.info("Created persistent Uber scrape page")
    return _scrape_page


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
    ctx = await _ensure_browser()
    page = await _ensure_scrape_page(ctx)

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
                    new_token = headers[name]
                    if new_token != csrf_token:
                        csrf_token = new_token
                        log.info("Captured CSRF from request header: %s", csrf_token[:30])

        page.on("request", capture_csrf)

        try:
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
            skipped_incomplete = 0
            deleted_low_fidelity = 0
            scrape_report: list[dict] = []
            for trip in all_trips:
                summary_ride = _parse_activity_trip(trip)
                if not summary_ride:
                    continue

                if _should_skip_summary_trip(summary_ride):
                    debug_entry = _build_trip_debug_entry(
                        trip,
                        summary_ride,
                        None,
                        summary_ride,
                        ["skipped_status"],
                    )
                    debug_entry["status"] = "skipped_status"
                    scrape_report.append(debug_entry)
                    log.info(
                        "Skipping Uber trip %s before detail scrape because status=%s",
                        summary_ride["external_ride_id"],
                        summary_ride.get("trip_status"),
                    )
                    continue

                page = await _ensure_scrape_page(ctx)
                detail_ride = await _scrape_trip_detail(ctx, page, summary_ride["external_ride_id"])
                ride = _build_verified_trip_record(summary_ride, detail_ride)
                missing_fields = _missing_required_history_fields(ride)
                debug_entry = _build_trip_debug_entry(trip, summary_ride, detail_ride, ride, missing_fields)
                scrape_report.append(debug_entry)
                _log_trip_debug_entry(debug_entry)

                if missing_fields:
                    skipped_incomplete += 1
                    log.info("Skipping incomplete Uber trip %s because missing fields: %s", summary_ride["external_ride_id"], ", ".join(missing_fields))
                    continue

                db.insert_ride(ride)
                synced += 1

            # Page intentionally NOT closed — kept alive for background price/ETA polling.
            log.info("Uber scrape page kept alive. Tabs open: %d", len(ctx.pages))

            if synced > 0:
                deleted_low_fidelity = db.delete_low_fidelity_uber_rides()
                db.set_setting("last_sync_time", datetime.utcnow().isoformat())
                db.set_setting("uber_history_synced", "true")

            debug_report_path = _write_history_debug_report(scrape_report)
            return {
                "synced": synced,
                "total_found": len(all_trips),
                "skipped_incomplete": skipped_incomplete,
                "deleted_low_fidelity": deleted_low_fidelity,
                "debug_report_path": debug_report_path,
            }
        finally:
            try:
                page.remove_listener("request", capture_csrf)
            except Exception:
                pass

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
        lowered_description = description.lower()
        canceled = "cancel" in lowered_description
        unfulfilled = "unfulfilled" in lowered_description
        price_match = re.search(r'[₹$€£]\s*([\d,.]+)', description)
        if price_match and not (canceled or unfulfilled):
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

        ride_type = _extract_ride_type(
            title,
            description,
            trip.get("accessibilityLabel"),
            trip.get("trackingLabel"),
        )

        return {
            "external_ride_id": uuid,
            "source_platform": "uber",
            "pickup_address": None,
            "dropoff_address": title or None,
            "pickup_lat": pickup_lat,
            "pickup_lng": pickup_lng,
            "request_timestamp": ts,
            "ride_type": ride_type,
            "is_canceled": canceled,
            "trip_status": _extract_trip_status(description),
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
        ride_type = _extract_ride_type(
            item.get("vehicleViewName"),
            item.get("productType"),
            item.get("product_id"),
            item.get("displayName"),
        )

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
            "ride_type": str(ride_type) if ride_type else None,
            "price": price if isinstance(price, (int, float)) else None,
            "duration_minutes": duration,
            "distance_miles": distance,
            "raw_payload": item,
        })

    log.info("Parsed %d rides from API response", len(rides))
    return rides


async def _scrape_trip_detail(ctx: BrowserContext, page: Page, trip_id: str, attempt: int = 1) -> dict | None:
    """Scrape details of a single trip."""
    global _scrape_page
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
                raw_text: body.substring(0, 8000),
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
        ride_type = _extract_ride_type(raw)
        route_pickup, route_dropoff = _extract_route_stop_addresses(raw)
        is_canceled = "cancel" in raw.lower()

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
            "pickup_address": _clean_scraped_value(detail.get("pickup")) or route_pickup,
            "dropoff_address": _clean_scraped_value(detail.get("dropoff")) or route_dropoff,
            "request_timestamp": ts,
            "ride_type": ride_type,
            "is_canceled": is_canceled,
            "price": price,
            "duration_minutes": detail.get("duration_minutes"),
            "distance_miles": dist,
            "raw_payload": {"trip_id": trip_id, "scraped": detail},
        }

    except Exception as e:
        if attempt == 1 and _is_closed_page_error(e):
            log.warning("Retrying trip %s on a fresh scrape page after browser/page closure: %s", trip_id, e)
            try:
                if page and not page.is_closed():
                    await page.close()
            except Exception:
                pass
            _scrape_page = None
            fresh_page = await _ensure_scrape_page(ctx)
            return await _scrape_trip_detail(ctx, fresh_page, trip_id, attempt=2)
        log.warning("Failed to scrape trip %s: %s", trip_id, e)
        return None


def _clean_scraped_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in {"unknown", "unknown pickup", "unknown dropoff", "destination", "pickup", "dropoff"}:
        return None
    return text


def _extract_ride_type(*values: object) -> str | None:
    haystacks = [str(value) for value in values if value]
    for ride_type in KNOWN_RIDE_TYPES:
        needle = ride_type.lower()
        for haystack in haystacks:
            if needle in haystack.lower():
                return ride_type
    return None


def _is_closed_page_error(error: Exception) -> bool:
    message = str(error).lower()
    return any(
        token in message
        for token in [
            "target page, context or browser has been closed",
            "connection closed while reading from the driver",
            "the handler is closed",
            "transport closed",
        ]
    )


def _is_time_line(text: str) -> bool:
    return bool(re.fullmatch(r"\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)", text.strip()))


def _looks_like_address_line(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned or _is_time_line(cleaned):
        return False
    lowered = cleaned.lower()
    blocked = {
        "uber",
        "your trip",
        "trip rating",
        "trip details",
        "route",
        "cash",
        "view receipt",
        "resend receipt",
        "request invoice",
        "get help",
        "cancelled",
        "canceled",
    }
    if lowered in blocked:
        return False
    return "," in cleaned or bool(re.search(r"\d", cleaned))


def _extract_route_stop_addresses(raw_text: object) -> tuple[str | None, str | None]:
    if not raw_text:
        return None, None
    lines = [line.strip() for line in str(raw_text).splitlines() if line.strip()]
    route_idx = None
    for index, line in enumerate(lines):
        if line.lower() == "route":
            route_idx = index
            break
    if route_idx is None:
        return None, None

    route_lines = lines[route_idx + 1 : route_idx + 9]
    address_candidates: list[str] = []
    for index, line in enumerate(route_lines):
        if not _looks_like_address_line(line):
            continue
        next_line = route_lines[index + 1] if index + 1 < len(route_lines) else ""
        if _is_time_line(next_line):
            address_candidates.append(line)
            continue
        if index > 0 and _is_time_line(route_lines[index - 1]):
            address_candidates.append(line)

    deduped: list[str] = []
    for candidate in address_candidates:
        cleaned = _clean_scraped_value(candidate)
        if cleaned and cleaned not in deduped:
            deduped.append(cleaned)

    if len(deduped) >= 2:
        return deduped[0], deduped[1]
    if len(deduped) == 1:
        return deduped[0], None
    return None, None


def _merge_trip_records(summary_ride: dict | None, detail_ride: dict | None) -> dict | None:
    if not summary_ride:
        return detail_ride
    if not detail_ride:
        return summary_ride

    merged = dict(summary_ride)
    for key, value in detail_ride.items():
        if key == "raw_payload":
            merged["raw_payload"] = {
                "activity": summary_ride.get("raw_payload"),
                "detail": detail_ride.get("raw_payload"),
            }
            continue
        if value not in (None, "", []):
            merged[key] = value
    return merged


def _build_verified_trip_record(summary_ride: dict | None, detail_ride: dict | None) -> dict | None:
    if not summary_ride or not detail_ride:
        return None
    merged = _merge_trip_records(summary_ride, detail_ride)
    if not merged:
        return None
    verified = dict(merged)
    verified["pickup_address"] = _clean_scraped_value(detail_ride.get("pickup_address"))
    verified["dropoff_address"] = _clean_scraped_value(detail_ride.get("dropoff_address"))
    verified["ride_type"] = _clean_scraped_value(detail_ride.get("ride_type"))
    verified["price"] = detail_ride.get("price")
    verified["duration_minutes"] = detail_ride.get("duration_minutes")
    verified["distance_miles"] = detail_ride.get("distance_miles")
    return verified


def _extract_trip_status(description: object) -> str:
    text = str(description or "").strip().lower()
    if "cancel" in text:
        return "canceled"
    if "unfulfilled" in text:
        return "unfulfilled"
    if "completed" in text:
        return "completed"
    if text:
        return "completed"
    return "unknown"


def _should_skip_summary_trip(summary_ride: dict | None) -> bool:
    if not summary_ride:
        return True
    return summary_ride.get("trip_status") in {"canceled", "unfulfilled"}


def _ride_has_actual_history_fields(ride: dict | None) -> bool:
    if not ride:
        return False
    return not _missing_required_history_fields(ride)


def _missing_required_history_fields(ride: dict | None) -> list[str]:
    if not ride:
        return ["pickup_address", "dropoff_address", "price"]
    missing: list[str] = []
    if not _clean_scraped_value(ride.get("pickup_address")):
        missing.append("pickup_address")
    if not _clean_scraped_value(ride.get("dropoff_address")):
        missing.append("dropoff_address")
    if ride.get("price") is None:
        missing.append("price")
    return missing


def _truncate_debug_text(value: object, limit: int = 400) -> str | None:
    cleaned = _clean_scraped_value(value)
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def _build_trip_debug_entry(
    trip: dict,
    summary_ride: dict | None,
    detail_ride: dict | None,
    merged_ride: dict | None,
    missing_fields: list[str],
) -> dict:
    detail_payload = ((detail_ride or {}).get("raw_payload") or {}).get("scraped", {})
    return {
        "trip_id": (summary_ride or {}).get("external_ride_id") or trip.get("uuid"),
        "status": "ready" if not missing_fields else "skipped_incomplete",
        "missing_fields": missing_fields,
        "summary": {
            "title": _truncate_debug_text(trip.get("title")),
            "subtitle": _truncate_debug_text(trip.get("subtitle")),
            "description": _truncate_debug_text(trip.get("description")),
            "card_url": trip.get("cardURL"),
            "ride_type_guess": (summary_ride or {}).get("ride_type"),
            "dropoff_guess": (summary_ride or {}).get("dropoff_address"),
            "pickup_lat": (summary_ride or {}).get("pickup_lat"),
            "pickup_lng": (summary_ride or {}).get("pickup_lng"),
        },
        "detail": {
            "page_pickup": detail_ride.get("pickup_address") if detail_ride else None,
            "page_dropoff": detail_ride.get("dropoff_address") if detail_ride else None,
            "page_ride_type": detail_ride.get("ride_type") if detail_ride else None,
            "page_price": detail_ride.get("price") if detail_ride else None,
            "page_duration_minutes": detail_ride.get("duration_minutes") if detail_ride else None,
            "page_distance_miles": detail_ride.get("distance_miles") if detail_ride else None,
            "raw_pickup_label": detail_payload.get("pickup"),
            "raw_dropoff_label": detail_payload.get("dropoff"),
            "raw_price_text": detail_payload.get("price"),
            "raw_distance_text": detail_payload.get("distance_text"),
            "raw_text_excerpt": _truncate_debug_text(detail_payload.get("raw_text"), limit=1200),
        },
        "merged": {
            "pickup_address": (merged_ride or {}).get("pickup_address"),
            "dropoff_address": (merged_ride or {}).get("dropoff_address"),
            "ride_type": (merged_ride or {}).get("ride_type"),
            "request_timestamp": (merged_ride or {}).get("request_timestamp"),
            "price": (merged_ride or {}).get("price"),
        },
    }


def _log_trip_debug_entry(debug_entry: dict) -> None:
    trip_id = debug_entry.get("trip_id")
    summary = debug_entry.get("summary", {})
    detail = debug_entry.get("detail", {})
    merged = debug_entry.get("merged", {})
    missing_fields = debug_entry.get("missing_fields", [])
    log.info(
        "Uber trip %s summary: title=%r subtitle=%r description=%r ride_type_guess=%r",
        trip_id,
        summary.get("title"),
        summary.get("subtitle"),
        summary.get("description"),
        summary.get("ride_type_guess"),
    )
    log.info(
        "Uber trip %s detail: pickup=%r dropoff=%r ride_type=%r raw_pickup=%r raw_dropoff=%r",
        trip_id,
        detail.get("page_pickup"),
        detail.get("page_dropoff"),
        detail.get("page_ride_type"),
        detail.get("raw_pickup_label"),
        detail.get("raw_dropoff_label"),
    )
    if missing_fields:
        if "pickup_address" in missing_fields and detail.get("raw_text_excerpt") and "cancel" in str(detail.get("raw_text_excerpt")).lower():
            log.info("Uber trip %s note: this looks like a canceled trip detail page and Uber is not exposing route stops there", trip_id)
        log.info(
            "Uber trip %s missing required fields: %s | raw_text_excerpt=%r",
            trip_id,
            ", ".join(missing_fields),
            detail.get("raw_text_excerpt"),
        )
    else:
        log.info(
            "Uber trip %s merged result: pickup=%r dropoff=%r ride_type=%r price=%r",
            trip_id,
            merged.get("pickup_address"),
            merged.get("dropoff_address"),
            merged.get("ride_type"),
            merged.get("price"),
        )


def _write_history_debug_report(scrape_report: list[dict]) -> str:
    os.makedirs(SCRAPE_DEBUG_DIR, exist_ok=True)
    payload = {
        "generated_at": datetime.utcnow().isoformat(),
        "trip_count": len(scrape_report),
        "entries": scrape_report,
    }
    with open(UBER_HISTORY_DEBUG_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    log.info("Wrote Uber history scrape report to %s", UBER_HISTORY_DEBUG_PATH)
    return UBER_HISTORY_DEBUG_PATH


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

        # Extract product cards and full text from DOM
        dom_snapshot = await page.evaluate("""() => {
            const body = document.body.innerText || '';
            const nodes = Array.from(document.querySelectorAll('div, li, button, section, article'));
            const candidates = [];
            const seen = new Set();
            for (const node of nodes) {
                const text = (node.innerText || '').trim();
                if (!text || text.length < 5 || text.length > 300) continue;
                const normalized = text.replace(/\\s+/g, ' ').trim();
                const lower = normalized.toLowerCase();
                const hasRideName = ['uberxl', 'uber x', 'uberx', 'uber go', 'ubergo', 'uber auto', 'uberauto', 'premier', 'uber moto', 'uber intercity', 'uber black', 'uber pool', 'uberpool']
                    .some((name) => lower.includes(name));
                const hasPrice = /[₹$€£]\\s*[\\d,.]+/.test(normalized);
                const hasEta = /\\b\\d+\\s*(?:min|mins|minute|minutes)\\b/i.test(normalized);
                if (!hasRideName && !hasPrice) continue;
                if (seen.has(normalized)) continue;
                seen.add(normalized);
                candidates.push({text: normalized});
            }
            return {raw_text: body.substring(0, 12000), candidates};
        }""")

        if captured_responses:
            result["live"] = True
            result["network_data"] = captured_responses
            result["estimates"] = _extract_live_estimate_candidates(captured_responses)
            if result["estimates"]:
                result["best_estimate"] = _select_best_live_estimate(result["estimates"])

        if dom_snapshot:
            result["dom_data"] = dom_snapshot
            if not result["live"]:
                result["live"] = True
            if not result.get("estimates"):
                result["estimates"] = _extract_dom_estimate_candidates(dom_snapshot)
                if result["estimates"]:
                    result["best_estimate"] = _select_best_live_estimate(result["estimates"])
            else:
                dom_estimates = _extract_dom_estimate_candidates(dom_snapshot)
                result["estimates"] = _merge_live_estimates(result["estimates"], dom_estimates)
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
    if isinstance(dom_estimates, dict):
        for entry in dom_estimates.get("candidates") or []:
            if not isinstance(entry, dict):
                continue
            text = entry.get("text")
            if not isinstance(text, str):
                continue
            parsed = _parse_dom_product_candidate(text)
            if parsed:
                candidates.append(parsed)
        if candidates:
            return candidates
        raw_text = dom_estimates.get("raw_text")
        if isinstance(raw_text, str) and raw_text.strip():
            candidates.extend(_parse_dom_candidates_from_text(raw_text))
    return candidates


def _merge_live_estimates(primary: list[dict], secondary: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[str, str, int | None]] = set()
    for source in (primary, secondary):
        for item in source:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("ride_type") or "").strip().lower(),
                str(item.get("price_text") or "").strip(),
                item.get("eta_minutes"),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _parse_dom_candidates_from_text(raw_text: str) -> list[dict]:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    matches: list[dict] = []
    for index, line in enumerate(lines):
        ride_type = _extract_ride_type(line)
        if not ride_type:
            continue
        window = " ".join(lines[index : index + 4])
        candidate = _parse_dom_product_candidate(window)
        if candidate:
            matches.append(candidate)
    return matches


def _parse_dom_product_candidate(text: str) -> dict | None:
    ride_type = _extract_ride_type(text)
    price_match = re.search(r"[₹$€£]\s*[\d,.]+(?:\s*[-–]\s*[₹$€£]?\s*[\d,.]+)?", text)
    eta_match = re.search(r"\b(\d+)\s*(?:min|mins|minute|minutes)\b", text, re.IGNORECASE)
    if not ride_type and not price_match:
        return None
    price_text = price_match.group(0).replace(" ", "") if price_match else None
    high_estimate = _price_to_number(price_text) if price_text else None
    eta_minutes = int(eta_match.group(1)) if eta_match else None
    return {
        "ride_type": ride_type,
        "price_text": price_text,
        "low_estimate": None,
        "high_estimate": high_estimate,
        "eta_minutes": eta_minutes,
    }


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
