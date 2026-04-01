"""Fetcher helpers with timeout, fallback, cache, and scraper health tracking."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from . import database as db

log = logging.getLogger(__name__)


class DataUnavailableType:
    status = "unavailable"

    def __bool__(self) -> bool:
        return False


DataUnavailable = DataUnavailableType()


@dataclass
class CachedResult:
    data: dict[str, Any]
    fetched_at: str
    is_stale: bool
    stale_since: str | None = None


SCRAPER_HEALTH: dict[str, dict[str, Any]] = {}


def _snapshot_html(payload: dict[str, Any]) -> str:
    if payload.get("snapshot_html"):
        return str(payload["snapshot_html"])
    return f"<html><body><pre>{json.dumps(payload, indent=2, ensure_ascii=True)}</pre></body></html>"


def record_scraper_success(platform: str, page_url: str | None, payload: dict[str, Any]) -> None:
    health = SCRAPER_HEALTH.setdefault(platform, {"platform": platform, "last_success": None, "consecutive_failures": 0})
    timestamp = db.utc_now().isoformat()
    health["last_success"] = timestamp
    health["consecutive_failures"] = 0
    health["status"] = "ok"
    db.add_scraper_snapshot(platform, page_url, _snapshot_html(payload))


def record_scraper_failure(platform: str, warning: str | None = None) -> None:
    health = SCRAPER_HEALTH.setdefault(platform, {"platform": platform, "last_success": None, "consecutive_failures": 0})
    health["consecutive_failures"] += 1
    if health["consecutive_failures"] >= 5:
        health["status"] = "down"
    else:
        health["status"] = "degraded"
    if warning:
        health["warning"] = warning


def get_scraper_health() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for platform in sorted(set(SCRAPER_HEALTH) | {"uber", "swiggy", "traffic"}):
        entry = SCRAPER_HEALTH.get(platform, {"platform": platform, "last_success": None, "consecutive_failures": 0})
        results.append(
            {
                "platform": platform,
                "last_success": entry.get("last_success"),
                "consecutive_failures": entry.get("consecutive_failures", 0),
                "status": entry.get("status") or "ok",
                "warning": entry.get("warning"),
            }
        )
    return results


class DataFetcher:
    async def fetch_with_fallback(
        self,
        primary_fn: Callable[[], Awaitable[dict[str, Any]]],
        fallback_fn: Callable[[], Awaitable[dict[str, Any]]],
        cache_key: str,
        cache_ttl_minutes: int,
        *,
        platform: str,
        page_url: str | None = None,
    ) -> dict[str, Any] | CachedResult | DataUnavailableType:
        try:
            primary_result = await asyncio.wait_for(primary_fn(), timeout=8)
            db.upsert_scraper_cache(cache_key, primary_result, is_stale=False)
            record_scraper_success(platform, page_url, primary_result)
            return primary_result
        except Exception as primary_exc:
            record_scraper_failure(platform, "primary_failed")
            log.warning("Primary fetch failed for %s: %s", cache_key, primary_exc)

        try:
            fallback_result = await asyncio.wait_for(fallback_fn(), timeout=5)
            db.upsert_scraper_cache(cache_key, fallback_result, is_stale=False)
            record_scraper_success(platform, page_url, fallback_result)
            return fallback_result
        except Exception as fallback_exc:
            latest_snapshot = db.get_latest_scraper_snapshot(platform)
            if latest_snapshot:
                record_scraper_failure(platform, "SCRAPER_DRIFT")
                log.warning("SCRAPER_DRIFT for %s: %s", platform, fallback_exc)

        cached = db.get_scraper_cache(cache_key)
        if not cached:
            return DataUnavailable

        fetched_at = cached["fetched_at"]
        fetched_dt = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        age_minutes = (db.utc_now() - fetched_dt).total_seconds() / 60
        is_stale = age_minutes > cache_ttl_minutes
        db.upsert_scraper_cache(cache_key, cached["data"], fetched_dt, is_stale=is_stale)
        return CachedResult(
            data=cached["data"],
            fetched_at=fetched_at,
            is_stale=is_stale,
            stale_since=fetched_at if is_stale else None,
        )
