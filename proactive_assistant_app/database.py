"""SQLite persistence layer for the deterministic proactive assistant."""

from __future__ import annotations

import json
import os
import sqlite3
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# Overridable so an evaluation or test run can point at a throwaway database
# instead of the developer's real one. get_db() reads the module global on every
# call, so tests may also assign database.DB_PATH directly.
DB_PATH = os.environ.get(
    "ASSISTANT_DB_PATH", os.path.join(os.path.dirname(__file__), "assistant.db")
)
UTC = timezone.utc


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def utc_now() -> datetime:
    return datetime.now(UTC)


def _to_iso(value: Any | None) -> str:
    if value is None:
        return utc_now().isoformat()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC).isoformat()
        return value.astimezone(UTC).isoformat()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC).isoformat()
    text = str(value).strip()
    if not text:
        return utc_now().isoformat()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return utc_now().isoformat()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _json(value: Any | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def _stable_row_id(platform: str, external_id: Any | None) -> int | None:
    if external_id is None:
        return None
    text = str(external_id).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        digest = hashlib.sha256(f"{platform}:{text}".encode("utf-8")).digest()
        # Keep the value inside SQLite's signed 64-bit integer range.
        return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


def _rows_to_list(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rides(
                id INTEGER PRIMARY KEY,
                platform TEXT NOT NULL,
                origin_label TEXT,
                origin_lat REAL,
                origin_lng REAL,
                dest_lat REAL,
                dest_lng REAL,
                dest_label TEXT,
                departure_time TEXT NOT NULL,
                ride_type TEXT,
                fare REAL,
                duration_min REAL,
                synced_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders(
                id INTEGER PRIMARY KEY,
                platform TEXT NOT NULL,
                restaurant_id TEXT,
                restaurant_name TEXT,
                items_json TEXT,
                total_price REAL,
                ordered_at TEXT NOT NULL,
                delivery_time_min REAL,
                cuisine TEXT,
                synced_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS departure_patterns(
                id INTEGER PRIMARY KEY,
                day_of_week INTEGER NOT NULL,
                hour_bin INTEGER NOT NULL,
                destination_id INTEGER,
                platform TEXT,
                ride_type TEXT,
                frequency INTEGER NOT NULL,
                confidence REAL NOT NULL,
                dismissal_count INTEGER NOT NULL DEFAULT 0,
                suppressed INTEGER NOT NULL DEFAULT 0,
                last_seen TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS destination_clusters(
                id INTEGER PRIMARY KEY,
                centroid_lat REAL NOT NULL,
                centroid_lng REAL NOT NULL,
                label TEXT,
                member_count INTEGER NOT NULL,
                last_visited TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS order_patterns(
                id INTEGER PRIMARY KEY,
                day_of_week INTEGER NOT NULL,
                hour_bin INTEGER NOT NULL,
                cuisine TEXT,
                restaurant_id TEXT,
                confidence REAL NOT NULL,
                dismissal_count INTEGER NOT NULL DEFAULT 0,
                suppressed INTEGER NOT NULL DEFAULT 0,
                last_seen TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS restaurant_patterns(
                id INTEGER PRIMARY KEY,
                restaurant_id TEXT NOT NULL,
                platform TEXT,
                score REAL NOT NULL,
                reorder_rate REAL NOT NULL,
                avg_order_value REAL,
                last_ordered TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cuisine_by_day(
                id INTEGER PRIMARY KEY,
                day_of_week INTEGER NOT NULL,
                cuisine TEXT NOT NULL,
                frequency INTEGER NOT NULL,
                confidence REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trigger_log(
                id INTEGER PRIMARY KEY,
                type TEXT NOT NULL,
                trigger_reasons_json TEXT NOT NULL,
                confidence REAL NOT NULL,
                fired_at TEXT NOT NULL,
                suppressed INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS suggestions(
                id INTEGER PRIMARY KEY,
                trigger_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                reason_string TEXT NOT NULL,
                shown_at TEXT NOT NULL,
                outcome TEXT,
                outcome_at TEXT,
                FOREIGN KEY(trigger_id) REFERENCES trigger_log(id)
            );

            CREATE TABLE IF NOT EXISTS dismissed_suggestions(
                id INTEGER PRIMARY KEY,
                suggestion_id INTEGER NOT NULL,
                dismissed_at TEXT NOT NULL,
                FOREIGN KEY(suggestion_id) REFERENCES suggestions(id)
            );

            CREATE TABLE IF NOT EXISTS confirmed_suggestions(
                id INTEGER PRIMARY KEY,
                suggestion_id INTEGER NOT NULL,
                edited INTEGER NOT NULL,
                edits_json TEXT,
                confirmed_at TEXT NOT NULL,
                FOREIGN KEY(suggestion_id) REFERENCES suggestions(id)
            );

            CREATE TABLE IF NOT EXISTS pattern_feedback(
                id INTEGER PRIMARY KEY,
                pattern_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                delta REAL NOT NULL,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scraper_cache(
                id INTEGER PRIMARY KEY,
                cache_key TEXT NOT NULL UNIQUE,
                data_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                is_stale INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS scraper_snapshots(
                id INTEGER PRIMARY KEY,
                platform TEXT NOT NULL,
                page_url TEXT,
                html_snapshot TEXT NOT NULL,
                captured_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS platform_connections(
                id INTEGER PRIMARY KEY,
                platform TEXT NOT NULL UNIQUE,
                storage_state_path TEXT,
                last_auth TEXT,
                status TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_rides_departure_time ON rides(departure_time DESC);
            CREATE INDEX IF NOT EXISTS idx_orders_ordered_at ON orders(ordered_at DESC);
            CREATE INDEX IF NOT EXISTS idx_departure_patterns_day_bin ON departure_patterns(day_of_week, hour_bin);
            CREATE INDEX IF NOT EXISTS idx_order_patterns_day_bin ON order_patterns(day_of_week, hour_bin);
            CREATE INDEX IF NOT EXISTS idx_trigger_log_fired_at ON trigger_log(fired_at DESC);
            CREATE INDEX IF NOT EXISTS idx_suggestions_outcome ON suggestions(outcome, shown_at DESC);
            CREATE INDEX IF NOT EXISTS idx_dismissed_suggestion_id ON dismissed_suggestions(suggestion_id);
            CREATE INDEX IF NOT EXISTS idx_confirmed_suggestion_id ON confirmed_suggestions(suggestion_id);
            CREATE INDEX IF NOT EXISTS idx_pattern_feedback_pattern ON pattern_feedback(pattern_id, applied_at DESC);
            CREATE INDEX IF NOT EXISTS idx_scraper_snapshots_platform ON scraper_snapshots(platform, captured_at DESC);
            """
        )
        ride_columns = {row["name"] for row in conn.execute("PRAGMA table_info(rides)").fetchall()}
        if "origin_label" not in ride_columns:
            conn.execute("ALTER TABLE rides ADD COLUMN origin_label TEXT")


def set_setting(key: str, value: str) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO app_settings(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, utc_now().isoformat()),
        )


def get_setting(key: str, default: str | None = None) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def upsert_platform_connection(
    platform: str,
    *,
    storage_state_path: str | None = None,
    last_auth: str | None = None,
    status: str | None = None,
) -> None:
    existing = get_platform_connection(platform) or {}
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO platform_connections(platform, storage_state_path, last_auth, status)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(platform) DO UPDATE SET
                storage_state_path = excluded.storage_state_path,
                last_auth = excluded.last_auth,
                status = excluded.status
            """,
            (
                platform,
                storage_state_path if storage_state_path is not None else existing.get("storage_state_path"),
                last_auth if last_auth is not None else existing.get("last_auth"),
                status if status is not None else existing.get("status", "disconnected"),
            ),
        )


def get_platform_connection(platform: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM platform_connections WHERE platform = ?",
            (platform,),
        ).fetchone()
    return _row_to_dict(row)


def list_platform_connections() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM platform_connections ORDER BY platform ASC"
        ).fetchall()
    return _rows_to_list(rows)


def insert_ride(ride: dict[str, Any]) -> None:
    departure_time = _to_iso(
        ride.get("departure_time")
        or ride.get("request_timestamp")
        or ride.get("booking_time")
    )
    synced_at = _to_iso(ride.get("synced_at"))
    external_id = ride.get("external_ride_id") or ride.get("id")
    platform = ride.get("platform") or ride.get("source_platform") or "uber"
    ride_id = _stable_row_id(str(platform), external_id)

    with get_db() as conn:
        if ride_id is not None:
            existing = conn.execute("SELECT * FROM rides WHERE id = ?", (ride_id,)).fetchone()
            if existing:
                existing = dict(existing)
                conn.execute(
                    """
                    UPDATE rides
                    SET platform = ?,
                        origin_label = COALESCE(origin_label, ?),
                        origin_lat = COALESCE(origin_lat, ?),
                        origin_lng = COALESCE(origin_lng, ?),
                        dest_lat = COALESCE(dest_lat, ?),
                        dest_lng = COALESCE(dest_lng, ?),
                        dest_label = COALESCE(dest_label, ?),
                        departure_time = COALESCE(departure_time, ?),
                        ride_type = COALESCE(ride_type, ?),
                        fare = COALESCE(fare, ?),
                        duration_min = COALESCE(duration_min, ?),
                        synced_at = ?
                    WHERE id = ?
                    """,
                    (
                        platform,
                        ride.get("origin_label") or ride.get("pickup_address"),
                        ride.get("origin_lat") if ride.get("origin_lat") is not None else ride.get("pickup_lat"),
                        ride.get("origin_lng") if ride.get("origin_lng") is not None else ride.get("pickup_lng"),
                        ride.get("dest_lat") if ride.get("dest_lat") is not None else ride.get("dropoff_lat"),
                        ride.get("dest_lng") if ride.get("dest_lng") is not None else ride.get("dropoff_lng"),
                        ride.get("dest_label") or ride.get("dropoff_address") or ride.get("title"),
                        departure_time,
                        ride.get("ride_type"),
                        ride.get("fare") if ride.get("fare") is not None else ride.get("price"),
                        ride.get("duration_min") if ride.get("duration_min") is not None else ride.get("duration_minutes"),
                        synced_at,
                        ride_id,
                    ),
                )
                return
        conn.execute(
            """
            INSERT INTO rides(id, platform, origin_label, origin_lat, origin_lng, dest_lat, dest_lng,
                              dest_label, departure_time, ride_type, fare, duration_min, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ride_id,
                platform,
                ride.get("origin_label") or ride.get("pickup_address"),
                ride.get("origin_lat") if ride.get("origin_lat") is not None else ride.get("pickup_lat"),
                ride.get("origin_lng") if ride.get("origin_lng") is not None else ride.get("pickup_lng"),
                ride.get("dest_lat") if ride.get("dest_lat") is not None else ride.get("dropoff_lat"),
                ride.get("dest_lng") if ride.get("dest_lng") is not None else ride.get("dropoff_lng"),
                ride.get("dest_label") or ride.get("dropoff_address") or ride.get("title"),
                departure_time,
                ride.get("ride_type"),
                ride.get("fare") if ride.get("fare") is not None else ride.get("price"),
                ride.get("duration_min") if ride.get("duration_min") is not None else ride.get("duration_minutes"),
                synced_at,
            ),
        )


def delete_low_fidelity_uber_rides() -> int:
    """Remove Uber rows created from placeholder scrape data."""
    with get_db() as conn:
        cursor = conn.execute(
            """
            DELETE FROM rides
            WHERE platform = 'uber'
              AND origin_lat IS NULL
              AND origin_lng IS NULL
              AND dest_lat IS NULL
              AND dest_lng IS NULL
                            AND fare IS NULL
                            AND duration_min IS NULL
                            AND (dest_label IS NULL OR TRIM(dest_label) = '' OR LOWER(TRIM(dest_label)) IN (
                                        'unknown destination',
                                        'unknown dropoff',
                                        'unknown'
                            ))
                            AND (ride_type IS NULL OR TRIM(ride_type) = '' OR ride_type = 'UberX')
            """
        )
        return int(cursor.rowcount or 0)


def _normalize_items_json(order: dict[str, Any]) -> str:
    if order.get("items_json"):
        value = order["items_json"]
        if isinstance(value, str):
            return value
        return _json(value) or "[]"
    item_name = order.get("item_name")
    if item_name:
        return _json([{"name": item_name, "quantity": 1}]) or "[]"
    return "[]"


def insert_food_order(order: dict[str, Any]) -> None:
    ordered_at = _to_iso(order.get("ordered_at") or order.get("order_timestamp"))
    synced_at = _to_iso(order.get("synced_at"))
    external_id = order.get("external_order_id") or order.get("id")
    order_id = None
    if external_id is not None:
        try:
            order_id = int(str(external_id))
        except ValueError:
            order_id = None

    with get_db() as conn:
        if order_id is not None:
            existing = conn.execute("SELECT 1 FROM orders WHERE id = ?", (order_id,)).fetchone()
            if existing:
                return
        conn.execute(
            """
            INSERT INTO orders(id, platform, restaurant_id, restaurant_name, items_json,
                               total_price, ordered_at, delivery_time_min, cuisine, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                order.get("platform") or order.get("source_platform") or "swiggy",
                order.get("restaurant_id") or order.get("restaurant_name"),
                order.get("restaurant_name"),
                _normalize_items_json(order),
                order.get("total_price") if order.get("total_price") is not None else order.get("price"),
                ordered_at,
                order.get("delivery_time_min") if order.get("delivery_time_min") is not None else order.get("eta_minutes"),
                order.get("cuisine"),
                synced_at,
            ),
        )


def get_ride_history(limit: int = 100, offset: int = 0, source_platform: str | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM rides"
    params: list[Any] = []
    if source_platform:
        query += " WHERE platform = ?"
        params.append(source_platform)
    query += " ORDER BY departure_time DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with get_db() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return _rows_to_list(rows)


def get_food_order_history(limit: int = 100, offset: int = 0, source_platform: str | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM orders"
    params: list[Any] = []
    if source_platform:
        query += " WHERE platform = ?"
        params.append(source_platform)
    query += " ORDER BY ordered_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with get_db() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return _rows_to_list(rows)


def get_ride_count(source_platform: str | None = None) -> int:
    with get_db() as conn:
        if source_platform:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM rides WHERE platform = ?", (source_platform,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM rides").fetchone()
    return int(row["cnt"] if row else 0)


def get_food_order_count(source_platform: str | None = None) -> int:
    with get_db() as conn:
        if source_platform:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM orders WHERE platform = ?", (source_platform,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM orders").fetchone()
    return int(row["cnt"] if row else 0)


def replace_destination_clusters(clusters: list[dict[str, Any]]) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM destination_clusters")
        conn.executemany(
            """
            INSERT INTO destination_clusters(id, centroid_lat, centroid_lng, label, member_count, last_visited)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    cluster.get("id"),
                    cluster["centroid_lat"],
                    cluster["centroid_lng"],
                    cluster.get("label"),
                    cluster["member_count"],
                    _to_iso(cluster["last_visited"]),
                )
                for cluster in clusters
            ],
        )


def replace_departure_patterns(patterns: list[dict[str, Any]]) -> None:
    existing_rows = list_departure_patterns(include_suppressed=True)
    existing = {
        (
            row["day_of_week"],
            row["hour_bin"],
            row["destination_id"],
            row["platform"],
            row["ride_type"],
        ): row
        for row in existing_rows
    }
    with get_db() as conn:
        conn.execute("DELETE FROM departure_patterns")
        conn.executemany(
            """
            INSERT INTO departure_patterns(
                id, day_of_week, hour_bin, destination_id, platform, ride_type,
                frequency, confidence, dismissal_count, suppressed, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    (existing.get((
                        pattern["day_of_week"],
                        pattern["hour_bin"],
                        pattern.get("destination_id"),
                        pattern.get("platform"),
                        pattern.get("ride_type"),
                    )) or {}).get("id"),
                    pattern["day_of_week"],
                    pattern["hour_bin"],
                    pattern.get("destination_id"),
                    pattern.get("platform"),
                    pattern.get("ride_type"),
                    pattern["frequency"],
                    pattern["confidence"],
                    pattern.get("dismissal_count", (existing.get((
                        pattern["day_of_week"],
                        pattern["hour_bin"],
                        pattern.get("destination_id"),
                        pattern.get("platform"),
                        pattern.get("ride_type"),
                    )) or {}).get("dismissal_count", 0)),
                    1 if pattern.get("suppressed", (existing.get((
                        pattern["day_of_week"],
                        pattern["hour_bin"],
                        pattern.get("destination_id"),
                        pattern.get("platform"),
                        pattern.get("ride_type"),
                    )) or {}).get("suppressed", 0)) else 0,
                    _to_iso(pattern["last_seen"]),
                )
                for pattern in patterns
            ],
        )


def replace_order_patterns(patterns: list[dict[str, Any]]) -> None:
    existing_rows = list_order_patterns(include_suppressed=True)
    existing = {
        (
            row["day_of_week"],
            row["hour_bin"],
            row["cuisine"],
            row["restaurant_id"],
        ): row
        for row in existing_rows
    }
    with get_db() as conn:
        conn.execute("DELETE FROM order_patterns")
        conn.executemany(
            """
            INSERT INTO order_patterns(
                id, day_of_week, hour_bin, cuisine, restaurant_id, confidence,
                dismissal_count, suppressed, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    (existing.get((
                        pattern["day_of_week"],
                        pattern["hour_bin"],
                        pattern.get("cuisine"),
                        pattern.get("restaurant_id"),
                    )) or {}).get("id"),
                    pattern["day_of_week"],
                    pattern["hour_bin"],
                    pattern.get("cuisine"),
                    pattern.get("restaurant_id"),
                    pattern["confidence"],
                    pattern.get("dismissal_count", (existing.get((
                        pattern["day_of_week"],
                        pattern["hour_bin"],
                        pattern.get("cuisine"),
                        pattern.get("restaurant_id"),
                    )) or {}).get("dismissal_count", 0)),
                    1 if pattern.get("suppressed", (existing.get((
                        pattern["day_of_week"],
                        pattern["hour_bin"],
                        pattern.get("cuisine"),
                        pattern.get("restaurant_id"),
                    )) or {}).get("suppressed", 0)) else 0,
                    _to_iso(pattern["last_seen"]),
                )
                for pattern in patterns
            ],
        )


def replace_restaurant_patterns(patterns: list[dict[str, Any]]) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM restaurant_patterns")
        conn.executemany(
            """
            INSERT INTO restaurant_patterns(
                restaurant_id, platform, score, reorder_rate, avg_order_value, last_ordered
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    pattern["restaurant_id"],
                    pattern.get("platform"),
                    pattern["score"],
                    pattern["reorder_rate"],
                    pattern.get("avg_order_value"),
                    _to_iso(pattern["last_ordered"]),
                )
                for pattern in patterns
            ],
        )


def replace_cuisine_by_day(rows: list[dict[str, Any]]) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM cuisine_by_day")
        conn.executemany(
            """
            INSERT INTO cuisine_by_day(day_of_week, cuisine, frequency, confidence)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    row["day_of_week"],
                    row["cuisine"],
                    row["frequency"],
                    row["confidence"],
                )
                for row in rows
            ],
        )


def list_destination_clusters() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM destination_clusters ORDER BY member_count DESC, last_visited DESC"
        ).fetchall()
    return _rows_to_list(rows)


def list_departure_patterns(include_suppressed: bool = False) -> list[dict[str, Any]]:
    query = "SELECT * FROM departure_patterns"
    if not include_suppressed:
        query += " WHERE suppressed = 0"
    query += " ORDER BY confidence DESC, frequency DESC, last_seen DESC"
    with get_db() as conn:
        rows = conn.execute(query).fetchall()
    return _rows_to_list(rows)


def list_order_patterns(include_suppressed: bool = False) -> list[dict[str, Any]]:
    query = "SELECT * FROM order_patterns"
    if not include_suppressed:
        query += " WHERE suppressed = 0"
    query += " ORDER BY confidence DESC, last_seen DESC"
    with get_db() as conn:
        rows = conn.execute(query).fetchall()
    return _rows_to_list(rows)


def list_restaurant_patterns() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM restaurant_patterns ORDER BY score DESC, last_ordered DESC"
        ).fetchall()
    return _rows_to_list(rows)


def list_cuisine_by_day() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM cuisine_by_day ORDER BY day_of_week ASC, confidence DESC"
        ).fetchall()
    return _rows_to_list(rows)


def get_destination_cluster(destination_id: int) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM destination_clusters WHERE id = ?",
            (destination_id,),
        ).fetchone()
    return _row_to_dict(row)


def find_matching_departure_patterns(day_of_week: int, minute_of_day: int, tolerance_minutes: int) -> list[dict[str, Any]]:
    patterns = []
    for row in list_departure_patterns():
        if row["day_of_week"] != day_of_week:
            continue
        bin_minute = int(row["hour_bin"]) * 15
        if _circular_minute_diff(bin_minute, minute_of_day) <= tolerance_minutes:
            patterns.append(row)
    return patterns


def find_matching_order_patterns(day_of_week: int, minute_of_day: int, tolerance_minutes: int) -> list[dict[str, Any]]:
    patterns = []
    for row in list_order_patterns():
        if row["day_of_week"] != day_of_week:
            continue
        bin_minute = int(row["hour_bin"]) * 15
        if _circular_minute_diff(bin_minute, minute_of_day) <= tolerance_minutes:
            patterns.append(row)
    return patterns


def _circular_minute_diff(left: int, right: int) -> int:
    diff = abs(left - right)
    return min(diff, (24 * 60) - diff)


def get_rides_for_destination(destination_id: int) -> list[dict[str, Any]]:
    cluster = get_destination_cluster(destination_id)
    if not cluster:
        return []
    lat = cluster["centroid_lat"]
    lng = cluster["centroid_lng"]
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM rides
            WHERE dest_lat IS NOT NULL AND dest_lng IS NOT NULL
              AND ABS(dest_lat - ?) <= 0.01
              AND ABS(dest_lng - ?) <= 0.01
            ORDER BY departure_time DESC
            """,
            (lat, lng),
        ).fetchall()
    return _rows_to_list(rows)


def get_orders_for_restaurant(restaurant_id: str) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE restaurant_id = ? ORDER BY ordered_at DESC",
            (restaurant_id,),
        ).fetchall()
    return _rows_to_list(rows)


def get_recent_order_for_restaurant(restaurant_id: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE restaurant_id = ? ORDER BY ordered_at DESC LIMIT 1",
            (restaurant_id,),
        ).fetchone()
    return _row_to_dict(row)


def any_ride_booked_today_in_window(destination_id: int, center_minute: int, window_minutes: int, now: datetime) -> bool:
    rides = get_rides_for_destination(destination_id)
    today = now.date()
    for ride in rides:
        departure = _from_iso(ride["departure_time"])
        if not departure or departure.date() != today:
            continue
        minute = departure.hour * 60 + departure.minute
        if abs(minute - center_minute) <= window_minutes:
            return True
    return False


def any_order_today_in_window(restaurant_id: str, center_minute: int, window_minutes: int, now: datetime) -> bool:
    orders = get_orders_for_restaurant(restaurant_id)
    today = now.date()
    for order in orders:
        ordered = _from_iso(order["ordered_at"])
        if not ordered or ordered.date() != today:
            continue
        minute = ordered.hour * 60 + ordered.minute
        if abs(minute - center_minute) <= window_minutes:
            return True
    return False


def count_recent_dismissals_for_pattern(
    pattern_id: str, days: int = 7, now: datetime | None = None
) -> int:
    # `now` is injectable so an offline replay can evaluate the policy at a
    # simulated timestamp. Reading the wall clock unconditionally would make
    # every historical session look like it had no recent dismissals.
    cutoff = ((now or utc_now()) - timedelta(days=days)).isoformat()
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM dismissed_suggestions ds
            JOIN suggestions s ON s.id = ds.suggestion_id
            WHERE json_extract(s.payload_json, '$.pattern_ref') = ?
              AND ds.dismissed_at >= ?
            """,
            (pattern_id, cutoff),
        ).fetchone()
    return int(row["cnt"] if row else 0)


def get_last_trigger_time(trigger_type: str, suppressed: bool = False) -> datetime | None:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT fired_at FROM trigger_log
            WHERE type = ? AND suppressed = ?
            ORDER BY fired_at DESC
            LIMIT 1
            """,
            (trigger_type, 1 if suppressed else 0),
        ).fetchone()
    return _from_iso(row["fired_at"]) if row else None


def insert_trigger_event(
    trigger_type: str,
    trigger_reasons: list[str],
    confidence: float,
    fired_at: datetime | None = None,
    suppressed: bool = False,
) -> int:
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO trigger_log(type, trigger_reasons_json, confidence, fired_at, suppressed)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                trigger_type,
                _json(trigger_reasons) or "[]",
                confidence,
                _to_iso(fired_at),
                1 if suppressed else 0,
            ),
        )
        return int(cursor.lastrowid)


def list_trigger_events(limit: int = 20) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trigger_log ORDER BY fired_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    events = _rows_to_list(rows)
    for item in events:
        item["trigger_reasons"] = json.loads(item["trigger_reasons_json"] or "[]")
    return events


def insert_suggestion(
    trigger_id: int,
    suggestion_type: str,
    payload: dict[str, Any],
    reason_string: str,
    shown_at: datetime | None = None,
) -> int:
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO suggestions(trigger_id, type, payload_json, reason_string, shown_at, outcome, outcome_at)
            VALUES (?, ?, ?, ?, ?, NULL, NULL)
            """,
            (trigger_id, suggestion_type, _json(payload) or "{}", reason_string, _to_iso(shown_at)),
        )
        return int(cursor.lastrowid)


def get_suggestion(suggestion_id: int) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM suggestions WHERE id = ?",
            (suggestion_id,),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["payload"] = json.loads(result["payload_json"] or "{}")
    return result


def get_latest_pending_suggestion() -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM suggestions
            WHERE outcome IS NULL
            ORDER BY shown_at DESC
            LIMIT 1
            """
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["payload"] = json.loads(result["payload_json"] or "{}")
    return result


def list_pending_suggestions() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM suggestions WHERE outcome IS NULL ORDER BY shown_at ASC"
        ).fetchall()
    results = _rows_to_list(rows)
    for item in results:
        item["payload"] = json.loads(item["payload_json"] or "{}")
    return results


def update_suggestion_outcome(
    suggestion_id: int,
    outcome: str,
    *,
    edits: dict[str, Any] | None = None,
    at: datetime | None = None,
) -> dict[str, Any] | None:
    timestamp = _to_iso(at)
    with get_db() as conn:
        conn.execute(
            "UPDATE suggestions SET outcome = ?, outcome_at = ? WHERE id = ?",
            (outcome, timestamp, suggestion_id),
        )
        if outcome == "dismissed" or outcome == "ignored":
            conn.execute(
                "INSERT INTO dismissed_suggestions(suggestion_id, dismissed_at) VALUES (?, ?)",
                (suggestion_id, timestamp),
            )
        elif outcome == "confirmed":
            conn.execute(
                """
                INSERT INTO confirmed_suggestions(suggestion_id, edited, edits_json, confirmed_at)
                VALUES (?, ?, ?, ?)
                """,
                (suggestion_id, 1 if edits else 0, _json(edits), timestamp),
            )
        row = conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["payload"] = json.loads(result["payload_json"] or "{}")
    return result


def mark_expired_pending_suggestions(
    timeout_minutes: int = 10, now: datetime | None = None
) -> list[dict[str, Any]]:
    current = now or utc_now()
    cutoff = (current - timedelta(minutes=timeout_minutes)).isoformat()
    pending = []
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM suggestions
            WHERE outcome IS NULL AND shown_at <= ?
            ORDER BY shown_at ASC
            """,
            (cutoff,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE suggestions SET outcome = 'ignored', outcome_at = ? WHERE id = ?",
                (current.isoformat(), row["id"]),
            )
            conn.execute(
                "INSERT INTO dismissed_suggestions(suggestion_id, dismissed_at) VALUES (?, ?)",
                (row["id"], current.isoformat()),
            )
            pending.append(dict(row))
    for item in pending:
        item["payload"] = json.loads(item["payload_json"] or "{}")
    return pending


def insert_pattern_feedback(pattern_id: str, event_type: str, delta: float, applied_at: datetime | None = None) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO pattern_feedback(pattern_id, event_type, delta, applied_at) VALUES (?, ?, ?, ?)",
            (pattern_id, event_type, delta, _to_iso(applied_at)),
        )


def get_feedback_history(pattern_id: str) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM pattern_feedback WHERE pattern_id = ? ORDER BY applied_at DESC",
            (pattern_id,),
        ).fetchall()
    return _rows_to_list(rows)


def get_pattern_row(pattern_id: str) -> tuple[str, dict[str, Any] | None]:
    table, _, raw_id = pattern_id.partition(":")
    if not raw_id:
        return table, None
    with get_db() as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (raw_id,)).fetchone()
    return table, _row_to_dict(row)


def update_pattern_state(
    table: str,
    row_id: int,
    *,
    frequency_delta: int = 0,
    confidence: float | None = None,
    dismissal_count_delta: int = 0,
    suppressed: bool | None = None,
    last_seen: datetime | None = None,
) -> None:
    row = None
    with get_db() as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if not row:
            return
        updates: dict[str, Any] = {}
        if "frequency" in row.keys():
            updates["frequency"] = int(row["frequency"]) + frequency_delta
        if confidence is not None and "confidence" in row.keys():
            updates["confidence"] = confidence
        if "dismissal_count" in row.keys():
            updates["dismissal_count"] = int(row["dismissal_count"]) + dismissal_count_delta
        if suppressed is not None and "suppressed" in row.keys():
            updates["suppressed"] = 1 if suppressed else 0
        if last_seen is not None and "last_seen" in row.keys():
            updates["last_seen"] = _to_iso(last_seen)
        if not updates:
            return
        sets = ", ".join(f"{column} = ?" for column in updates)
        conn.execute(
            f"UPDATE {table} SET {sets} WHERE id = ?",
            (*updates.values(), row_id),
        )


def insert_pattern_variant(table: str, row: dict[str, Any], overrides: dict[str, Any]) -> int | None:
    if table == "departure_patterns":
        columns = [
            "day_of_week",
            "hour_bin",
            "destination_id",
            "platform",
            "ride_type",
            "frequency",
            "confidence",
            "dismissal_count",
            "suppressed",
            "last_seen",
        ]
    elif table == "order_patterns":
        columns = [
            "day_of_week",
            "hour_bin",
            "cuisine",
            "restaurant_id",
            "confidence",
            "dismissal_count",
            "suppressed",
            "last_seen",
        ]
    else:
        return None
    data = {column: row.get(column) for column in columns}
    data.update(overrides)
    if "frequency" in data and data["frequency"] is None:
        data["frequency"] = 1
    if "dismissal_count" in data and data["dismissal_count"] is None:
        data["dismissal_count"] = 0
    if "suppressed" in data and data["suppressed"] is None:
        data["suppressed"] = 0
    if "last_seen" in data:
        data["last_seen"] = _to_iso(data["last_seen"])
    with get_db() as conn:
        columns_sql = ", ".join(data.keys())
        values_sql = ", ".join("?" for _ in data)
        cursor = conn.execute(
            f"INSERT INTO {table}({columns_sql}) VALUES ({values_sql})",
            tuple(data.values()),
        )
        return int(cursor.lastrowid)


def upsert_scraper_cache(cache_key: str, data: dict[str, Any], fetched_at: datetime | None = None, is_stale: bool = False) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO scraper_cache(cache_key, data_json, fetched_at, is_stale)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                data_json = excluded.data_json,
                fetched_at = excluded.fetched_at,
                is_stale = excluded.is_stale
            """,
            (cache_key, _json(data) or "{}", _to_iso(fetched_at), 1 if is_stale else 0),
        )


def get_scraper_cache(cache_key: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM scraper_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["data"] = json.loads(result["data_json"] or "{}")
    return result


def add_scraper_snapshot(platform: str, page_url: str | None, html_snapshot: str, captured_at: datetime | None = None) -> None:
    timestamp = _to_iso(captured_at)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO scraper_snapshots(platform, page_url, html_snapshot, captured_at)
            VALUES (?, ?, ?, ?)
            """,
            (platform, page_url, html_snapshot, timestamp),
        )
        rows = conn.execute(
            """
            SELECT id FROM scraper_snapshots
            WHERE platform = ?
            ORDER BY captured_at DESC
            """,
            (platform,),
        ).fetchall()
        for row in rows[3:]:
            conn.execute("DELETE FROM scraper_snapshots WHERE id = ?", (row["id"],))


def get_latest_scraper_snapshot(platform: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM scraper_snapshots
            WHERE platform = ?
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (platform,),
        ).fetchone()
    return _row_to_dict(row)
