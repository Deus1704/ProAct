"""Swiggy integration via browser-assisted login and history scraping."""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import logging
import os
import re
from difflib import SequenceMatcher
from datetime import datetime, timezone
from typing import Any

from . import database as db
log = logging.getLogger(__name__)

SESSION_DIR = os.path.join(os.path.dirname(__file__), "browser_session")

PROVIDER_CONFIG = {
    "swiggy": {
        "label": "Swiggy",
        "login_url": "https://www.swiggy.com/",
        "orders_url": "https://www.swiggy.com/my-account/orders",
        "login_urls": [
            "https://www.swiggy.com/",
            "https://www.swiggy.com/auth",
        ],
        "orders_urls": [
            "https://www.swiggy.com/my-account/orders",
            "https://www.swiggy.com/my-account",
        ],
    },
}

_browser: Any | None = None
_playwright = None
_contexts: dict[str, Any] = {}
_pages: dict[str, Any] = {}
_active_provider: str | None = None


def _is_http2_protocol_error(exc: Exception) -> bool:
    return "ERR_HTTP2_PROTOCOL_ERROR" in str(exc)


async def _goto_with_fallback(page, provider: str, target: str) -> str:
    cfg = PROVIDER_CONFIG[provider]
    if target == "login":
        urls = cfg.get("login_urls") or [cfg["login_url"]]
    elif target == "orders":
        urls = cfg.get("orders_urls") or [cfg["orders_url"]]
    else:
        raise ValueError(f"Unsupported navigation target: {target}")

    last_exc: Exception | None = None
    for idx, url in enumerate(urls):
        try:
            wait_until = "domcontentloaded" if idx == 0 else "commit"
            await page.goto(url, wait_until=wait_until, timeout=45000)
            return url
        except Exception as exc:
            last_exc = exc
            if _is_http2_protocol_error(exc):
                log.warning(
                    "HTTP/2 protocol error while opening %s %s at %s; trying fallback.",
                    provider,
                    target,
                    url,
                )
            else:
                log.warning(
                    "Navigation failed while opening %s %s at %s: %s",
                    provider,
                    target,
                    url,
                    exc,
                )

            if idx < len(urls) - 1:
                await page.wait_for_timeout(700)
                continue
            break

    if last_exc:
        raise last_exc
    raise RuntimeError(f"Failed to navigate to {provider} {target} page")


def _storage_state_path(provider: str) -> str:
    return os.path.join(SESSION_DIR, f"{provider}_storage_state.json")


def _provider_key(provider: str) -> str:
    value = (provider or "").strip().lower()
    if value not in PROVIDER_CONFIG:
        supported = ", ".join(sorted(PROVIDER_CONFIG.keys()))
        raise ValueError(f"Unsupported provider '{provider}'. Supported providers: {supported}")
    return value


def _setting_key(provider: str, suffix: str) -> str:
    return f"{provider}_{suffix}"


async def _ensure_context(provider: str):
    global _browser, _playwright

    if provider in _contexts:
        return _contexts[provider]

    os.makedirs(SESSION_DIR, exist_ok=True)

    if not _browser:
        try:
            playwright_async = importlib.import_module("playwright.async_api")
            async_playwright = getattr(playwright_async, "async_playwright")
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Playwright is not installed. Install with 'pip install playwright' and run 'playwright install chromium'."
            ) from exc
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-http2",
                "--disable-quic",
            ],
        )

    state_file = _storage_state_path(provider)
    if os.path.exists(state_file):
        context = await _browser.new_context(
            storage_state=state_file,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            is_mobile=False,
            has_touch=False,
            ignore_https_errors=True,
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
    else:
        context = await _browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            is_mobile=False,
            has_touch=False,
            ignore_https_errors=True,
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )

    _contexts[provider] = context
    return context


async def _save_storage_state(provider: str):
    context = _contexts.get(provider)
    if not context:
        return
    await context.storage_state(path=_storage_state_path(provider))


async def close_browser():
    global _browser, _playwright, _active_provider

    for provider, page in list(_pages.items()):
        try:
            await _save_storage_state(provider)
            await page.close()
        except Exception:
            pass

    _pages.clear()

    for provider, context in list(_contexts.items()):
        try:
            await _save_storage_state(provider)
            await context.close()
        except Exception:
            pass

    _contexts.clear()

    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None

    _active_provider = None


def get_connection_status(provider: str | None = None) -> dict:
    if provider:
        p = _provider_key(provider)
        return {
            "provider": p,
            "label": PROVIDER_CONFIG[p]["label"],
            "connected": db.get_setting(_setting_key(p, "connected")) == "true",
            "history_synced": db.get_setting(_setting_key(p, "history_synced")) == "true",
            "last_sync_time": db.get_setting(_setting_key(p, "last_sync_time")),
            "login_time": db.get_setting(_setting_key(p, "login_time")),
        }

    providers = {name: get_connection_status(name) for name in PROVIDER_CONFIG.keys()}
    return {
        "providers": providers,
        "active_provider": _active_provider,
        "any_connected": any(info["connected"] for info in providers.values()),
    }


def _active_page() -> tuple[str | None, Any | None]:
    if not _active_provider:
        return None, None
    return _active_provider, _pages.get(_active_provider)


async def _capture_view(page, provider: str) -> dict:
    screenshot = await page.screenshot(type="png")
    b64 = base64.b64encode(screenshot).decode("utf-8")
    logged_in = await _looks_logged_in(provider, page)
    return {
        "provider": provider,
        "status": "logged_in" if logged_in else "in_progress",
        "screenshot": b64,
        "url": page.url,
        "logged_in": logged_in,
    }


async def start_login(provider: str) -> dict:
    global _active_provider

    p = _provider_key(provider)
    context = await _ensure_context(p)

    old_page = _pages.get(p)
    if old_page:
        try:
            await old_page.close()
        except Exception:
            pass

    page = await context.new_page()
    _pages[p] = page
    _active_provider = p

    try:
        await _goto_with_fallback(page, p, target="login")
        await page.wait_for_timeout(1200)
        db.set_setting(_setting_key(p, "login_status"), "in_progress")
        data = await _capture_view(page, p)
        data["message"] = f"{PROVIDER_CONFIG[p]['label']} login page loaded."
        return data
    except Exception as exc:
        log.error("Failed to open %s login: %s", p, exc)
        return {"status": "error", "provider": p, "message": str(exc)}

async def get_screenshot() -> dict:
    provider, page = _active_page()
    if not provider or not page:
        return {"status": "no_page", "screenshot": None}

    try:
        await page.wait_for_timeout(250)
        return await _capture_view(page, provider)
    except Exception as exc:
        log.error("Food screenshot failed: %s", exc)
        return {"status": "error", "message": str(exc)}


async def browser_click(x: int, y: int) -> dict:
    provider, page = _active_page()
    if not provider or not page:
        return {"status": "no_page"}

    try:
        await page.mouse.click(x, y)
        await page.wait_for_timeout(800)
        return await _capture_view(page, provider)
    except Exception as exc:
        log.error("Food click failed: %s", exc)
        return {"status": "error", "message": str(exc)}


async def browser_type(text: str) -> dict:
    provider, page = _active_page()
    if not provider or not page:
        return {"status": "no_page"}

    try:
        await page.keyboard.type(text, delay=45)
        await page.wait_for_timeout(250)
        return await _capture_view(page, provider)
    except Exception as exc:
        log.error("Food type failed: %s", exc)
        return {"status": "error", "message": str(exc)}


async def browser_key(key: str) -> dict:
    provider, page = _active_page()
    if not provider or not page:
        return {"status": "no_page"}

    try:
        await page.keyboard.press(key)
        await page.wait_for_timeout(700)
        return await _capture_view(page, provider)
    except Exception as exc:
        log.error("Food key press failed: %s", exc)
        return {"status": "error", "message": str(exc)}


async def _looks_logged_in(provider: str, page) -> bool:
    try:
        url = page.url.lower()
        if "orders" in url or "my-account" in url or "account" in url:
            db.set_setting(_setting_key(provider, "connected"), "true")
            db.set_setting(_setting_key(provider, "login_time"), datetime.now(timezone.utc).isoformat())
            await _save_storage_state(provider)
            return True

        account_markers = await page.evaluate(
            """() => {
                const selectors = [
                    'a[href*="logout"]',
                    'a[href*="orders"]',
                    'a[href*="account"]',
                    '[data-testid*="account"]',
                    '[data-testid*="profile"]'
                ];
                return selectors.some((selector) => Boolean(document.querySelector(selector)));
            }"""
        )
        if account_markers:
            db.set_setting(_setting_key(provider, "connected"), "true")
            db.set_setting(_setting_key(provider, "login_time"), datetime.now(timezone.utc).isoformat())
            await _save_storage_state(provider)
            return True

        return False
    except Exception:
        return False


async def finish_login(provider: str) -> dict:
    p = _provider_key(provider)
    page = _pages.get(p)
    if not page:
        return {"status": "error", "message": "No active login page for provider."}

    try:
        await _goto_with_fallback(page, p, target="orders")
        await page.wait_for_timeout(1800)

        if not await _looks_logged_in(p, page):
            db.set_setting(_setting_key(p, "connected"), "false")
            return {
                "status": "error",
                "provider": p,
                "message": f"Could not verify {PROVIDER_CONFIG[p]['label']} login. Please complete login and retry.",
            }

        db.set_setting(_setting_key(p, "connected"), "true")
        db.set_setting(_setting_key(p, "login_status"), "logged_in")
        db.set_setting(_setting_key(p, "login_time"), datetime.now(timezone.utc).isoformat())
        await _save_storage_state(p)

        return await _capture_view(page, p)
    except Exception as exc:
        log.error("Finish login failed for %s: %s", p, exc)
        return {"status": "error", "provider": p, "message": str(exc)}


async def sync_order_history(provider: str | None = None) -> dict:
    providers = [
        _provider_key(provider)
    ] if provider else ["swiggy"] if db.get_setting(_setting_key("swiggy", "connected")) == "true" else []

    if not providers:
        return {
            "synced": 0,
            "total_found": 0,
            "errors": ["Connect Swiggy before syncing."],
        }

    total_synced = 0
    total_found = 0
    errors: list[str] = []

    for p in providers:
        result = await _sync_provider_history(p)
        total_synced += result.get("synced", 0)
        total_found += result.get("total_found", 0)
        if result.get("error"):
            errors.append(result["error"])

    return {
        "synced": total_synced,
        "total_found": total_found,
        "providers": providers,
        "errors": errors,
    }


async def _sync_provider_history(provider: str) -> dict:
    return await _sync_swiggy_history(provider)


async def _sync_swiggy_history(provider: str) -> dict:
    context = await _ensure_context(provider)

    # Reuse the existing persistent page for this provider (kept alive after login).
    # If no page exists yet, create one and store it so future calls can reuse it.
    page = _pages.get(provider)
    if not page or page.is_closed():
        page = await context.new_page()
        _pages[provider] = page
        log.info("Created new persistent scrape page for %s", provider)

    try:
        await _goto_with_fallback(page, provider, target="orders")
        await page.wait_for_timeout(2500)

        if not await _looks_logged_in(provider, page):
            db.set_setting(_setting_key(provider, "connected"), "false")
            return {
                "synced": 0,
                "total_found": 0,
                "error": f"Session expired for {PROVIDER_CONFIG[provider]['label']}. Reconnect and retry.",
            }

        raw_orders = await _extract_orders_from_swiggy(page)
        normalized = [_normalize_order(provider, item) for item in raw_orders]
        normalized = [item for item in normalized if item]

        for order in normalized:
            db.insert_food_order(order)

        db.set_setting(_setting_key(provider, "history_synced"), "true")
        db.set_setting(_setting_key(provider, "last_sync_time"), datetime.now(timezone.utc).isoformat())

        # Page intentionally NOT closed — kept alive for background scraping & live data polling.
        log.info("Swiggy scrape page kept alive. Provider: %s, URL: %s", provider, page.url)

        return {
            "synced": len(normalized),
            "total_found": len(normalized),
            "provider": provider,
        }
    except Exception as exc:
        log.error("Failed to sync %s history: %s", provider, exc)
        # If the page is dead/crashed, clear it so next call recreates it
        if page.is_closed():
            _pages.pop(provider, None)
        return {
            "synced": 0,
            "total_found": 0,
            "provider": provider,
            "error": f"{PROVIDER_CONFIG[provider]['label']} sync failed: {exc}",
        }


async def _extract_orders_from_swiggy(page) -> list[dict]:
    """Extract Swiggy orders by calling their internal API directly from within
    the logged-in browser page context (same-origin fetch with session cookies).

    Swiggy's order endpoint: GET /dapi/order/all?order_id=&offset=N&page_size=10
    We paginate until empty, then fall back to DOM scraping if needed.
    """
    # Make sure we're on the Swiggy domain (for same-origin credentials)
    if "swiggy.com" not in page.url:
        await page.goto("https://www.swiggy.com/my-account/orders", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)

    all_orders: list[dict] = []
    offset = 0
    page_size = 15
    max_pages = 20  # Safety cap at 300 orders

    for page_num in range(max_pages):
        log.info("Fetching Swiggy orders page %d (offset=%d)", page_num + 1, offset)

        result = await page.evaluate("""async (params) => {
            try {
                const url = `/dapi/order/all?order_id=&order_type=&offset=${params.offset}&page_size=${params.page_size}&delivery_order=1`;
                const resp = await fetch(url, {
                    method: 'GET',
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                        '__fetch_req_id': Date.now().toString(),
                    },
                    credentials: 'include',
                });
                if (!resp.ok) {
                    return { error: `HTTP ${resp.status}` };
                }
                return await resp.json();
            } catch (e) {
                return { error: e.message };
            }
        }""", {"offset": offset, "page_size": page_size})

        if not result or result.get("error"):
            log.warning("Swiggy API fetch failed at offset %d: %s", offset, result)
            break

        # Parse structured response
        batch = _parse_swiggy_api_response(result)
        if not batch:
            log.info("No more Swiggy orders at offset %d", offset)
            break

        log.info("Got %d Swiggy orders at offset %d", len(batch), offset)
        all_orders.extend(batch)
        offset += len(batch)

        if len(batch) < page_size:
            break  # Last page

    if all_orders:
        log.info("Total Swiggy orders fetched via API: %d", len(all_orders))
        return all_orders

    # ---- DOM fallback (if API call returned nothing) ----
    log.warning("Swiggy direct API returned no orders, using DOM fallback")
    return await _extract_orders_from_dom_fallback(page)



def _parse_swiggy_api_response(data: dict) -> list[dict]:
    """Parse Swiggy's internal order list API JSON.

    The response structure varies by endpoint version but typically looks like:
      { "data": { "orders": [...] } }  or  { "statusCode": 0, "data": { "orders": [...] } }
    """
    orders: list[dict] = []

    # Try various known response shapes
    candidates = []
    if isinstance(data, dict):
        d = data.get("data") or data
        if isinstance(d, dict):
            candidates.append(d.get("orders") or d.get("order_list") or d.get("order_history") or [])
            candidates.append(d.get("data", {}).get("orders") or [])
        if isinstance(d, list):
            candidates.append(d)

    for candidate in candidates:
        if not isinstance(candidate, list) or not candidate:
            continue
        for item in candidate:
            if not isinstance(item, dict):
                continue
            parsed = _parse_swiggy_order_item(item)
            if parsed:
                orders.append(parsed)
        if orders:
            break  # Stop once we've found a working candidate

    return orders


def _parse_swiggy_order_item(item: dict) -> dict | None:
    """Normalize a single order object from Swiggy's API JSON."""
    try:
        order_id = str(item.get("order_id") or item.get("id") or "")
        if not order_id:
            return None

        # Restaurant name
        restaurant = (
            item.get("restaurant_name")
            or item.get("restaurant", {}).get("name")
            or item.get("order_items", [{}])[0].get("restaurant_name")
            if item.get("order_items")
            else None
        )

        # Item name — join all item names from the order
        item_list = item.get("order_items") or item.get("items") or []
        item_names = [
            i.get("name") or i.get("item_name") or i.get("dish_name", "")
            for i in item_list
            if isinstance(i, dict)
        ]
        item_name = ", ".join(filter(None, item_names)) or item.get("item_name")

        # Price
        raw_price = item.get("order_total") or item.get("total") or item.get("grand_total") or item.get("bill_total")

        # Timestamp
        raw_ts = item.get("order_time") or item.get("created_at") or item.get("placed_at") or item.get("updated_at")

        # Status
        raw_status = (
            item.get("order_status")
            or item.get("status")
            or item.get("order_status_v2")
            or "delivered"
        )

        return {
            "external_order_id": f"swiggy-{order_id}",
            "restaurant_name": str(restaurant).strip() if restaurant else None,
            "item_name": str(item_name).strip() if item_name else None,
            "cuisine": None,
            "status": str(raw_status).lower(),
            "price": raw_price,
            "eta_minutes": None,
            "order_timestamp": str(raw_ts) if raw_ts else None,
            "delivery_address": item.get("delivery_address", {}).get("address") if isinstance(item.get("delivery_address"), dict) else None,
            "raw_payload": item,
        }
    except Exception as exc:
        log.debug("Could not parse Swiggy order item: %s", exc)
        return None


async def _extract_orders_from_dom_fallback(page) -> list[dict]:
    """Best-effort DOM scrape (kept as a fallback for when API interception fails)."""
    for _ in range(6):
        await page.mouse.wheel(0, 2200)
        await page.wait_for_timeout(500)

    return await page.evaluate(
        """() => {
            const results = [];

            const parsePrice = (text) => {
                const match = text.match(/(?:\u20b9|Rs\\.?|INR)\\s*([0-9][0-9,]*(?:\\.[0-9]{1,2})?)/i);
                return match ? match[1] : null;
            };

            const cards = Array.from(document.querySelectorAll('article, li, div')).filter((node) => {
                const text = (node.innerText || '').trim();
                if (!text || text.length < 30 || text.length > 550) return false;
                const hasOrderHints = /(order|delivered|cancelled|ordered)/i.test(text);
                const hasMoney = /(\u20b9|Rs\\.?|INR)/i.test(text);
                return hasOrderHints && hasMoney;
            }).slice(0, 120);

            for (const card of cards) {
                const text = (card.innerText || '').replace(/\\s+/g, ' ').trim();
                const lines = (card.innerText || '')
                    .split('\\n')
                    .map((line) => line.trim())
                    .filter(Boolean)
                    .slice(0, 8);

                const orderLink = card.querySelector('a[href*="order"], a[href*="orders"]');
                const href = orderLink ? orderLink.getAttribute('href') || '' : '';
                const idMatch = href.match(/(?:order|orders)\\/([A-Za-z0-9_-]{5,})/i);
                const statusMatch = text.match(/(delivered|cancelled|completed|processing)/i);
                const restaurant = lines.find((l) => l && !/(order|delivered|cancelled|\u20b9|rs|inr|mins|minutes)/i.test(l));
                const item = lines.length > 1 ? lines[1] : null;

                results.push({
                    external_order_id: idMatch ? idMatch[1] : null,
                    restaurant_name: restaurant || null,
                    item_name: item || null,
                    cuisine: null,
                    status: statusMatch ? statusMatch[1].toLowerCase() : 'delivered',
                    price: parsePrice(text),
                    eta_minutes: null,
                    order_timestamp: null,
                    delivery_address: null,
                    raw_payload: { snippet: text },
                });
            }

            const deduped = [];
            const seen = new Set();
            for (const item of results) {
                const key = `${item.external_order_id || ''}|${item.restaurant_name || ''}|${item.item_name || ''}|${item.price || ''}`.toLowerCase();
                if (seen.has(key)) continue;
                seen.add(key);
                deduped.push(item);
            }

            return deduped;
        }"""
    )


async def scrape_top_restaurants(lat: float | None = None, lng: float | None = None) -> dict:
    """Scrape top restaurants from Swiggy's homepage restaurant listing.

    Uses the existing Playwright browser session to call Swiggy's internal API
    from within the logged-in page context (same-origin fetch with cookies).
    Does NOT require the user to have any order history.

    Returns a dict with 'restaurants' list and metadata.
    """
    provider = "swiggy"

    # --- Ensure a Swiggy browser context is alive ---
    try:
        context = await _ensure_context(provider)
    except Exception as exc:
        log.error("Failed to create Swiggy browser context: %s", exc)
        return {"restaurants": [], "error": f"Browser init failed: {exc}"}

    # Reuse / create persistent page for Swiggy
    page = _pages.get(provider)
    if not page or page.is_closed():
        page = await context.new_page()
        _pages[provider] = page
        log.info("Created new persistent page for Swiggy restaurant scraping")

    # Navigate to Swiggy if not already there
    try:
        current_url = page.url or ""
        if "swiggy.com" not in current_url:
            await page.goto("https://www.swiggy.com/", wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)
    except Exception as exc:
        log.warning("Navigation to Swiggy failed: %s", exc)

    # Default coordinates: Bangalore (Swiggy HQ area) — fallback if none provided
    _lat = lat or 12.9352
    _lng = lng or 77.6245

    # --- Try Swiggy's internal restaurant listing API ---
    restaurants: list[dict] = []

    result = await page.evaluate("""async (params) => {
        try {
            const url = `/dapi/restaurants/list/v5?lat=${params.lat}&lng=${params.lng}&is-seo-homepage-enabled=true&page_type=DESKTOP_WEB_LISTING`;
            const resp = await fetch(url, {
                method: 'GET',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                },
                credentials: 'include',
            });
            if (!resp.ok) {
                return { error: `HTTP ${resp.status}` };
            }
            return await resp.json();
        } catch (e) {
            return { error: e.message };
        }
    }""", {"lat": _lat, "lng": _lng})

    if result and not result.get("error"):
        restaurants = _parse_restaurant_listing(result)
        log.info("Swiggy restaurant API returned %d restaurants", len(restaurants))
    else:
        log.warning("Swiggy restaurant API failed: %s — trying DOM fallback", result)
        restaurants = await _scrape_restaurants_from_dom(page)

    if not restaurants:
        log.warning("No restaurants found from Swiggy, returning empty list")

    return {
        "restaurants": restaurants[:20],
        "count": len(restaurants[:20]),
        "source": "swiggy",
        "coordinates": {"lat": _lat, "lng": _lng},
    }


async def get_live_match_for_suggestion(
    restaurant_name: str,
    item_name: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
) -> dict | None:
    if not restaurant_name:
        return None

    result = await scrape_top_restaurants(lat=lat, lng=lng)
    restaurants = result.get("restaurants") or []
    if not restaurants:
        return None

    target_restaurant = _normalize_match_text(restaurant_name)
    target_item = _normalize_match_text(item_name)

    best_match: dict | None = None
    best_score = 0.0

    for restaurant in restaurants:
        if not isinstance(restaurant, dict):
            continue

        restaurant_score = _text_similarity(
            target_restaurant,
            _normalize_match_text(restaurant.get("name")),
        )
        item_match = _match_menu_item(target_item, restaurant.get("menu_items"))
        item_score = item_match.get("score", 0.0) if item_match else 0.0
        total_score = (restaurant_score * 0.78) + (item_score * 0.22)

        if total_score > best_score:
            best_score = total_score
            best_match = {
                "restaurant_name": restaurant.get("name") or restaurant_name,
                "item_name": item_match.get("name") if item_match else item_name,
                "item_price": item_match.get("price") if item_match else None,
                "delivery_time_mins": _safe_int(restaurant.get("delivery_time_mins")),
                "cost_for_two": restaurant.get("cost_for_two"),
                "deeplink": restaurant.get("deeplink"),
                "offer": restaurant.get("offer"),
                "image_url": restaurant.get("image_url"),
                "is_open": restaurant.get("is_open"),
                "match_score": round(total_score, 3),
            }

    if best_match and best_score >= 0.62:
        return best_match
    return None


def _parse_restaurant_listing(data: dict) -> list[dict]:
    """Parse Swiggy's restaurant listing API response into structured restaurant dicts."""
    restaurants: list[dict] = []
    seen_ids: set[str] = set()

    # Swiggy's response has nested cards structure
    # data -> data -> cards -> [card] -> card -> card -> info
    cards = []

    # Try different known response structures
    if isinstance(data, dict):
        top_data = data.get("data", data)
        if isinstance(top_data, dict):
            # Structure: data.cards[].card.card.gridElements.infoWithStyle.restaurants[].info
            # or data.cards[].card.card.info (direct restaurant cards)
            raw_cards = top_data.get("cards", [])
            if not raw_cards:
                raw_cards = top_data.get("data", {}).get("cards", [])

            for card_wrapper in raw_cards:
                if not isinstance(card_wrapper, dict):
                    continue

                card = card_wrapper.get("card", {}).get("card", card_wrapper)
                if not isinstance(card, dict):
                    continue

                # Check for restaurant grid (most common)
                grid_elements = card.get("gridElements", {})
                if isinstance(grid_elements, dict):
                    info_style = grid_elements.get("infoWithStyle", {})
                    if isinstance(info_style, dict):
                        res_list = info_style.get("restaurants", [])
                        for r in res_list:
                            if isinstance(r, dict):
                                info = r.get("info", r)
                                if isinstance(info, dict) and info.get("name"):
                                    cards.append({"info": info, "cta": r.get("cta", {})})

                # Direct restaurant info in card
                if card.get("name") and card.get("id"):
                    cards.append({"info": card, "cta": card.get("cta", {})})

    for item in cards:
        info = item.get("info", {})
        cta = item.get("cta", {})

        rest_id = str(info.get("id", ""))
        if not rest_id or rest_id in seen_ids:
            continue
        seen_ids.add(rest_id)

        name = (info.get("name") or "").strip()
        if not name:
            continue

        # Extract cuisines
        cuisines_raw = info.get("cuisines", [])
        if isinstance(cuisines_raw, list):
            cuisines = ", ".join(str(c) for c in cuisines_raw[:5])
        else:
            cuisines = str(cuisines_raw)

        # Rating
        avg_rating = info.get("avgRating") or info.get("avgRatingString")
        total_ratings = info.get("totalRatingsString") or info.get("totalRatings", "")

        # Delivery time
        sla = info.get("sla", {})
        delivery_time = sla.get("deliveryTime") or sla.get("slaString", "")

        # Price
        cost_for_two = info.get("costForTwo", "")

        # Image
        cloud_img_id = info.get("cloudinaryImageId", "")
        image_url = ""
        if cloud_img_id:
            image_url = f"https://media-assets.swiggy.com/swiggy/image/upload/fl_lossy,f_auto,q_auto,w_660/{cloud_img_id}"

        # Swiggy deeplink
        slug = info.get("slugs", {})
        city_slug = slug.get("city", "")
        rest_slug = slug.get("restaurant", "")
        deeplink = ""
        if city_slug and rest_slug:
            deeplink = f"https://www.swiggy.com/{city_slug}/{rest_slug}"
        elif cta and cta.get("link"):
            deeplink = cta.get("link", "")
            if deeplink and not deeplink.startswith("http"):
                deeplink = f"https://www.swiggy.com{deeplink}"

        # Offers
        aggregated_discount = info.get("aggregatedDiscountInfoV3", {})
        offer_header = aggregated_discount.get("header", "") if isinstance(aggregated_discount, dict) else ""
        offer_sub = aggregated_discount.get("subHeader", "") if isinstance(aggregated_discount, dict) else ""
        offer_text = f"{offer_header} {offer_sub}".strip() if offer_header else ""

        # Veg/NonVeg
        veg = info.get("veg", False)

        # Is open / available
        is_open = info.get("isOpen", True)
        availability = info.get("availability", {})

        # Top dishes from menu items if available
        menu_items = []
        if info.get("menu"):
            for mi in info.get("menu", [])[:5]:
                if isinstance(mi, dict):
                    menu_items.append({
                        "name": mi.get("name", ""),
                        "price": mi.get("price", 0),
                    })

        restaurants.append({
            "id": rest_id,
            "name": name,
            "cuisines": cuisines,
            "avg_rating": str(avg_rating) if avg_rating else None,
            "total_ratings": str(total_ratings) if total_ratings else None,
            "delivery_time_mins": delivery_time,
            "cost_for_two": str(cost_for_two),
            "image_url": image_url,
            "deeplink": deeplink,
            "offer": offer_text,
            "is_veg": veg,
            "is_open": is_open,
            "area_name": info.get("areaName", ""),
            "menu_items": menu_items,
        })

    return restaurants


async def _scrape_restaurants_from_dom(page) -> list[dict]:
    """Fallback DOM scraping to get restaurant cards from Swiggy's homepage."""
    # Ensure we're on Swiggy's main page
    try:
        current_url = page.url or ""
        if "swiggy.com" not in current_url:
            await page.goto("https://www.swiggy.com/", wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)

        # Scroll down to load more restaurants
        for _ in range(4):
            await page.mouse.wheel(0, 1500)
            await page.wait_for_timeout(800)
    except Exception as exc:
        log.warning("DOM scraping navigation failed: %s", exc)
        return []

    return await page.evaluate("""() => {
        const results = [];
        const seen = new Set();

        // Swiggy restaurant cards typically have data-testid or specific class patterns
        // Try to find restaurant card links
        const links = document.querySelectorAll('a[href*="/restaurants/"]');
        
        for (const link of links) {
            const href = link.getAttribute('href') || '';
            if (seen.has(href) || !href.includes('/restaurants/')) continue;
            seen.add(href);

            const card = link.closest('div') || link;
            const text = (card.innerText || '').trim();
            if (!text || text.length < 10) continue;

            const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);

            // Try to extract structured data from card text
            const name = lines[0] || '';
            const ratingMatch = text.match(/(\\d+\\.\\d)\\s*/);
            const timeMatch = text.match(/(\\d+)\\s*mins?/i);
            const priceMatch = text.match(/₹(\\d+)/);

            // Get image
            const img = card.querySelector('img');
            const imageUrl = img ? (img.getAttribute('src') || '') : '';

            if (name && name.length > 2 && name.length < 80) {
                results.push({
                    id: href.split('/').pop() || name.replace(/\\s+/g, '-').toLowerCase(),
                    name: name,
                    cuisines: lines.length > 1 ? lines[1] : '',
                    avg_rating: ratingMatch ? ratingMatch[1] : null,
                    total_ratings: null,
                    delivery_time_mins: timeMatch ? parseInt(timeMatch[1]) : null,
                    cost_for_two: priceMatch ? '₹' + priceMatch[1] + ' for two' : '',
                    image_url: imageUrl,
                    deeplink: href.startsWith('http') ? href : 'https://www.swiggy.com' + href,
                    offer: '',
                    is_veg: false,
                    is_open: true,
                    area_name: '',
                    menu_items: [],
                });
            }

            if (results.length >= 20) break;
        }

        return results;
    }""")


def _normalize_order(provider: str, payload: dict) -> dict | None:
    if not isinstance(payload, dict):
        return None

    restaurant = _clean_text(payload.get("restaurant_name"))
    item_name = _clean_text(payload.get("item_name"))

    if not restaurant and not item_name:
        return None

    price = _to_float(payload.get("price"))
    eta = _to_float(payload.get("eta_minutes"))
    order_timestamp = _parse_order_timestamp(payload.get("order_timestamp"))

    external_order_id = _clean_text(payload.get("external_order_id"))
    if not external_order_id:
        hash_source = f"{provider}|{restaurant}|{item_name}|{order_timestamp}|{price}"
        external_order_id = hashlib.md5(hash_source.encode("utf-8")).hexdigest()[:20]

    return {
        "external_order_id": external_order_id,
        "source_platform": provider,
        "restaurant_name": restaurant or "Unknown restaurant",
        "item_name": item_name or "Order item",
        "cuisine": _clean_text(payload.get("cuisine")) or "Unknown",
        "order_timestamp": order_timestamp,
        "status": _clean_text(payload.get("status")) or "delivered",
        "price": price,
        "eta_minutes": eta,
        "delivery_address": _clean_text(payload.get("delivery_address")),
        "raw_payload": payload.get("raw_payload") or payload,
    }


def _normalize_match_text(value) -> str:
    text = _clean_text(value) or ""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _text_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.94
    return SequenceMatcher(None, left, right).ratio()


def _match_menu_item(target_item: str, menu_items) -> dict | None:
    if not target_item or not isinstance(menu_items, list):
        return None

    best: dict | None = None
    best_score = 0.0
    for item in menu_items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        score = _text_similarity(target_item, _normalize_match_text(name))
        if score > best_score:
            best_score = score
            best = {
                "name": name,
                "price": _to_float(item.get("price")),
                "score": score,
            }

    if best and best_score >= 0.58:
        return best
    return None


def _safe_int(value) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    match = re.search(r"(\d+)", str(value))
    if not match:
        return None
    return int(match.group(1))


def _clean_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _to_float(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().replace(",", "")
    text = re.sub(r"[^0-9.]", "", text)
    if not text:
        return None

    try:
        return float(text)
    except ValueError:
        return None


def _parse_order_timestamp(value) -> str:
    now = datetime.now(timezone.utc)
    if not value:
        return now.isoformat()

    text = str(value).strip()

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        pass

    formats = [
        "%d %b %Y, %I:%M %p",
        "%d %b %Y %I:%M %p",
        "%b %d, %Y %I:%M %p",
        "%d %b, %I:%M %p",
        "%b %d, %I:%M %p",
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            if "%Y" not in fmt:
                parsed = parsed.replace(year=now.year)
            return parsed.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue

    return now.isoformat()
