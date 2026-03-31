"""Zomato cookie-based API client for order history.

Inspired by https://github.com/maheshrijal/zocli — uses Zomato's internal
web-routes with a session cookie instead of browser automation.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

ZOMATO_BASE_URL = "https://www.zomato.com"

_COMMON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}


def _make_headers(cookie: str) -> dict[str, str]:
    """Build request headers with the given cookie string."""
    headers = dict(_COMMON_HEADERS)
    headers["Cookie"] = cookie
    return headers


# ---------------------------------------------------------------------------
# Auth check
# ---------------------------------------------------------------------------

def check_auth(cookie: str) -> bool:
    """Return *True* if the cookie grants authenticated access to Zomato.

    Uses ``/webroutes/user/address`` exactly like zocli's CheckAuth.
    """
    url = f"{ZOMATO_BASE_URL}/webroutes/user/address"
    try:
        resp = requests.get(url, headers=_make_headers(cookie), timeout=15)
        if resp.status_code == 200:
            return True
        if resp.status_code in (401, 403):
            return False
        log.warning("Zomato auth-check returned status %s", resp.status_code)
        return False
    except requests.RequestException as exc:
        log.error("Zomato auth-check network error: %s", exc)
        return False


async def check_auth_async(cookie: str) -> bool:
    """Async wrapper around :func:`check_auth`."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, check_auth, cookie)


# ---------------------------------------------------------------------------
# Order fetching
# ---------------------------------------------------------------------------

def _fetch_orders_page(cookie: str, page: int = 1) -> dict:
    """Fetch a single page from ``/webroutes/user/orders``.

    Returns the raw JSON dict or raises on HTTP failure.
    """
    url = f"{ZOMATO_BASE_URL}/webroutes/user/orders"
    params = {}
    if page > 1:
        params["page"] = str(page)

    resp = requests.get(
        url, headers=_make_headers(cookie), params=params, timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Zomato orders request failed: HTTP {resp.status_code}"
        )
    return resp.json()


def _orders_from_response(data: dict) -> list[dict]:
    """Extract normalised order dicts from a raw Zomato API response.

    Mirrors the logic in zocli's ``ordersFromResponse`` /
    ``normalizeOrder``.
    """
    sections = data.get("sections", {})
    history = sections.get("SECTION_USER_ORDER_HISTORY", {})
    entities_map: dict[str, dict] = (
        data.get("entities", {}).get("ORDER", {})
    )

    entity_lists = history.get("entities", [])
    orders: list[dict] = []

    for entity_block in entity_lists:
        if entity_block.get("entity_type") != "ORDER":
            continue
        for eid in entity_block.get("entity_ids", []):
            raw = entities_map.get(str(eid))
            if not raw:
                continue
            orders.append(_normalize_order_entity(raw))

    return orders


_PRICE_RE = re.compile(r"[₹Rs.INR\s,]*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)


def _parse_price(text: str) -> float | None:
    if not text:
        return None
    text = text.replace(",", "").strip()
    text = re.sub(r"[^\d.]", "", text)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


_ORDER_DATE_FORMATS = [
    "%B %d, %Y at %I:%M %p",
    "%B %d, %Y %I:%M %p",
    "%b %d, %Y at %I:%M %p",
    "%b %d, %Y %I:%M %p",
    "%d %b %Y at %I:%M %p",
    "%d %b %Y %I:%M %p",
    "%Y-%m-%d %H:%M",
]


def _parse_order_date(text: str) -> str:
    """Parse Zomato's order date string into an ISO timestamp."""
    if not text:
        return datetime.now(timezone.utc).isoformat()

    # Try ISO first
    try:
        return (
            datetime.fromisoformat(text.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .isoformat()
        )
    except ValueError:
        pass

    for fmt in _ORDER_DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue

    return datetime.now(timezone.utc).isoformat()


_DISH_QTY_RE = re.compile(r"^\s*(\d+)\s*x\s*(.+)\s*$")


def _parse_dish_string(dish_string: str) -> list[dict]:
    """Parse Zomato's comma-separated dish string into item dicts.

    Handles ``2 x Paneer Butter Masala, 1 x Naan`` style strings and
    respects brackets/parentheses.
    """
    if not dish_string:
        return []

    parts: list[str] = []
    current: list[str] = []
    depth = 0

    for ch in dish_string:
        if ch in ("(", "["):
            depth += 1
            current.append(ch)
        elif ch in (")", "]"):
            depth = max(0, depth - 1)
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)

    if current:
        parts.append("".join(current))

    items: list[dict] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = _DISH_QTY_RE.match(part)
        if m:
            qty = int(m.group(1))
            name = m.group(2).strip() or part
            items.append({"name": name, "quantity": qty})
        else:
            items.append({"name": part, "quantity": 1})

    return items


def _normalize_order_entity(raw: dict) -> dict:
    """Convert a single Zomato ORDER entity into our internal format."""
    delivery = raw.get("deliveryDetails", {})
    status = (
        (delivery.get("deliveryLabel") or "").strip()
        or (delivery.get("deliveryMessage") or "").strip()
    )
    if not status and raw.get("status"):
        status = f"Status {raw['status']}"
    if not status:
        status = "delivered"

    order_id = str(raw.get("orderId", ""))
    restaurant = (raw.get("resInfo", {}).get("name") or "").strip()
    total_cost = (raw.get("totalCost") or "").strip()
    dish_string = (raw.get("dishString") or "").strip()
    order_date = (raw.get("orderDate") or "").strip()

    price = _parse_price(total_cost)
    items = _parse_dish_string(dish_string)
    order_timestamp = _parse_order_date(order_date)

    return {
        "external_order_id": order_id,
        "source_platform": "zomato",
        "restaurant_name": restaurant or "Unknown restaurant",
        "item_name": items[0]["name"] if items else (dish_string or "Order"),
        "cuisine": "Unknown",
        "order_timestamp": order_timestamp,
        "status": status.lower(),
        "price": price,
        "eta_minutes": None,
        "delivery_address": None,
        "raw_payload": raw,
    }


# ---------------------------------------------------------------------------
# Paginated fetch
# ---------------------------------------------------------------------------

def fetch_all_orders(cookie: str) -> list[dict]:
    """Fetch *all* orders by paginating through ``/webroutes/user/orders``.

    De-duplicates by order ID. Returns a list of normalised order dicts.
    """
    all_orders: list[dict] = []
    seen: set[str] = set()
    page = 1

    while True:
        data = _fetch_orders_page(cookie, page)

        sections = data.get("sections", {})
        history = sections.get("SECTION_USER_ORDER_HISTORY", {})
        total_pages = history.get("totalPages", 0)

        orders = _orders_from_response(data)
        new_count = 0
        for order in orders:
            oid = order.get("external_order_id", "")
            if not oid or oid in seen:
                continue
            seen.add(oid)
            all_orders.append(order)
            new_count += 1

        log.info(
            "Zomato orders page %d/%d — %d new, %d total",
            page, total_pages, new_count, len(all_orders),
        )

        if new_count == 0:
            break
        if total_pages == 0 or page >= total_pages:
            break

        time.sleep(0.5)
        page += 1

    return all_orders


async def fetch_all_orders_async(cookie: str) -> list[dict]:
    """Async wrapper around :func:`fetch_all_orders`."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_all_orders, cookie)
