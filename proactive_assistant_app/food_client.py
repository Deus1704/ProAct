"""Swiggy/Zomato integration via browser-assisted login and history scraping."""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from . import database as db
from . import zomato_api

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
    "zomato": {
        "label": "Zomato",
        "login_url": "https://www.zomato.com/restaurants",
        "orders_url": "https://www.zomato.com/orders",
        "login_urls": [
            "https://www.zomato.com/restaurants",
            "https://www.zomato.com/in",
            "https://www.zomato.com/?desktop_web=1",
        ],
        "orders_urls": [
            "https://www.zomato.com/orders",
            "https://www.zomato.com/in/orders",
            "https://www.zomato.com",
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
    if p == "zomato":
        return {
            "status": "info",
            "provider": p,
            "message": "Zomato requires cookie-based login. Please use the cookie login endpoint.",
            "requires_cookie": True,
        }

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


async def zomato_cookie_login(cookie: str) -> dict:
    p = "zomato"
    is_valid = await zomato_api.check_auth_async(cookie)
    if not is_valid:
        return {
            "status": "error",
            "provider": p,
            "message": "The provided Zomato cookie is invalid or expired.",
        }

    db.set_setting(_setting_key(p, "zomato_cookie"), cookie)
    db.set_setting(_setting_key(p, "connected"), "true")
    db.set_setting(_setting_key(p, "login_status"), "logged_in")
    db.set_setting(_setting_key(p, "login_time"), datetime.now(timezone.utc).isoformat())

    return {
        "status": "logged_in",
        "provider": p,
        "message": "Zomato successfully connected via cookie.",
    }


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
    ] if provider else [
        p for p in PROVIDER_CONFIG.keys() if db.get_setting(_setting_key(p, "connected")) == "true"
    ]

    if not providers:
        return {
            "synced": 0,
            "total_found": 0,
            "errors": ["Connect Swiggy or Zomato before syncing."],
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
    if provider == "zomato":
        return await _sync_zomato_history()
    return await _sync_swiggy_history(provider)


async def _sync_zomato_history() -> dict:
    provider = "zomato"
    cookie = db.get_setting(_setting_key(provider, "zomato_cookie"))
    if not cookie:
        db.set_setting(_setting_key(provider, "connected"), "false")
        return {
            "synced": 0,
            "total_found": 0,
            "error": "No Zomato cookie found. Please log in first.",
        }

    is_valid = await zomato_api.check_auth_async(cookie)
    if not is_valid:
        db.set_setting(_setting_key(provider, "connected"), "false")
        return {
            "synced": 0,
            "total_found": 0,
            "error": "Zomato session expired. Reconnect and retry.",
        }

    try:
        orders = await zomato_api.fetch_all_orders_async(cookie)
        for order in orders:
            db.insert_food_order(order)
        
        db.set_setting(_setting_key(provider, "history_synced"), "true")
        db.set_setting(_setting_key(provider, "last_sync_time"), datetime.now(timezone.utc).isoformat())

        return {
            "synced": len(orders),
            "total_found": len(orders),
            "provider": provider,
        }
    except Exception as exc:
        log.error("Failed to sync Zomato history via API: %s", exc)
        return {
            "synced": 0,
            "total_found": 0,
            "provider": provider,
            "error": f"Zomato API sync failed: {exc}",
        }


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
